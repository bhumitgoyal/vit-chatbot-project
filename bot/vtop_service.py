"""
bot/vtop_service.py
VTOP live-session engine for VITopia AI.

Design goals (see docs/VTOP_ENDPOINTS.md):
  * One live authenticated VTOP session per chat user_id. No cross-user profile
    sharing, no month-old JSON served as "live" data.
  * CAPTCHA is fetched with a retry loop — VTOP only renders a CAPTCHA image on
    some prelogin attempts, so we refresh until an image appears.
  * Every data module is fetched fresh from VTOP (with a short per-module TTL so a
    single conversation does not hammer the portal). If an endpoint cannot be
    reached the scraper returns {"status": "unavailable", ...} and the caller may
    fall back to clearly-labelled unverified reference data.
  * Nothing about other accounts or internal access scope is ever exposed.
"""

import os
import re
import json
import time
import base64
import logging
import threading
from pathlib import Path
from typing import Dict, Any, Optional, List, Tuple

import requests
from bs4 import BeautifulSoup

from bot.school_directory import resolve_school_and_dept

logger = logging.getLogger("vit.vtop_service")

VTOP_BASE_URL = "https://vtop.vit.ac.in/vtop"
VTOP_ROOT = "https://vtop.vit.ac.in"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
STUDENT_DATA_DIR = DATA_DIR / "students"
CREDENTIALS_DIR = DATA_DIR / "credentials"
STUDENT_DATA_DIR.mkdir(parents=True, exist_ok=True)
CREDENTIALS_DIR.mkdir(parents=True, exist_ok=True)

# Prelogin sessions waiting for a CAPTCHA answer, keyed by an opaque session_id.
ACTIVE_SESSIONS: Dict[str, Dict[str, Any]] = {}

# Live authenticated sessions, keyed by chat user_id.
SESSIONS: Dict[str, "LiveSession"] = {}

# How long a scraped module may be reused within a conversation before re-fetch.
MODULE_TTL_SECONDS = 300  # 5 minutes
# Prelogin CAPTCHA retry loop.
CAPTCHA_MAX_ATTEMPTS = 12
CAPTCHA_RETRY_DELAY = 0.7
# Silent re-login (stored creds + vision solver) attempts.
SILENT_REFRESH_MAX_ATTEMPTS = 5

SESSION_DEAD_MARKERS = (
    "Session Timed Out",
    "session timed out",
    "vtopLoginForm",
    "You are logged out",
    "HTTP Status 404",
)


class LiveSession:
    """A single authenticated VTOP connection bound to one chat user."""

    def __init__(self, user_id: str, http: requests.Session, csrf: str,
                 register_no: str, student_name: str):
        self.user_id = user_id
        self.http = http
        self.csrf = csrf
        self.register_no = register_no
        self.student_name = student_name
        self.semester_id: Optional[str] = None
        self.semester_name: Optional[str] = None
        self.authed_at = time.time()
        self.alive = True
        self.profile: Dict[str, Any] = {
            "register_no": register_no,
            "username": register_no,
            "student_name": student_name,
        }
        # module name -> {"ts": float, "result": dict}
        self.module_cache: Dict[str, Dict[str, Any]] = {}
        # requests.Session is not safe for truly concurrent use; serialise this
        # user's own scrapes (different users still run in parallel threads).
        self.lock = threading.Lock()

    def whoami(self) -> Dict[str, Any]:
        return {
            "register_no": self.register_no,
            "student_name": self.student_name,
            "active_semester": self.semester_name or self.profile.get("active_semester"),
        }


def _looks_dead(text: str, url: str = "") -> bool:
    if "login" in url and "/vtop/login" not in url:
        return True
    return any(m in text for m in SESSION_DEAD_MARKERS)


def _new_http() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })
    return s


def _post(http: requests.Session, path: str, reg_no: str, csrf: str,
          extra: Optional[Dict[str, str]] = None, timeout: int = 12) -> requests.Response:
    """POST a VTOP action with the standard authorizedID/_csrf/x body."""
    url = path if path.startswith("http") else f"{VTOP_BASE_URL}/{path.lstrip('/')}"
    body = {
        "authorizedID": reg_no,
        "_csrf": csrf,
        "x": time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime()),
    }
    if extra:
        body.update(extra)
    return http.post(
        url,
        data=body,
        headers={
            "Referer": f"{VTOP_BASE_URL}/content",
            "Origin": VTOP_ROOT,
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        timeout=timeout,
    )


def _rows_from_html(doc, min_cols: int = 2) -> List[List[str]]:
    """Flatten every <tr> into a list of trimmed cell-text lists.
    `doc` may be a raw HTML string or an already-parsed BeautifulSoup."""
    soup = doc if isinstance(doc, BeautifulSoup) else BeautifulSoup(doc, "html.parser")
    out: List[List[str]] = []
    for table in soup.find_all("table"):
        for tr in table.find_all("tr"):
            cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
            if len(cells) >= min_cols:
                out.append(cells)
    return out


class VTOPService:
    # ── construction ────────────────────────────────────────────────────────
    def __init__(self):
        # Retained only so a silent re-login can warm-start from the last known
        # profile of the SAME register number. Never served to another user_id.
        self._own_profile_cache: Dict[str, Dict[str, Any]] = {}

    # ── session accessors ──────────────────────────────────────────────────
    def get_session(self, user_id: str) -> Optional[LiveSession]:
        sess = SESSIONS.get(user_id)
        if sess and sess.alive:
            return sess
        return None

    def is_authenticated(self, user_id: str) -> bool:
        return self.get_session(user_id) is not None

    def whoami(self, user_id: str) -> Optional[Dict[str, Any]]:
        sess = self.get_session(user_id)
        return sess.whoami() if sess else None

    def logout(self, user_id: str) -> None:
        sess = SESSIONS.pop(user_id, None)
        if sess:
            try:
                sess.http.get(f"{VTOP_BASE_URL}/logout", timeout=8)
            except Exception:
                pass
            sess.alive = False
            # Drop any other key (e.g. the register number) pointing at this session.
            for k in [k for k, v in SESSIONS.items() if v is sess]:
                SESSIONS.pop(k, None)

    # ── CAPTCHA (retry loop) ───────────────────────────────────────────────
    def _prelogin(self, http: requests.Session) -> Tuple[str, Optional[str]]:
        """One prelogin pass. Returns (csrf, captcha_data_uri_or_None)."""
        res1 = http.get(f"{VTOP_BASE_URL}/initialProcess", timeout=12)
        soup1 = BeautifulSoup(res1.text, "html.parser")
        std_form = soup1.find("form", {"id": "stdForm"})
        if not std_form:
            std_form = soup1.find("form")
        csrf1 = ""
        flag1 = "VTOP"
        action = None
        if std_form:
            action = std_form.get("action")
            ci = std_form.find("input", {"name": "_csrf"})
            fi = std_form.find("input", {"name": "flag"})
            csrf1 = ci["value"] if ci and ci.has_attr("value") else ""
            flag1 = fi["value"] if fi and fi.has_attr("value") else "VTOP"

        prelogin_url = (
            f"{VTOP_ROOT}{action}" if action and action.startswith("/")
            else f"{VTOP_BASE_URL}/prelogin/setup"
        )
        res2 = http.post(prelogin_url, data={"_csrf": csrf1, "flag": flag1}, timeout=12)
        soup2 = BeautifulSoup(res2.text, "html.parser")

        form = soup2.find("form", {"id": "vtopLoginForm"}) or soup2.find("form")
        csrf_login = csrf1
        if form:
            ci = form.find("input", {"name": "_csrf"})
            if ci and ci.has_attr("value"):
                csrf_login = ci["value"]

        captcha_div = soup2.find("div", {"id": "captchaBlock"})
        img = None
        if captcha_div:
            img = captcha_div.find("img")
        if not img and form:
            img = form.find("img")
        if not img:
            img = soup2.find("img", src=re.compile(r"^data:image"))

        captcha_src = None
        if img and img.get("src", "").startswith("data:image"):
            captcha_src = img["src"]
        return csrf_login, captcha_src

    def _prelogin_with_captcha(self, max_attempts: int = CAPTCHA_MAX_ATTEMPTS
                               ) -> Tuple[Optional[requests.Session], str, Optional[str], int]:
        """
        Retry prelogin until VTOP renders a CAPTCHA image.
        Returns (http_session, csrf, captcha_data_uri, attempts_used).
        """
        last_http: Optional[requests.Session] = None
        last_csrf = ""
        for attempt in range(1, max_attempts + 1):
            http = _new_http()
            try:
                csrf, captcha = self._prelogin(http)
            except Exception as e:
                logger.warning(f"Prelogin attempt {attempt} failed: {e}")
                time.sleep(CAPTCHA_RETRY_DELAY)
                continue
            last_http, last_csrf = http, csrf
            if captcha:
                logger.info(f"CAPTCHA image obtained on prelogin attempt {attempt}.")
                return http, csrf, captcha, attempt
            logger.info(f"Prelogin attempt {attempt}: no CAPTCHA image yet, retrying.")
            time.sleep(CAPTCHA_RETRY_DELAY)
        return last_http, last_csrf, None, max_attempts

    @staticmethod
    def _prune_active_sessions(max_age: int = 600) -> None:
        cutoff = time.time() - max_age
        for sid in [s for s, d in ACTIVE_SESSIONS.items() if d.get("created_at", 0) < cutoff]:
            ACTIVE_SESSIONS.pop(sid, None)

    def fetch_captcha(self) -> Dict[str, Any]:
        self._prune_active_sessions()
        http, csrf, captcha, attempts = self._prelogin_with_captcha()
        if not captcha or http is None:
            return {
                "status": "error",
                "message": (
                    f"VTOP did not return a CAPTCHA image after {attempts} attempts. "
                    "The portal may be under maintenance — try again shortly."
                ),
            }
        session_id = f"sess_{os.urandom(8).hex()}"
        ACTIVE_SESSIONS[session_id] = {
            "http": http,
            "csrf": csrf,
            "created_at": time.time(),
        }
        return {
            "status": "success",
            "session_id": session_id,
            "captcha_image": captcha,
            "attempts": attempts,
            "message": "CAPTCHA retrieved",
        }

    # ── login ──────────────────────────────────────────────────────────────
    def login_student(self, username: str, password: str, captcha_code: str,
                      session_id: str, user_id: Optional[str] = None) -> Dict[str, Any]:
        sess_data = ACTIVE_SESSIONS.get(session_id)
        if not sess_data:
            return {"status": "error", "message": "Login session expired. Please start the login again."}

        http: requests.Session = sess_data["http"]
        csrf: str = sess_data["csrf"]
        uname = username.strip().upper()

        try:
            res = http.post(
                f"{VTOP_BASE_URL}/login",
                data={
                    "_csrf": csrf,
                    "username": uname,
                    "password": password.strip(),
                    "captchaStr": captcha_code.strip(),
                },
                headers={
                    "Referer": f"{VTOP_BASE_URL}/prelogin/setup",
                    "Origin": VTOP_ROOT,
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                timeout=15,
            )
        except Exception as e:
            logger.error(f"VTOP login network error: {e}", exc_info=True)
            return {"status": "error", "message": f"Network error reaching VTOP: {e}"}

        lower = res.text.lower()
        if "login/error" in res.url or "invalid captcha" in lower or "invalid user id" in lower \
                or "invalid username" in lower:
            ACTIVE_SESSIONS.pop(session_id, None)
            soup = BeautifulSoup(res.text, "html.parser")
            err = soup.find("span", {"id": "errorMessage"}) or soup.find("div", {"class": "alert"})
            msg = err.get_text(strip=True) if err else "Invalid credentials or CAPTCHA."
            captcha_wrong = "captcha" in msg.lower() or "invalid captcha" in lower
            return {"status": "error", "captcha_wrong": captcha_wrong, "message": msg}

        ACTIVE_SESSIONS.pop(session_id, None)

        # Authenticated — pull the content page for the real CSRF + identity.
        try:
            content = http.get(f"{VTOP_BASE_URL}/content", timeout=15)
        except Exception as e:
            return {"status": "error", "message": f"Logged in but could not load VTOP dashboard: {e}"}

        ctext = content.text
        csrf_content = csrf
        m = re.search(r'name=["\']_csrf["\']\s+value=["\']([0-9a-fA-F-]+)["\']', ctext)
        if not m:
            m = re.search(r'csrfValue\s*=\s*["\']([0-9a-fA-F-]+)["\']', ctext)
        if m:
            csrf_content = m.group(1)

        reg_no = uname
        m = re.search(r'var\s+id\s*=\s*["\']([A-Za-z0-9]+)["\']', ctext)
        if m:
            reg_no = m.group(1).upper()

        student_name = uname.capitalize()
        m = re.search(r'([A-Za-z][A-Za-z .]+?)\s*\(\s*' + re.escape(reg_no) + r'\s*\)', ctext)
        if m:
            student_name = m.group(1).strip().title()

        live = LiveSession(user_id or reg_no, http, csrf_content, reg_no, student_name)

        # Resolve current semester (best effort) and stamp identity/CGPA.
        self._resolve_semester(live)
        self._enrich_identity(live, ctext)
        SESSIONS[live.user_id] = live

        # Persist credentials (base64) + last-known profile for silent refresh.
        self.store_credentials(reg_no, uname, password.strip())
        self._save_own_profile(live)

        return {
            "status": "success",
            "register_no": reg_no,
            "student_name": student_name,
            "active_semester": live.semester_name,
            "message": "VTOP session established.",
        }

    def _enrich_identity(self, live: LiveSession, content_text: str) -> None:
        school_code = "SCOPE"
        for code in ("SITE", "SENSE", "SELECT", "SMEC", "SCOPE"):
            if code in content_text:
                school_code = code
                break
        try:
            resolved = resolve_school_and_dept(live.register_no, school_code)
            live.profile["school"] = resolved["dean"]["school_name"]
            live.profile["_reference_dean"] = resolved["dean"]
            live.profile["_reference_hod"] = resolved["hod"]
        except Exception:
            pass
        live.profile["active_semester"] = live.semester_name

        # The VTOP dashboard prints a "CGPA and CREDIT Status" block — the most
        # authoritative CGPA source, so grab it straight from /content.
        flat = re.sub(r"\s+", " ", BeautifulSoup(content_text, "html.parser").get_text(" "))
        for pat in (r"Current\s*CGPA\D{0,25}?(\d{1,2}\.\d{1,3})",
                    r"\bCGPA\D{0,15}?(\d\.\d{2,3})\b"):
            m = re.search(pat, flat, re.I)
            if m and 0 < float(m.group(1)) <= 10:
                live.profile["cgpa"] = float(m.group(1))
                live.profile["cgpa_source"] = "VTOP dashboard"
                break
        m = re.search(r"Earned\s*Credits\D{0,15}?(\d{1,3}(?:\.\d)?)", flat, re.I)
        if m:
            live.profile["total_credits_earned"] = float(m.group(1))
        m = re.search(r"Total\s*Credits\s*Required\D{0,15}?(\d{1,3}(?:\.\d)?)", flat, re.I)
        if m:
            live.profile["total_credits_required"] = float(m.group(1))
        logger.info(f"Dashboard parse for {live.register_no}: "
                    f"CGPA={live.profile.get('cgpa')} "
                    f"earned={live.profile.get('total_credits_earned')}")

    # ── silent refresh ─────────────────────────────────────────────────────
    def ensure_live_session(self, user_id: str) -> Optional[LiveSession]:
        sess = self.get_session(user_id)
        if sess:
            return sess
        return self._silent_relogin(user_id)

    def _silent_relogin(self, user_id: str) -> Optional[LiveSession]:
        # Which register number was this chat user last logged in as?
        dead = SESSIONS.get(user_id)
        reg_no = dead.register_no if dead else None
        creds = self.get_credentials(reg_no) if reg_no else None
        if not creds:
            # Fall back to any single stored credential file (single-tenant deploys).
            files = list(CREDENTIALS_DIR.glob("*.cred"))
            if len(files) == 1:
                creds = self.get_credentials(files[0].stem)
                reg_no = files[0].stem
        if not creds:
            logger.info(f"No stored credentials to silently re-login user {user_id}.")
            return None

        for attempt in range(1, SILENT_REFRESH_MAX_ATTEMPTS + 1):
            http, csrf, captcha, _ = self._prelogin_with_captcha(max_attempts=6)
            if not captcha or http is None:
                continue
            code = self._solve_captcha_with_vision(captcha)
            if not code:
                logger.info(f"Silent relogin attempt {attempt}: vision solve failed.")
                continue
            sid = f"silent_{os.urandom(6).hex()}"
            ACTIVE_SESSIONS[sid] = {"http": http, "csrf": csrf, "created_at": time.time()}
            result = self.login_student(
                creds["username"], creds["password"], code, sid, user_id=user_id
            )
            if result.get("status") == "success":
                logger.info(f"Silent relogin succeeded for {user_id} on attempt {attempt}.")
                return SESSIONS.get(user_id)
            logger.info(f"Silent relogin attempt {attempt} failed: {result.get('message')}")
        return None

    # ── Vertex AI Vision CAPTCHA solver ───────────────────────────────────
    def _solve_captcha_with_vision(self, captcha_src: str) -> Optional[str]:
        """Read a VTOP CAPTCHA (data:image URI) with Gemini on Vertex AI.
        Returns the 4–8 char code, or None if it can't."""
        try:
            import google.auth
            from google.auth.transport.requests import Request as GoogleAuthRequest

            project_id = os.environ.get("GCP_PROJECT_ID", "nuveroai")
            location = os.environ.get("GCP_LOCATION", "us-central1")
            model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

            creds, proj = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"])
            if proj:
                project_id = proj
            if not creds.valid:
                creds.refresh(GoogleAuthRequest())

            if captcha_src.startswith("data:image"):
                head, _, b64 = captcha_src.partition(",")
                mime = head.split(";")[0].split(":", 1)[-1] or "image/png"
            else:
                r = requests.get(captcha_src, timeout=10)
                b64 = base64.b64encode(r.content).decode()
                mime = "image/png"
            if not b64:
                return None

            url = (f"https://{location}-aiplatform.googleapis.com/v1/projects/"
                   f"{project_id}/locations/{location}/publishers/google/models/"
                   f"{model}:generateContent")
            payload = {
                "contents": [{"role": "user", "parts": [
                    {"text": "This is a CAPTCHA image. Reply with ONLY the exact "
                             "characters shown — no spaces, no quotes, no explanation."},
                    {"inlineData": {"mimeType": mime, "data": b64}},
                ]}],
                # Gemini 2.5 spends output budget on hidden reasoning — disable it
                # and give generous room, or `parts` comes back empty.
                "generationConfig": {
                    "temperature": 0,
                    "maxOutputTokens": 256,
                    "thinkingConfig": {"thinkingBudget": 0},
                },
            }
            res = requests.post(
                url, json=payload, timeout=20,
                headers={"Authorization": f"Bearer {creds.token}",
                         "Content-Type": "application/json"})
            if res.status_code != 200:
                logger.warning(f"Vision CAPTCHA API {res.status_code}: {res.text[:180]}")
                return None
            cands = res.json().get("candidates", [])
            if not cands:
                return None
            parts = cands[0].get("content", {}).get("parts", [])
            text = " ".join(p.get("text", "") for p in parts)
            if not text.strip():
                logger.warning(f"Vision empty response (finishReason="
                               f"{cands[0].get('finishReason')})")
            solved = re.sub(r"[^A-Za-z0-9]", "", text).upper()
            if 4 <= len(solved) <= 8:
                logger.info(f"Vision solved CAPTCHA: {solved}")
                return solved
            logger.warning(f"Vision CAPTCHA gave unusable text: {text!r}")
        except Exception as e:
            logger.warning(f"Vision CAPTCHA solve failed: {e}")
        return None

    def login_with_autocaptcha(self, user_id: str, username: str, password: str,
                               max_attempts: int = 4) -> Dict[str, Any]:
        """Log in, solving the CAPTCHA with Vertex Vision. Falls back to
        {'status':'needs_manual_captcha', 'session_id', 'captcha_image'} so the
        chat can ask the human to type it."""
        for attempt in range(1, max_attempts + 1):
            http, csrf, captcha, _ = self._prelogin_with_captcha(max_attempts=8)
            if not captcha or http is None:
                continue
            code = self._solve_captcha_with_vision(captcha)
            if not code:
                logger.info(f"Auto-login attempt {attempt}: vision solve failed.")
                continue
            sid = f"auto_{os.urandom(6).hex()}"
            ACTIVE_SESSIONS[sid] = {"http": http, "csrf": csrf, "created_at": time.time()}
            result = self.login_student(username, password, code, sid, user_id=user_id)
            if result.get("status") == "success":
                logger.info(f"Auto-captcha login succeeded on attempt {attempt}.")
                return result
            if not result.get("captcha_wrong"):
                return result  # genuine credential error — report it
            logger.info(f"Auto-login attempt {attempt}: CAPTCHA wrong, retrying.")
        # Vision couldn't crack it — hand a fresh CAPTCHA back for manual entry.
        cap = self.fetch_captcha()
        if cap.get("status") == "success":
            return {"status": "needs_manual_captcha",
                    "session_id": cap["session_id"],
                    "captcha_image": cap["captcha_image"]}
        return {"status": "error",
                "message": cap.get("message", "Could not reach VTOP.")}

    # ── credential storage ─────────────────────────────────────────────────
    # OFF by default: on a shared/public deployment we don't want every user's
    # VTOP password on disk. Set VITOPIA_STORE_CREDENTIALS=1 for single-user
    # setups where silent re-login across restarts is worth it.
    STORE_CREDENTIALS = os.environ.get("VITOPIA_STORE_CREDENTIALS", "").lower() in ("1", "true", "yes")

    def store_credentials(self, reg_no: str, username: str, password: str) -> None:
        if not self.STORE_CREDENTIALS:
            return
        clean = (reg_no or username).strip().upper()
        try:
            payload = json.dumps({"username": username, "password": password})
            enc = base64.b64encode(payload.encode()).decode()
            (CREDENTIALS_DIR / f"{clean}.cred").write_text(enc)
        except Exception as e:
            logger.warning(f"Could not store credentials for {clean}: {e}")

    def get_credentials(self, reg_no: Optional[str]) -> Optional[Dict[str, str]]:
        if not reg_no:
            return None
        f = CREDENTIALS_DIR / f"{reg_no.strip().upper()}.cred"
        if not f.exists():
            return None
        try:
            return json.loads(base64.b64decode(f.read_text().strip()).decode())
        except Exception as e:
            logger.warning(f"Could not read credentials for {reg_no}: {e}")
            return None

    def _save_own_profile(self, live: LiveSession) -> None:
        self._own_profile_cache[live.register_no] = dict(live.profile)
        dst = STUDENT_DATA_DIR / f"{live.register_no}.json"
        tmp = dst.with_suffix(f".json.{os.getpid()}.tmp")
        try:
            tmp.write_text(json.dumps(live.profile, indent=2))
            os.replace(tmp, dst)  # atomic — safe under concurrent scrapes
        except Exception as e:
            logger.warning(f"Could not cache profile for {live.register_no}: {e}")
            tmp.unlink(missing_ok=True)

    # ── semester resolution ────────────────────────────────────────────────
    def _resolve_semester(self, live: LiveSession) -> None:
        candidates = [
            "academics/common/StudentTimeTable",
            "examinations/StudentMarkView",
            "academics/common/StudentAttendance",
        ]
        for path in candidates:
            try:
                res = _post(live.http, path, live.register_no, live.csrf)
            except Exception:
                continue
            if res.status_code != 200 or _looks_dead(res.text, res.url):
                continue
            soup = BeautifulSoup(res.text, "html.parser")
            sel = (soup.find("select", {"id": "semesterSubId"})
                   or soup.find("select", {"name": "semesterSubId"})
                   or soup.find("select", id=re.compile("semester", re.I)))
            if not sel:
                continue
            opts = [o for o in sel.find_all("option") if o.get("value", "").strip()]
            if not opts:
                continue
            chosen = opts[0]
            for o in opts:
                if o.get("selected") is not None:
                    chosen = o
                    break
            live.semester_id = chosen["value"].strip()
            live.semester_name = chosen.get_text(strip=True)
            logger.info(f"Resolved semester for {live.register_no}: "
                        f"{live.semester_name} ({live.semester_id})")
            return
        logger.info(f"Could not resolve semester for {live.register_no}; "
                    "scrapers will try without an explicit semesterSubId.")

    # ── module dispatcher ──────────────────────────────────────────────────
    _MODULE_PATHS = {
        "attendance": "Academics → Class Attendance",
        "timetable": "Academics → Time Table",
        "marks": "Examinations → Marks",
        "grades": "Examinations → Grade History",
        "exam_schedule": "Examinations → Exam Schedule",
        "courses": "Academics → Course Page",
        "curriculum": "Academics → My Curriculum",
        "assignments": "Examinations → Digital Assignment Upload",
        "profile": "Services → Profile",
        "proctor": "Services → Proctor Details",
        "proctor_messages": "Services → Proctor Message",
        "hod_dean": "Academics → HOD and Dean Info",
        "receipts": "Finance → Payment Receipts",
        "class_messages": "Academics → Class Messages",
        "library_dues": "Finance → Library Due",
        "fee_intimations": "Finance → Fees Intimation",
        "additional_learning": "Academics → Minor / Honour",
        "scholarships": "Services → My Scholarships",
        "biometric": "Academics → Biometric Info",
        "project_work": "Academics → Project",
    }

    _SCRAPERS = {
        "attendance": "_scrape_attendance",
        "timetable": "_scrape_timetable",
        "marks": "_scrape_marks",
        "grades": "_scrape_grade_history",
        "exam_schedule": "_scrape_exam_schedule",
        "courses": "_scrape_course_page",
        "curriculum": "_scrape_curriculum",
        "assignments": "_scrape_assignments",
        "profile": "_scrape_profile",
        "proctor": "_scrape_proctor",
        "proctor_messages": "_scrape_proctor_messages",
        "receipts": "_scrape_receipts",
        "hod_dean": "_scrape_hod_dean",
        "class_messages": "_scrape_class_messages",
        "library_dues": "_scrape_library_dues",
        "fee_intimations": "_scrape_fee_intimations",
        "additional_learning": "_scrape_additional_learning",
        "scholarships": "_scrape_scholarships",
        "biometric": "_scrape_biometric_today",
        "project_work": "_scrape_project_work",
    }

    def _fresh(self, live: LiveSession, module: str) -> Dict[str, Any]:
        c = live.module_cache.get(module)
        if c and (time.time() - c["ts"] < MODULE_TTL_SECONDS):
            return c["result"]
        return {}

    def fetch_module(self, user_id: str, module: str, force: bool = False) -> Dict[str, Any]:
        live = self.ensure_live_session(user_id)
        if not live:
            return {"status": "needs_login"}
        if not force:
            hit = self._fresh(live, module)
            if hit:
                return hit

        fn_name = self._SCRAPERS.get(module)
        if not fn_name:
            return {"status": "unavailable", "vtop_path": "Unknown module"}
        fn = getattr(self, fn_name)

        with live.lock:
            # Another thread may have populated the cache while we waited.
            if not force:
                hit = self._fresh(live, module)
                if hit:
                    return hit
            try:
                data = fn(live)
            except Exception as e:
                logger.warning(f"Scrape '{module}' raised for {live.register_no}: {e}")
                data = None

            if data is None and not live.alive:
                relive = self._silent_relogin(user_id)
                if relive:
                    try:
                        data = getattr(self, fn_name)(relive)
                        live = relive
                    except Exception:
                        data = None

            if data is None:
                return {"status": "unavailable",
                        "vtop_path": self._MODULE_PATHS.get(module, "VTOP")}

            result = {"status": "ok", "module": module, "data": data,
                      "fetched_at": time.time(), "register_no": live.register_no}
            live.module_cache[module] = {"ts": time.time(), "result": result}
            self._merge_profile(live, module, data)
            return result

    def _merge_profile(self, live: LiveSession, module: str, data: Any) -> None:
        p = live.profile
        if module == "attendance" and isinstance(data, list):
            p["attendance"] = data
        elif module == "timetable" and isinstance(data, dict):
            p["registered_courses"] = data.get("courses", p.get("registered_courses", []))
        elif module == "courses" and isinstance(data, dict) and data.get("courses"):
            p["registered_courses"] = data["courses"]
        elif module == "grades" and isinstance(data, dict):
            keys = ["grade_counts", "student_name", "program", "school", "gender", "year_joined"]
            # Don't clobber the dashboard CGPA (authoritative) with a computed one.
            if p.get("cgpa_source") != "VTOP dashboard":
                keys += ["cgpa", "cgpa_source", "total_credits_earned"]
            for k in keys:
                if data.get(k) is not None:
                    p[k] = data[k]
            if data.get("courses"):
                p["graded_courses"] = data["courses"]
        elif module == "profile" and isinstance(data, dict):
            for k, v in data.items():
                if v:
                    p[k] = v
        elif module == "proctor" and isinstance(data, dict) and data:
            p["proctor"] = data
        self._save_own_profile(live)

    # ── individual scrapers ────────────────────────────────────────────────
    # Each returns parsed data on success, or None on failure. On a dead session
    # they set live.alive = False and return None.

    # Only these markers mean the SESSION is gone (needs re-login). A 404 on a
    # single candidate endpoint must NOT kill the session — we just try the next.
    _SESSION_DEAD = ("Session Timed Out", 'id="vtopLoginForm"', "vtopLoginForm",
                     "You are logged out")

    def _guarded(self, live: LiveSession, res: requests.Response) -> Optional[BeautifulSoup]:
        text = res.text or ""
        if any(m in text for m in self._SESSION_DEAD):
            live.alive = False
            return None
        if res.status_code != 200 or "HTTP Status 404" in text or len(text) < 200:
            return None
        return BeautifulSoup(text, "html.parser")

    def _sem_body(self, live: LiveSession) -> Dict[str, str]:
        return {"semesterSubId": live.semester_id} if live.semester_id else {}

    _COURSE_CODE = re.compile(r"^[A-Z]{2,4}\d{3,4}[A-Z]?$")

    def _scrape_attendance(self, live: LiveSession) -> Optional[List[Dict[str, str]]]:
        # Columns: Sl | Class Group | Course Detail | Class Detail | Faculty |
        #          Attended | Total | Percentage | Debar | (detail link)
        res = _post(live.http, "processViewStudentAttendance", live.register_no,
                    live.csrf, self._sem_body(live))
        soup = self._guarded(live, res)
        if not soup:
            return None
        out = []
        for cols in _rows_from_html(soup, min_cols=9):
            if not cols[0].isdigit():
                continue
            out.append({
                "sl": cols[0],
                "class_group": cols[1],
                "course": cols[2],
                "class_detail": cols[3],
                "faculty": cols[4],
                "attended": cols[5],
                "total": cols[6],
                "percentage": cols[7],
                "debar": cols[8] or "-",
            })
        return out or None

    def _scrape_timetable(self, live: LiveSession) -> Optional[Dict[str, Any]]:
        # Columns: Sl | Class Group | Course | L T P J C | Category | Option |
        #          Class Id | Slot/Venue | Faculty | Reg date | Att date | Status
        res = _post(live.http, "processViewTimeTable", live.register_no,
                    live.csrf, self._sem_body(live))
        soup = self._guarded(live, res)
        if not soup:
            return None
        courses = []
        for cols in _rows_from_html(soup, min_cols=12):
            if not cols[0].isdigit():
                continue
            courses.append({
                "sl": cols[0],
                "course": cols[2],
                "credits": cols[3],
                "category": cols[4],
                "class_id": cols[6],
                "slot_venue": cols[7],
                "faculty": cols[8],
                "status": cols[11],
            })
        return {"courses": courses} if courses else None

    def _scrape_marks(self, live: LiveSession) -> Optional[List[Dict[str, Any]]]:
        try:
            live.http.get(f"{VTOP_BASE_URL}/examinations/StudentMarkView", timeout=10)
        except Exception:
            pass
        res = _post(live.http, "examinations/doStudentMarkView", live.register_no,
                    live.csrf, self._sem_body(live))
        soup = self._guarded(live, res)
        if not soup:
            return None
        marks: List[Dict[str, Any]] = []
        seen = set()
        # Iterate each <table> separately with its own `current` course, so a
        # standalone nested mark-table (no course header) is skipped rather than
        # being misattributed to the previously seen course.
        for table in soup.find_all("table"):
            current = None
            for tr in table.find_all("tr"):
                cols = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
                if not cols:
                    continue
                if (len(cols) >= 8 and cols[0].isdigit()
                        and cols[1].startswith("VL") and self._COURSE_CODE.match(cols[2] or "")):
                    current = f"{cols[2]} — {cols[3]}"
                    continue
                if (current and len(cols) >= 7 and cols[0].isdigit()
                        and not cols[1].startswith("VL")
                        and re.match(r"^\d+(\.\d+)?$", (cols[2] or "").strip())):
                    key = (current, cols[1], cols[5])
                    if key in seen:
                        continue
                    seen.add(key)
                    marks.append({
                        "course": current, "title": cols[1], "max": cols[2],
                        "weightage_pct": cols[3], "status": cols[4],
                        "scored": cols[5], "weighted": cols[6],
                    })
        return marks or None

    _GRADE_POINTS = {"S": 10, "A": 9, "B": 8, "C": 7, "D": 6, "E": 5, "F": 0}

    def _scrape_grade_history(self, live: LiveSession) -> Optional[Dict[str, Any]]:
        res = _post(live.http, "examinations/examGradeView/StudentGradeHistory",
                    live.register_no, live.csrf)
        soup = self._guarded(live, res)
        if not soup:
            return None
        full_text = soup.get_text(" ", strip=True)

        identity: Dict[str, str] = {}
        courses: List[Dict[str, str]] = []
        seen = set()
        for cols in _rows_from_html(soup, min_cols=2):
            # Identity row: RegNo | Name | Programme & Branch | Mode | System |
            #               Gender | YearJoined | EduStatus | School | Campus
            if (len(cols) >= 9 and re.match(r"^\d{2}[A-Z]{3}\d{4}$", cols[0] or "")):
                identity = {
                    "student_name": cols[1].title(),
                    "program": cols[2],
                    "gender": cols[5] if len(cols) > 5 else "",
                    "year_joined": cols[6] if len(cols) > 6 else "",
                    "school": cols[8] if len(cols) > 8 else "",
                }
                continue
            # Grade row: Sl | Code | Title | Type | Credits | Grade | ExamMonth | ResultDate | Dist | Detail
            if (len(cols) >= 8 and cols[0].isdigit()
                    and self._COURSE_CODE.match(cols[1] or "")):
                key = (cols[1], cols[6] if len(cols) > 6 else "")
                if key in seen:
                    continue
                seen.add(key)
                courses.append({
                    "code": cols[1], "title": cols[2], "type": cols[3],
                    "credits": cols[4], "grade": cols[5],
                    "exam_month": cols[6] if len(cols) > 6 else "",
                    "result_date": cols[7] if len(cols) > 7 else "",
                })

        # Printed CGPA if the page shows one; otherwise compute it.
        printed = None
        m = re.search(r"CGPA[\s:]*([0-9]{1,2}\.[0-9]{1,3})", full_text)
        if m:
            v = float(m.group(1))
            if 0 < v <= 10:
                printed = v

        tot_pts = tot_cr = earned = 0.0
        grade_counts: Dict[str, int] = {}
        for c in courses:
            try:
                cr = float(c["credits"])
            except (TypeError, ValueError):
                continue
            g = (c["grade"] or "").strip().upper()
            grade_counts[g] = grade_counts.get(g, 0) + 1
            if g in self._GRADE_POINTS:
                tot_pts += self._GRADE_POINTS[g] * cr
                tot_cr += cr
                if g != "F":
                    earned += cr
            elif g == "P":
                earned += cr
        computed = round(tot_pts / tot_cr, 2) if tot_cr else None

        if not courses and printed is None:
            return None
        return {
            **identity,
            "cgpa": printed if printed is not None else computed,
            "cgpa_source": "printed on VTOP" if printed is not None else "computed from grade history",
            "total_credits_earned": round(earned, 1) or None,
            "grade_counts": grade_counts or None,
            "courses": courses,
        }

    def _scrape_exam_schedule(self, live: LiveSession) -> Optional[List[Dict[str, str]]]:
        res = _post(live.http, "examinations/doSearchExamScheduleForStudent",
                    live.register_no, live.csrf, self._sem_body(live))
        soup = self._guarded(live, res)
        if not soup:
            return None
        exams: List[Dict[str, str]] = []
        exam_type = ""
        for cols in _rows_from_html(soup, min_cols=1):
            if len(cols) == 1:
                t = cols[0].strip()
                if re.fullmatch(r"(FAT|CAT\s*-?\s*(?:1|2|I|II)|Final Assessment Test)", t, re.I):
                    exam_type = t.upper().replace(" ", "").replace("-", "")
                continue
            # Sl | Code | Title | Type | ClassId | Slot | Date | Session |
            # Reporting | ExamTime | Venue | SeatLoc | SeatNo
            if (len(cols) >= 13 and cols[0].isdigit()
                    and self._COURSE_CODE.match(cols[1] or "")):
                exams.append({
                    "exam_type": exam_type,
                    "course_code": cols[1],
                    "course_title": cols[2],
                    "class_id": cols[4],
                    "slot": cols[5],
                    "date": cols[6],
                    "session": cols[7],
                    "reporting_time": cols[8],
                    "exam_time": cols[9],
                    "venue": cols[10],
                    "seat_location": cols[11],
                    "seat_no": cols[12],
                })
        return exams or None

    def _scrape_course_page(self, live: LiveSession) -> Optional[Dict[str, Any]]:
        # No stable tabular course-list endpoint on current VTOP; the timetable
        # scrape already yields registered courses + faculty + slot.
        tt = self._scrape_timetable(live)
        return tt if tt and tt.get("courses") else None

    def _scrape_curriculum(self, live: LiveSession) -> Optional[Dict[str, Any]]:
        res = _post(live.http, "academics/common/Curriculum", live.register_no, live.csrf)
        soup = self._guarded(live, res)
        if not soup:
            return None
        text = soup.get_text(" ", strip=True)
        total = None
        m = re.search(r"Total Credits:\s*([0-9]+(?:\.[0-9]+)?)", text)
        if m:
            total = m.group(1)
        baskets = []
        for mm in re.finditer(
            r"([A-Za-z]{2,5})\s+Credit:\s*([0-9]+(?:\.[0-9]+)?)\s+Max\.\s*Credit:\s*"
            r"([0-9]+(?:\.[0-9]+)?)\s+([A-Za-z][A-Za-z /&-]+?)"
            r"(?=\s+[A-Za-z]{2,5}\s+Credit:|\s*$)", text
        ):
            baskets.append({
                "code": mm.group(1),
                "earned": mm.group(2),
                "max": mm.group(3),
                "name": mm.group(4).strip(),
            })
        if total or baskets:
            return {"total_credits": total, "baskets": baskets}
        return None

    def _scrape_assignments(self, live: LiveSession) -> Optional[List[List[str]]]:
        # examinations/StudentDA — structure unconfirmed, so parse defensively:
        # keep only rows that carry a course code, and cap the width.
        try:
            live.http.get(f"{VTOP_BASE_URL}/examinations/StudentDA", timeout=10)
        except Exception:
            pass
        for path in self.DEBUG_ENDPOINTS["assignments"]:
            for body in (self._sem_body(live), {}):
                res = _post(live.http, path, live.register_no, live.csrf, body)
                soup = self._guarded(live, res)
                if not soup:
                    continue
                out = []
                for cols in _rows_from_html(soup, min_cols=3):
                    low0 = cols[0].lower()
                    if low0 in ("s.no", "sl.no", "course code", "course", "class number"):
                        continue
                    if any(self._COURSE_CODE.match((c or "").strip()) for c in cols[:3]):
                        out.append([c[:60] for c in cols[:6]])
                if out:
                    return out
        return None

    def _scrape_profile(self, live: LiveSession) -> Optional[Dict[str, Any]]:
        # studentsRecord/StudentProfileAllView — several key/value tables covering
        # personal info, hostel allotment and mess. Keys repeat (parent rows), so
        # the first occurrence of an ambiguous key wins.
        for path in self.DEBUG_ENDPOINTS["profile"]:
            res = _post(live.http, path, live.register_no, live.csrf)
            soup = self._guarded(live, res)
            if not soup:
                continue
            info: Dict[str, Any] = {}
            hostel: Dict[str, str] = {}
            for cols in _rows_from_html(soup, min_cols=2):
                key = re.sub(r"[\s:.]+$", "", cols[0].lower().strip())
                val = cols[1].strip()
                if not val or val == "-":
                    continue
                if key in ("student name", "name"):
                    info.setdefault("student_name", val.title())
                elif key == "date of birth":
                    info.setdefault("dob", val)
                elif key == "gender":
                    info.setdefault("gender", val.title())
                elif key == "blood group":
                    info.setdefault("blood_group", val)
                elif key in ("hosteller", "day scholar / hosteller", "residential status"):
                    info.setdefault("residential_status", val.title())
                elif key == "nationality":
                    info.setdefault("nationality", val.title())
                elif key in ("program", "programme", "branch", "programme and branch"):
                    info.setdefault("program", val)
                elif key == "school":
                    info.setdefault("school", val)
                elif key in ("block name", "hostel block", "block"):
                    hostel.setdefault("block", val)
                elif key.startswith("room no") or key == "room number":
                    hostel.setdefault("room_no", val)
                elif key == "bed type":
                    hostel.setdefault("bed_type", val)
                elif key == "mess information":
                    info.setdefault("mess", val)
            if hostel:
                info["hostel"] = hostel
            if info:
                return info
        return None

    def _scrape_proctor(self, live: LiveSession) -> Optional[Dict[str, Any]]:
        # proctor/viewProctorDetails — single key/value table.
        for path in self.DEBUG_ENDPOINTS["proctor"]:
            res = _post(live.http, path, live.register_no, live.csrf)
            soup = self._guarded(live, res)
            if not soup:
                continue
            info: Dict[str, str] = {}
            for cols in _rows_from_html(soup, min_cols=2):
                key = re.sub(r"[\s:.]+$", "", cols[0].lower().strip())
                val = cols[1].strip()
                if not val or val == "-":
                    continue
                if key == "faculty name":
                    info["name"] = val.title() if val.isupper() else val
                elif key == "faculty designation":
                    info["designation"] = val
                elif key == "school":
                    info["school"] = val
                elif key == "cabin":
                    info["cabin"] = val
                elif key == "faculty department":
                    info["department"] = val
                elif key == "faculty email":
                    info["email"] = val
                elif key == "faculty intercom":
                    info["intercom"] = val
                elif key == "faculty mobile number":
                    info["mobile"] = val
            return info or None
        return None

    def _scrape_proctor_messages(self, live: LiveSession) -> Optional[List[Dict[str, str]]]:
        for path in self.DEBUG_ENDPOINTS["proctor_messages"]:
            res = _post(live.http, path, live.register_no, live.csrf)
            soup = self._guarded(live, res)
            if soup is None:
                continue
            msgs = []
            for cols in _rows_from_html(soup, min_cols=2):
                if cols[0].lower() in ("s.no", "sl.no", "date", "message", "from date"):
                    continue
                msgs.append({"when": cols[0], "message": " ".join(cols[1:])[:400]})
            return msgs  # [] means "connected, no messages from proctor"
        return None

    def _scrape_hod_dean(self, live: LiveSession) -> Optional[Dict[str, str]]:
        # hrms/viewHodDeanDetails — key/value table (HOD + Dean names, emails, cabins).
        for path in self.DEBUG_ENDPOINTS["hod_dean"]:
            res = _post(live.http, path, live.register_no, live.csrf)
            soup = self._guarded(live, res)
            if not soup:
                continue
            info: Dict[str, str] = {}
            for cols in _rows_from_html(soup, min_cols=2):
                key = re.sub(r"[\s:.]+$", "", cols[0].lower().strip())
                val = cols[1].strip()
                if not val or val == "-":
                    continue
                if "dean" in key and "name" in key:
                    info["dean_name"] = val
                elif "dean" in key and ("email" in key or "mail" in key):
                    info["dean_email"] = val
                elif "dean" in key and "cabin" in key:
                    info["dean_cabin"] = val
                elif ("hod" in key or "head" in key) and "name" in key:
                    info["hod_name"] = val
                elif ("hod" in key or "head" in key) and ("email" in key or "mail" in key):
                    info["hod_email"] = val
                elif ("hod" in key or "head" in key) and "cabin" in key:
                    info["hod_cabin"] = val
            if info:
                return info
        return None

    def _scrape_receipts(self, live: LiveSession) -> Optional[List[Dict[str, str]]]:
        # finance/getStudentReceipts — Invoice | Receipt | Date | Amount | Campus | View
        for path in self.DEBUG_ENDPOINTS["receipts"]:
            res = _post(live.http, path, live.register_no, live.csrf)
            soup = self._guarded(live, res)
            if not soup:
                continue
            receipts = []
            for cols in _rows_from_html(soup, min_cols=4):
                if cols[0].lower() in ("invoice number", "s.no", "sl.no", "receipt no",
                                       "receipt number"):
                    continue
                receipts.append({
                    "invoice_no": cols[0],
                    "receipt_no": cols[1],
                    "date": cols[2],
                    "amount": cols[3],
                    "campus": cols[4] if len(cols) > 4 else "",
                })
            if receipts:
                return receipts
        return None

    # ── extra read-only modules (discovered via menu crawl) ───────────────
    def _kv(self, soup, wanted: Dict[str, str]) -> Dict[str, str]:
        """Pull the value cells for a set of label→key mappings from any table."""
        out: Dict[str, str] = {}
        for cols in _rows_from_html(soup, min_cols=2):
            key = re.sub(r"[\s:.]+$", "", cols[0].lower().strip())
            for label, outkey in wanted.items():
                if label in key and cols[1].strip() not in ("", "-"):
                    out.setdefault(outkey, cols[1].strip())
        return out

    def _scrape_class_messages(self, live: LiveSession) -> Optional[List[Dict[str, str]]]:
        res = _post(live.http, "academics/common/StudentClassMessage", live.register_no, live.csrf,
                    self._sem_body(live))
        soup = self._guarded(live, res)
        if not soup:
            return None
        if "no messages sent by faculty" in soup.get_text(" ", strip=True).lower():
            return []
        msgs = []
        for cols in _rows_from_html(soup, min_cols=3):
            if cols[0].lower() in ("s.no", "sl.no", "date", "sent on"):
                continue
            msgs.append({"course": cols[1] if len(cols) > 1 else "",
                         "faculty": cols[2] if len(cols) > 2 else "",
                         "message": " ".join(cols[3:])[:400] if len(cols) > 3 else cols[-1]})
        return msgs

    def _scrape_library_dues(self, live: LiveSession) -> Optional[Dict[str, str]]:
        res = _post(live.http, "finance/libraryPayments", live.register_no, live.csrf)
        soup = self._guarded(live, res)
        if not soup:
            return None
        txt = soup.get_text(" ", strip=True)
        m = re.search(r"(?:Koha\s*Due\s*Amount|Due\s*Amount|Total\s*Due)[^0-9₹]*₹?\s*([\d,]+\.?\d*)", txt, re.I)
        if m:
            return {"due_amount": m.group(1)}
        return {"due_amount": "0.00"} if "library" in txt.lower() else None

    def _scrape_fee_intimations(self, live: LiveSession) -> Optional[List[Dict[str, str]]]:
        res = _post(live.http, "finance/getStudentFeesIntimation", live.register_no, live.csrf)
        soup = self._guarded(live, res)
        if not soup:
            return None
        out = []
        for cols in _rows_from_html(soup, min_cols=4):
            if cols[0].lower() in ("s.no", "sl.no", "sl.no."):
                continue
            if not re.match(r"^\d+\.?$", cols[0].strip()):
                continue
            out.append({"year": cols[1], "term": cols[2], "description": cols[3]})
        return out or None

    def _scrape_additional_learning(self, live: LiveSession) -> Optional[List[Dict[str, str]]]:
        res = _post(live.http, "academics/additionalLearning/AdditionalLearningStudentView",
                    live.register_no, live.csrf)
        soup = self._guarded(live, res)
        if not soup:
            return None
        out = []
        for cols in _rows_from_html(soup, min_cols=3):
            low = " ".join(cols).lower()
            if "learning type" in low or cols[0].lower() in ("s.no", "sl.no"):
                continue
            if len(cols) >= 3:
                out.append({"type": cols[0], "code": cols[1], "detail": " ".join(cols[2:])[:120]})
        return out or []

    def _scrape_scholarships(self, live: LiveSession) -> Optional[List[Dict[str, str]]]:
        res = _post(live.http, "admissions/getStudentScholarshipDetails", live.register_no, live.csrf)
        soup = self._guarded(live, res)
        if not soup:
            return None
        out = []
        for cols in _rows_from_html(soup, min_cols=3):
            if cols[0].lower() in ("s.no", "sl.no") or "scholarship name" in " ".join(cols).lower():
                continue
            if re.match(r"^\d+$", cols[0].strip()):
                out.append({"name": cols[1] if len(cols) > 1 else "",
                            "source": cols[2] if len(cols) > 2 else "",
                            "received": cols[4] if len(cols) > 4 else ""})
        return out or []

    def _scrape_biometric_today(self, live: LiveSession) -> Optional[Dict[str, Any]]:
        return self.biometric_for_date(live, time.strftime("%d-%b-%Y"))

    def _scrape_project_work(self, live: LiveSession) -> Optional[List[Dict[str, str]]]:
        # academics/common/ProjectView — capstone / project-course registration
        # status. Confirmed real (its "view" confirm dialog echoes
        # "Course Id: VL_<CODE>_00100", the same token attendance_detail derives).
        for path in ("academics/common/ProjectView", "academics/common/doProjectView"):
            res = _post(live.http, path, live.register_no, live.csrf, self._sem_body(live))
            soup = self._guarded(live, res)
            if not soup:
                continue
            out = []
            for cols in _rows_from_html(soup, min_cols=3):
                low0 = cols[0].lower()
                if low0 in ("course code",) or not self._COURSE_CODE.match(cols[0].strip()):
                    continue
                out.append({"code": cols[0], "title": cols[1] if len(cols) > 1 else "",
                           "status": cols[2] if len(cols) > 2 else ""})
            if out:
                return out
        return None

    def biometric_for_date(self, live: LiveSession, date_ddmonyyyy: str) -> Optional[Dict[str, Any]]:
        res = _post(live.http, "getStudViewBioList", live.register_no, live.csrf,
                    {"fromDate": date_ddmonyyyy})
        soup = self._guarded(live, res)
        if not soup:
            return None
        txt = soup.get_text(" ", strip=True).lower()
        if "no record" in txt:
            return {"date": date_ddmonyyyy, "punches": []}
        punches = []
        for cols in _rows_from_html(soup, min_cols=2):
            if cols[0].lower() in ("s.no", "sl.no", "date", "time"):
                continue
            punches.append(" — ".join(c for c in cols if c)[:80])
        return {"date": date_ddmonyyyy, "punches": punches}

    _AT_TYPE = {"theory": "TH", "lab": "LO", "project": "PJ"}

    def attendance_detail(self, live: LiveSession, course_code: str,
                          course_type: str = "TH") -> Optional[Dict[str, Any]]:
        """Per-course day-by-day attendance log (processViewAttendanceDetail)."""
        reg_number = f"VL_{course_code.upper()}_00100"
        for body in ({"semesterSubId": live.semester_id or "",
                      "registerNumber": reg_number, "courseType": course_type},
                     {"semesterSubId": live.semester_id or "",
                      "classId": reg_number, "courseType": course_type}):
            res = _post(live.http, "processViewAttendanceDetail", live.register_no, live.csrf, body)
            soup = self._guarded(live, res)
            if not soup:
                continue
            summary = self._kv(soup, {"present": "present", "absent": "absent",
                                      "on duty": "on_duty", "attended": "attended",
                                      "total class": "total", "percentage": "percentage"})
            sessions = []
            for cols in _rows_from_html(soup, min_cols=5):
                if not re.match(r"^\d+$", cols[0].strip()):
                    continue
                sessions.append({"date": cols[1], "slot": cols[2],
                                 "when": cols[3], "status": cols[4]})
            if sessions or summary:
                return {"course_code": course_code, "summary": summary, "sessions": sessions}
        return None

    # ── live faculty search ────────────────────────────────────────────────
    def search_faculty_live(self, user_id: str, term: str, detail_limit: int = 3) -> Dict[str, Any]:
        """Two-step VTOP faculty directory search (confirmed via live menu crawl):
          1. POST hrms/EmployeeSearchForStudent {searchEmployee: term (>=3 chars)}
             -> table: Name | Designation | School/Centre | Action(button id=empId)
          2. POST hrms/EmployeeSearch1ForStudent {empId} for each match
             -> KV: Name, Designation, Department, School/Centre, E-Mail Id, Cabin Number
        Only the first `detail_limit` matches get the (slower) detail call; the
        rest come back with just name/designation/school.
        """
        live = self.ensure_live_session(user_id)
        if not live:
            return {"status": "needs_login"}
        term = term.strip()
        if len(term) < 3:
            return {"status": "unavailable", "vtop_path": "Employee search needs 3+ characters"}

        matches: List[Dict[str, str]] = []
        for path in ("hrms/EmployeeSearchForStudent", "hrms/employeeSearchForStudent"):
            try:
                res = _post(live.http, path, live.register_no, live.csrf,
                            {"searchEmployee": term})
            except Exception:
                continue
            soup = self._guarded(live, res)
            if not soup:
                continue
            for table in soup.find_all("table"):
                for tr in table.find_all("tr"):
                    tds = tr.find_all("td")
                    if len(tds) < 4:
                        continue
                    btn = tds[3].find(id=True)
                    emp_id = btn.get("id", "").strip() if btn else ""
                    if not emp_id or not emp_id.isdigit():
                        continue
                    matches.append({
                        "emp_id": emp_id,
                        "name": tds[0].get_text(" ", strip=True),
                        "designation": tds[1].get_text(" ", strip=True),
                        "school": tds[2].get_text(" ", strip=True),
                        "email": "", "department": "", "cabin": "",
                    })
            if matches:
                break
        if not matches:
            return {"status": "unavailable", "vtop_path": self._MODULE_PATHS.get("profile", "VTOP")}

        for person in matches[:detail_limit]:
            try:
                res = _post(live.http, "hrms/EmployeeSearch1ForStudent", live.register_no,
                            live.csrf, {"empId": person["emp_id"]})
            except Exception:
                continue
            soup = self._guarded(live, res)
            if not soup:
                continue
            detail = self._kv(soup, {"e-mail id": "email", "cabin number": "cabin",
                                     "name of department": "department",
                                     "school / centre name": "school",
                                     "designation": "designation"})
            person.update({k: v for k, v in detail.items() if v})

        return {"status": "ok", "data": matches}

    # ── diagnostics ────────────────────────────────────────────────────────
    DEBUG_ENDPOINTS = {
        "attendance": ["processViewStudentAttendance", "academics/common/StudentAttendance"],
        "timetable": ["processViewTimeTable", "academics/common/StudentTimeTable"],
        "marks": ["examinations/doStudentMarkView", "processStudentMark",
                  "examinations/StudentMarkView"],
        "grades": ["examinations/examGradeView/StudentGradeHistory",
                   "academics/common/StudentGradeHistory", "examinations/StudentGradeHistory",
                   "examinations/examGradeView/doStudentGradeView"],
        "exam_schedule": ["examinations/doSearchExamScheduleForStudent",
                          "examinations/doStudentExamSchedule",
                          "examinations/StudentExamSchedule",
                          "examinations/examSchedule/search"],
        "courses": ["academics/common/StudentCoursePage", "processViewStudentCourseDetail",
                    "academics/common/CoursePageConsolidated"],
        "curriculum": ["academics/common/curriculumCategoryView", "academics/common/Curriculum",
                       "processViewCurriculum"],
        "assignments": ["examinations/StudentDA", "examinations/doDigitalAssignment",
                        "examinations/processDigitalAssignmentUpload"],
        "profile": ["studentsRecord/StudentProfileAllView"],
        "proctor": ["proctor/viewProctorDetails"],
        "proctor_messages": ["proctor/viewMessagesSendByProctor"],
        "hod_dean": ["hrms/viewHodDeanDetails"],
        "receipts": ["finance/getStudentReceipts", "finance/getStudentFeesIntimation"],
        "class_messages": ["academics/common/StudentClassMessage"],
        "library_dues": ["finance/libraryPayments"],
        "fee_intimations": ["finance/getStudentFeesIntimation"],
        "additional_learning": ["academics/additionalLearning/AdditionalLearningStudentView"],
        "scholarships": ["admissions/getStudentScholarshipDetails"],
        "biometric": ["getStudViewBioList"],
        "project_work": ["academics/common/ProjectView", "academics/common/doProjectView"],
        "attendance_detail": ["processViewAttendanceDetail"],
    }

    def debug_probe(self, live: LiveSession, module: Optional[str] = None) -> Dict[str, Any]:
        """Hit candidate endpoints and report exactly what VTOP returns, so the
        parsers can be tuned to the real HTML. Never surfaced to normal users."""
        targets = ({module: self.DEBUG_ENDPOINTS[module]} if module in self.DEBUG_ENDPOINTS
                   else self.DEBUG_ENDPOINTS)
        report: Dict[str, Any] = {
            "register_no": live.register_no,
            "semester_id": live.semester_id,
            "semester_name": live.semester_name,
            "modules": {},
        }
        for mod, paths in targets.items():
            entries = []
            for path in paths:
                try:
                    res = _post(live.http, path, live.register_no, live.csrf,
                                self._sem_body(live), timeout=15)
                    text = res.text
                    soup = BeautifulSoup(text, "html.parser")
                    rows = _rows_from_html(text, min_cols=1)
                    entries.append({
                        "path": path,
                        "status_code": res.status_code,
                        "length": len(text),
                        "looks_dead": _looks_dead(text, res.url),
                        "title": (soup.title.get_text(strip=True) if soup.title else ""),
                        "num_tables": len(soup.find_all("table")),
                        "sample_rows": [[c[:40] for c in r] for r in rows[:45]],
                        "text_head": re.sub(r"\s+", " ", soup.get_text(" ", strip=True))[:400],
                    })
                except Exception as e:
                    entries.append({"path": path, "error": repr(e)})
            report["modules"][mod] = entries
        return report
