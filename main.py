"""
main.py
VITopia AI — live VTOP academic & campus agent.

Principles:
  * The bot only states a student-specific fact that it just fetched live from
    VTOP for the currently connected chat user (or a clearly-labelled unverified
    reference fallback when a live fetch fails).
  * If a query needs personal VTOP data and there is no live session (and a
    silent re-login is not possible), the bot asks the user to `login` instead of
    answering from stale data.
  * It never enumerates other accounts or its own access scope.
"""

import os
import re
import time
import logging
from pathlib import Path
from collections import deque
from contextlib import asynccontextmanager
from typing import Optional, List, Dict, Any

from fastapi import FastAPI, HTTPException, Request, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

from bot.rag import RAGEngine
from bot.llm import GeminiChat
from bot.memory import ConversationMemory
from bot.vtop_service import VTOPService
from bot.faculty_service import FacultyService
from bot.hostel_mess_service import HostelMessService
from bot.proctor_service import ProctorService
from bot.timetable_mapper import format_day_schedule_block, format_full_week_schedule
from bot import telegram as tg

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
)
logger = logging.getLogger("vit.main")

STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)

# user_id -> {"username", "password", "session_id"} awaiting a CAPTCHA answer
PENDING_LOGINS: Dict[str, Dict[str, Any]] = {}

# Recently handled Telegram update_ids (Telegram re-delivers on slow ACK)
_TG_SEEN: deque = deque(maxlen=500)
TG_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")

TG_WELCOME = (
    "👋 **VITopia AI** — your VIT academic copilot on Telegram.\n\n"
    "Ask general questions (FFCS, grading, campus rules) right away.\n\n"
    "For **your own** records — attendance, marks, CGPA, timetable, exam schedule, "
    "proctor, hostel, fees — connect your VTOP account:\n"
    "`login <your-vtop-username> <your-password>`\n\n"
    "I'll read the CAPTCHA automatically; if it fails I'll send you the image to type.\n"
    "Use /logout to disconnect. Your session is per-chat and not shared."
)

LOGIN_HINT = (
    "🔐 I need your live VTOP data to answer that. Reply "
    "`login <username> <password>` and I'll pull it fresh from the portal."
)

# Unambiguously about the connected student's own VTOP records.
PERSONAL_STRONG = {
    "my ", "am i ", "do i ", "i have ", "i've ", "for me",
    "cgpa", "gpa", "debar", "hosteller", "day scholar", "backlog",
    "seat number", "seat no", "reporting time", "hall ticket",
    "registered course", "pending assignment", "pending credit",
    "fee receipt", "proctor message", "amount paid", "who am i", "whoami",
    "blood group", "date of birth",
    "biometric", "scholarship", "library due", "minor/honour",
    "class message", "message from faculty", "messages from faculty", "faculty message",
    "who is", "who teaches", "email of", "cabin of", "contact of", "'s email",
    "'s cabin", "'s office", "email", "e-mail", "mail id",
}

# Personal only when the message also carries a possessive ("my", "i", …).
PERSONAL_WEAK = {
    "attendance", "attend", "absent", "debar", "missed", "bunk", "timetable",
    "time table", "schedule", "slot", "class", "classes", "marks", "mark",
    "cat 1", "cat 2", "cat-1", "cat-2", "cat1", "cat2", "fat", "quiz", "internal",
    "scored", "assessment", "grade", "grades", "credit", "result", "curriculum",
    "basket", "exam", "seat", "seating", "hostel", "room", "block", "bed type",
    "warden", "curfew", "mess", "caterer", "meal", "proctor", "mentor",
    "assignment", "assig", "digital assignment", "submission", "due date",
    "fees", "fee", "receipt", "payment", "dues", "paid", "badge", "punch", "swipe",
    "course", "courses", "faculty", "teacher", "teachers", "professor", "prof",
    "guide", "capstone", "project", "profile", "dean", "hod", "head of department",
    "minor", "honour", "honor", "additional learning", "announcement",
    "faculty message",
}

POSSESSIVE = ("my ", " i ", "i'm ", "i am ", "am i ", "do i ", "i have", "mine",
              "for me", "i've")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Initializing VITopia AI services…")
    app.state.rag = RAGEngine()
    app.state.llm = GeminiChat()
    app.state.memory = ConversationMemory()
    app.state.vtop = VTOPService()
    app.state.faculty = FacultyService()
    app.state.hostel_mess = HostelMessService()
    app.state.proctor = ProctorService()

    if tg.enabled():
        hook = os.environ.get("TELEGRAM_WEBHOOK_URL")
        if hook and TG_SECRET:
            res = tg.set_webhook(hook, TG_SECRET)
            logger.info(f"Telegram webhook → {hook} : ok={res.get('ok')}")
        else:
            missing = [n for n in ("TELEGRAM_WEBHOOK_URL", "TELEGRAM_WEBHOOK_SECRET")
                       if not os.environ.get(n)]
            logger.warning(f"Telegram bot token set but {', '.join(missing)} missing "
                           "— webhook not auto-registered (use GET /telegram/setup).")
    yield
    logger.info("Shutting down VITopia AI.")


app = FastAPI(title="VITopia AI", version="3.0.0", lifespan=lifespan)

# No cookies are used for the app's own auth (the VTOP session is server-side,
# keyed by user_id in the request body), so credentialed CORS is unnecessary.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ChatRequest(BaseModel):
    user_id: str
    message: str
    register_no: Optional[str] = None


@app.get("/api/health")
async def health_check():
    return {
        "status": "healthy",
        "service": "VITopia AI",
        "version": "3.0.0",
        "model": os.environ.get("GEMINI_MODEL", "gemini-2.5-flash"),
        "knowledge_chunks": len(app.state.rag.chunks),
    }


# ── helpers ────────────────────────────────────────────────────────────────
def _match_credentials(msg: str) -> Optional[tuple]:
    patterns = [
        r"(?:login|signin|sign in|auth|connect)\s+(?:as\s+|to\s+)?([A-Za-z0-9_.]{3,25})\s+(\S{4,40})",
        r"user(?:name)?\s*[:=]?\s*([A-Za-z0-9_.]{3,25})\s+(?:password|pwd|pass)\s*[:=]?\s*(\S{4,40})",
    ]
    low = msg.lower()
    if not any(k in low for k in ("login", "sign in", "signin", "password", "auth", "connect")):
        return None
    for pat in patterns:
        m = re.search(pat, msg, re.IGNORECASE)
        if m:
            u, p = m.group(1).strip(), m.group(2).strip()
            if u.lower() in ("what", "check", "tell", "show", "where", "my", "the"):
                continue
            return u, p
    return None


def _looks_like_captcha(msg: str) -> bool:
    cand = msg.strip().replace(" ", "")
    if not (4 <= len(cand) <= 8):
        return False
    if not re.fullmatch(r"[A-Za-z0-9]+", cand):
        return False
    low = msg.lower()
    return not any(w in low for w in ("what", "how", "who", "when", "why", "my", "the", "is "))


def _needs_personal_data(msg_low: str) -> bool:
    """True when the query is about the connected student's own VTOP records."""
    padded = f" {msg_low} "
    if any(s in padded for s in PERSONAL_STRONG):
        return True
    if any(w in msg_low for w in PERSONAL_WEAK) and any(p in padded for p in POSSESSIVE):
        return True
    return False


def _reply(text: str, profile: Optional[Dict[str, Any]] = None,
           captcha_image: Optional[str] = None, sources: Optional[list] = None) -> Dict[str, Any]:
    return {"response": text, "student_profile": profile,
            "captcha_image": captcha_image, "sources": sources or []}


def _captcha_prompt(username: str, attempts: int, retry: bool = False) -> str:
    if retry:
        lead = "That CAPTCHA didn't match. Here's a fresh one — "
    else:
        lead = f"🔐 **VTOP CAPTCHA for `{username}`**\n\n"
    note = f" _(fetched after {attempts} refreshes)_" if attempts > 1 else ""
    return f"{lead}read the image below and reply with the code.{note}"


def _login_success_reply(vtop: VTOPService, user_id: str, result: Dict[str, Any]) -> Dict[str, Any]:
    sem = f" · {result['active_semester']}" if result.get("active_semester") else ""
    return _reply(
        f"✅ Connected to VTOP as **{result['student_name']}** "
        f"(`{result['register_no']}`){sem}. Ask about your attendance, timetable, "
        f"marks, CGPA, exam schedule, proctor, hostel, fees, and more.",
        profile=vtop.get_session(user_id).profile,
    )


def _start_login(vtop: VTOPService, user_id: str, uname: str, pwd: str) -> Dict[str, Any]:
    """Try the Vertex Vision auto-CAPTCHA solver first; fall back to asking the human."""
    result = vtop.login_with_autocaptcha(user_id, uname, pwd)
    if result.get("status") == "success":
        return _login_success_reply(vtop, user_id, result)
    if result.get("status") == "needs_manual_captcha":
        PENDING_LOGINS[user_id] = {"username": uname, "password": pwd,
                                   "session_id": result["session_id"]}
        return _reply(_captcha_prompt(uname, 1) +
                      "\n\n_(the auto-solver couldn't read it — please type the code)_",
                      captcha_image=result["captcha_image"])
    return _reply(f"❌ VTOP login failed: {result.get('message', 'unknown error')}. "
                  f"Reply `login <username> <password>` to try again.")


# ── live-module context builders ──────────────────────────────────────────
def _fallback_banner(vtop_path: str) -> str:
    return (f"> ⚠️ Couldn't fetch this live from VTOP just now. The details below are "
            f"**unverified reference data** — confirm on VTOP → *{vtop_path}*.\n\n")


def _build_dynamic_context(app_state, user_id: str, msg_low: str,
                           profile: Optional[Dict[str, Any]]) -> str:
    vtop: VTOPService = app_state.vtop
    blocks: List[str] = []

    def live(module: str):
        return vtop.fetch_module(user_id, module)

    # Timetable / day schedule
    day = next((d for d in ("today", "tomorrow", "monday", "tuesday", "wednesday",
                            "thursday", "friday", "saturday", "sunday") if d in msg_low), None)
    if any(w in msg_low for w in ("timetable", "time table", "schedule", "class", "course",
                                   "slot", "free", "faculty", "teacher", "professor", "prof ",
                                   "who teaches", "my guide")) or day:
        r = live("timetable")
        courses = (r.get("data") or {}).get("courses", []) if r.get("status") == "ok" else []
        if not courses and profile:
            courses = profile.get("registered_courses", [])
        if courses:
            if day:
                blocks.append(format_day_schedule_block(courses, day))
            elif any(w in msg_low for w in ("week", "weekly")):
                blocks.append(format_full_week_schedule(courses))
            else:
                blocks.append("### Registered Courses (live from VTOP)\n"
                              "| Course | Slot / Venue | Faculty | Status |\n| :-- | :-- | :-- | :-- |\n" +
                              "\n".join(
                                  f"| {c.get('course','')} | {c.get('slot_venue','')} | "
                                  f"{c.get('faculty','')} | {c.get('status','')} |"
                                  for c in courses if c.get("course")))
        elif r.get("status") == "unavailable":
            blocks.append(_fallback_banner(r.get("vtop_path", "VTOP")) +
                          "_Timetable not retrieved._")

    # Attendance
    if any(w in msg_low for w in ("attendance", "attend", "present", "absent", "debar", "missed", "bunk")):
        r = live("attendance")
        att_rows = r["data"] if r.get("status") == "ok" and r["data"] else []
        if att_rows:
            rows = "\n".join(
                f"| {a['course']} | {a['attended']}/{a['total']} | {a['percentage']} | {a['debar']} |"
                for a in att_rows)
            blocks.append("### Live Attendance (from VTOP)\n| Course | Attended | % | Debar |\n"
                          "| :-- | :-: | :-: | :-: |\n" + rows)
            # Day-by-day detail when the student names a specific course
            m = re.search(r"\b([a-z]{3,4}\d{3,4}[a-z]?)\b", msg_low)
            hit = None
            if m:
                code_u = m.group(1).upper()
                hit = next((a for a in att_rows if code_u in a["course"].upper()), None)
            if not hit:
                for a in att_rows:
                    words = [w for w in re.findall(r"[a-z]{4,}", a["course"].lower())
                             if w not in ("theory", "only", "lab")]
                    if words and any(w in msg_low for w in words):
                        hit = a
                        break
            if hit and app_state.vtop.get_session(user_id):
                code = re.search(r"[A-Z]{3,4}\d{3,4}[A-Z]?", hit["course"].upper())
                ctype = "LO" if "lab" in hit["course"].lower() else "TH"
                det = app_state.vtop.attendance_detail(app_state.vtop.get_session(user_id),
                                                       code.group(0) if code else "", ctype)
                if det and det.get("sessions"):
                    absent = [s for s in det["sessions"] if "absent" in s["status"].lower()]
                    lines = [f"### {hit['course']} — session log ({len(det['sessions'])} classes)"]
                    if det.get("summary"):
                        lines.append("Summary: " + ", ".join(f"{k}={v}" for k, v in det["summary"].items()))
                    lines.append(f"Missed ({len(absent)}): " +
                                 (", ".join(f"{s['date']} {s['slot']}" for s in absent[:15]) or "none"))
                    blocks.append("\n".join(lines))
        elif r.get("status") == "unavailable":
            blocks.append(_fallback_banner(r.get("vtop_path", "VTOP")) + "_Attendance not retrieved._")

    # Biometric campus-entry log
    if any(w in msg_low for w in ("biometric", "badge", "punch", "swipe", "gate entry",
                                   "campus entry", "entry log", "did i enter")):
        vt = app_state.vtop
        sess = vt.get_session(user_id)
        if sess:
            dt = time.strftime("%d-%b-%Y",
                               time.localtime(time.time() - (86400 if "yesterday" in msg_low else 0)))
            bio = vt.biometric_for_date(sess, dt)
            if bio and bio.get("punches"):
                blocks.append(f"### Biometric log — {dt}\n" +
                              "\n".join(f"• {p}" for p in bio["punches"]))
            elif bio is not None:
                blocks.append(f"_No biometric campus-entry records for {dt}._")
            else:
                blocks.append(_fallback_banner("Academics → Biometric Info") + "_Not retrieved._")

    # Messages from faculty (class messages — distinct from proctor messages)
    if any(w in msg_low for w in ("class message", "faculty message", "message from faculty",
                                   "announcement", "class announcement")):
        r = live("class_messages")
        if r.get("status") == "ok":
            if r["data"]:
                blocks.append("### Messages from Faculty (live)\n" + "\n".join(
                    f"• **{m['course']}** ({m['faculty']}): {m['message']}" for m in r["data"][:6]))
            else:
                blocks.append("_No messages sent by faculty on VTOP._")
        elif r.get("status") == "unavailable":
            blocks.append(_fallback_banner(r.get("vtop_path", "VTOP")) + "_Class messages not retrieved._")

    # Capstone / project course registration
    if any(w in msg_low for w in ("capstone", "project - i", "project-i", "my project",
                                   "project guide", "project status", "project registration")):
        r = live("project_work")
        if r.get("status") == "ok" and r["data"]:
            blocks.append("### Project / Capstone Registration (live)\n" + "\n".join(
                f"• **{x['code']}** — {x['title']}: {x['status']}" for x in r["data"][:5]))
        elif r.get("status") == "unavailable":
            blocks.append(_fallback_banner(r.get("vtop_path", "VTOP")) + "_Project status not retrieved._")

    # Minor / Honour / additional learning
    if any(w in msg_low for w in ("minor", "honour", "honor", "additional learning")):
        r = live("additional_learning")
        if r.get("status") == "ok":
            if r["data"]:
                blocks.append("### Minor / Honour (live)\n" + "\n".join(
                    f"• {x['type']} — {x['code']} — {x['detail']}" for x in r["data"][:10]))
            else:
                blocks.append("_No Minor / Honour / additional-learning registrations on VTOP._")

    # Scholarships
    if "scholarship" in msg_low:
        r = live("scholarships")
        if r.get("status") == "ok":
            if r["data"]:
                blocks.append("### Scholarships (live)\n" + "\n".join(
                    f"• **{x['name']}** — {x['source']} {('(' + x['received'] + ')') if x['received'] else ''}"
                    for x in r["data"][:8]))
            else:
                blocks.append("_No scholarship records on VTOP._")

    # Marks
    if any(w in msg_low for w in ("marks", "mark", "cat", "fat", "quiz", "scored", "internal", "assessment")):
        r = live("marks")
        if r.get("status") == "ok" and r["data"]:
            rows = "\n".join(
                f"| {m['course']} | {m['title']} | {m['scored']} / {m['max']} | {m['weighted']} | {m['status']} |"
                for m in r["data"])
            blocks.append("### Live Marks (from VTOP)\n"
                          "| Course | Component | Scored / Max | Weighted | Status |\n"
                          "| :-- | :-- | :-: | :-: | :-- |\n" + rows)
        elif r.get("status") == "unavailable":
            blocks.append(_fallback_banner(r.get("vtop_path", "VTOP")) +
                          "_Marks not retrieved — they may be unpublished._")

    # Grades / CGPA / credits
    if any(w in msg_low for w in ("grade", "cgpa", "gpa", "credit", "result", "backlog", "transcript")):
        r = live("grades")
        d = r.get("data") or {}
        p = profile or {}
        parts = []
        if p.get("cgpa") is not None:
            parts.append(f"- **CGPA:** {p['cgpa']}  _({p.get('cgpa_source', 'from VTOP')})_")
        if p.get("total_credits_earned") is not None:
            tot = f" of {p['total_credits_required']}" if p.get("total_credits_required") else ""
            parts.append(f"- **Credits earned:** {p['total_credits_earned']}{tot}")
        if d.get("grade_counts"):
            parts.append("- **Grade distribution:** " +
                         ", ".join(f"{k}×{v}" for k, v in sorted(d["grade_counts"].items()) if k))
        if d.get("courses"):
            parts.append(f"- **Courses on transcript:** {len(d['courses'])}")
        if parts:
            blocks.append("### Live Grades (from VTOP)\n" + "\n".join(parts))
        elif r.get("status") == "unavailable":
            blocks.append(_fallback_banner(r.get("vtop_path", "VTOP")) + "_Grade history not retrieved._")

    # Exam schedule / seating
    if any(w in msg_low for w in ("exam schedule", "exam seat", "seating", "seat", "hall ticket",
                                   "reporting time", "exam date", "exam time", "exam", "cat 1",
                                   "cat 2", "cat-1", "cat-2", "cat1", "cat2", "fat")):
        r = live("exam_schedule")
        if r.get("status") == "ok" and r["data"]:
            rows = "\n".join(
                f"| {e.get('exam_type','')} | {e['course_code']} | {e.get('date') or 'TBA'} "
                f"{e.get('exam_time','')} | {e.get('venue') or '-'} | {e.get('seat_no') or '-'} |"
                for e in r["data"])
            blocks.append("### Live Exam Schedule (from VTOP)\n| Type | Course | Date / Time | Venue | Seat |\n"
                          "| :-- | :-- | :-- | :-- | :-: |\n" + rows)
        elif r.get("status") == "unavailable":
            blocks.append(_fallback_banner(r.get("vtop_path", "VTOP")) +
                          "_Exam schedule not published / not retrieved._")

    # Curriculum
    if any(w in msg_low for w in ("curriculum", "basket", "pending credit", "credits required",
                                   "credit requirement")):
        r = live("curriculum")
        d = r.get("data") or {}
        if r.get("status") == "ok" and d.get("baskets"):
            rows = "\n".join(f"| {b['name']} ({b['code']}) | {b['max']} | {b['earned']} |"
                             for b in d["baskets"])
            head = f"**Total programme credits:** {d['total_credits']}\n\n" if d.get("total_credits") else ""
            blocks.append("### Live Curriculum Progress (from VTOP)\n" + head +
                          "| Basket | Required | Earned |\n| :-- | :-: | :-: |\n" + rows)
        elif r.get("status") == "unavailable":
            blocks.append(_fallback_banner(r.get("vtop_path", "VTOP")) + "_Curriculum not retrieved._")

    # Personal profile details
    if any(w in msg_low for w in ("my profile", "my details", "blood group", "my dob",
                                   "date of birth", "my email", "my mobile", "my phone",
                                   "hosteller", "day scholar", "who am i")):
        r = live("profile")
        d = r.get("data") or {}
        if r.get("status") == "ok" and d:
            lines = ["### Profile (live from VTOP)"]
            for label, k in (("Name", "student_name"), ("Date of birth", "dob"),
                             ("Gender", "gender"), ("Blood group", "blood_group"),
                             ("Nationality", "nationality"), ("Residential status", "residential_status"),
                             ("Programme", "program"), ("School", "school")):
                if d.get(k):
                    lines.append(f"- **{label}:** {d[k]}")
            blocks.append("\n".join(lines))
        elif r.get("status") == "unavailable":
            blocks.append(_fallback_banner(r.get("vtop_path", "Services → Profile")) +
                          "_Profile not retrieved._")

    # Proctor
    if any(w in msg_low for w in ("proctor", "mentor")):
        r = live("proctor")
        d = r.get("data") or {}
        if r.get("status") == "ok" and d.get("name"):
            lines = ["### Assigned Proctor (live from VTOP)", f"- **Name:** {d['name']}"]
            for label, k in (("Designation", "designation"), ("School", "school"),
                             ("Department", "department"), ("Cabin", "cabin"),
                             ("Intercom", "intercom"), ("Mobile", "mobile")):
                if d.get(k):
                    lines.append(f"- **{label}:** {d[k]}")
            if d.get("email"):
                lines.append(f"- **Email:** [{d['email']}](mailto:{d['email']})")
            blocks.append("\n".join(lines))
        elif r.get("status") == "unavailable":
            blocks.append(_fallback_banner(r.get("vtop_path", "VTOP")) +
                          app_state.proctor.format_proctor_card((profile or {}).get("proctor")))
        mr = live("proctor_messages")
        if mr.get("status") == "ok":
            if mr["data"]:
                blocks.append("### Proctor Messages (live)\n" + "\n".join(
                    f"- _{m['when']}_ — {m['message']}" for m in mr["data"][:5]))
            else:
                blocks.append("_No messages from your proctor on VTOP._")

    # Dean & HoD
    if any(w in msg_low for w in ("dean", "hod", "head of department", "head of dept")):
        r = live("hod_dean")
        d = r.get("data") or {}
        if r.get("status") == "ok" and (d.get("dean_name") or d.get("hod_name")):
            lines = ["### Dean & Head of Department (live from VTOP)"]
            if d.get("dean_name"):
                lines.append(f"- **Dean:** {d['dean_name']}"
                             + (f" · [{d['dean_email']}](mailto:{d['dean_email']})" if d.get("dean_email") else "")
                             + (f" · {d['dean_cabin']}" if d.get("dean_cabin") else ""))
            if d.get("hod_name"):
                lines.append(f"- **HoD:** {d['hod_name']}"
                             + (f" · [{d['hod_email']}](mailto:{d['hod_email']})" if d.get("hod_email") else "")
                             + (f" · {d['hod_cabin']}" if d.get("hod_cabin") else ""))
            blocks.append("\n".join(lines))
        else:
            dn = (profile or {}).get("_reference_dean") or {}
            hd = (profile or {}).get("_reference_hod") or {}
            if dn or hd:
                fb = ["### Dean & HoD"]
                if dn:
                    fb.append(f"- **Dean ({dn.get('school_name','')}):** {dn.get('dean_name','')} "
                              f"· {dn.get('email','')} · {dn.get('cabin','')}")
                if hd:
                    fb.append(f"- **HoD ({hd.get('dept_name','')}):** {hd.get('hod_name','')} "
                              f"· {hd.get('email','')} · {hd.get('cabin','')}")
                blocks.append(_fallback_banner("Academics → HOD and Dean Info") + "\n".join(fb))

    # Hostel / mess (both come from the VTOP profile page)
    if any(w in msg_low for w in ("hostel", "room", "block", "bed type", "warden", "curfew",
                                   "in-time", "mess", "caterer", "meal", "breakfast", "lunch", "dinner")):
        r = live("profile")
        d = r.get("data") or {}
        h = d.get("hostel") or {}
        if r.get("status") == "ok" and (h or d.get("mess")):
            lines = ["### Hostel & Mess (live from VTOP)"]
            if h.get("block"):
                lines.append(f"- **Block:** {h['block']}")
            if h.get("room_no"):
                lines.append(f"- **Room:** {h['room_no']}")
            if h.get("bed_type"):
                lines.append(f"- **Bed type:** {h['bed_type']}")
            if d.get("residential_status"):
                lines.append(f"- **Status:** {d['residential_status']}")
            if d.get("mess"):
                lines.append(f"- **Mess:** {d['mess']}")
            blocks.append("\n".join(lines))
        elif r.get("status") == "unavailable":
            blocks.append(_fallback_banner(r.get("vtop_path", "Services → Profile")) +
                          app_state.hostel_mess.format_hostel_card((profile or {}).get("hostel")))

    # Fees — receipts (paid) + library dues + fee-intimation letters
    if any(w in msg_low for w in ("fees", "fee", "receipt", "payment", "dues", "amount paid",
                                   "how much have i paid", "library due", "owe")):
        r = live("receipts")
        if r.get("status") == "ok" and r["data"]:
            rows = "\n".join(f"| {x['receipt_no']} | {x['date']} | ₹{x['amount']} | {x.get('invoice_no','')} |"
                             for x in r["data"][:15])
            blocks.append("### Fee Receipts (live from VTOP)\n| Receipt | Date | Amount | Invoice |\n"
                          "| :-- | :-- | --: | :-- |\n" + rows)
        elif r.get("status") == "unavailable":
            blocks.append(_fallback_banner(r.get("vtop_path", "Finance → Payment Receipts")) +
                          "_Receipts not retrieved._")
        lib = live("library_dues")
        if lib.get("status") == "ok" and lib["data"]:
            blocks.append(f"### Library Due (live)\n• **₹ {lib['data'].get('due_amount', '0.00')}**")
        if any(w in msg_low for w in ("intimation", "letter", "pending fee", "fee letter")):
            fi = live("fee_intimations")
            if fi.get("status") == "ok" and fi["data"]:
                blocks.append("### Fee Intimation Letters (live)\n" + "\n".join(
                    f"• {x['description']} — Year {x['year']}, Term {x['term']}" for x in fi["data"][:10]))

    # Digital assignments
    if any(w in msg_low for w in ("assig", "da-1", "da 1", "da1", "da-2", "da 2",
                                  "digital assignment", "submission", "due date")):
        r = live("assignments")
        if r.get("status") == "ok" and r["data"]:
            rows = "\n".join("| " + " | ".join(x) + " |" for x in r["data"][:20])
            blocks.append("### Digital Assignments (live from VTOP)\n" + rows)
        else:
            blocks.append(
                "_I couldn't pull your Digital Assignments from VTOP this time. See "
                "**VTOP → Examinations → Digital Assignment Upload** for the DA list, "
                "max marks and due dates. I don't keep a copy, so I won't guess._")

    # Faculty lookup (live VTOP "Faculty Info" search → unverified directory fallback)
    # Triggers on explicit keywords AND on bare "who is <Name>" / "<Name>'s email"
    # style questions, so just naming a faculty member is enough.
    FACULTY_STOPWORDS = {
        "faculty", "professor", "prof", "cabin", "intercom", "email", "mail",
        "who", "what", "find", "tell", "show", "give", "the", "for", "his",
        "her", "their", "sir", "maam", "mam", "doctor", "dr", "and", "email",
        "contact", "number", "office", "room", "does", "teach",
    }
    if any(w in msg_low for w in ("faculty", "professor", "prof ", "cabin", "intercom",
                                   "email of", "email", "who is", "who teaches",
                                   " sir'", " sir ", " mam'", " maam", "contact of")):
        terms = [t for t in re.findall(r"[A-Za-z]{3,}", msg_low) if t not in FACULTY_STOPWORDS]
        found = []
        for t in terms[:3]:
            fr = vtop.search_faculty_live(user_id, t)
            if fr.get("status") == "ok":
                found.extend(fr["data"])
        if found:
            seen, cards = set(), []
            for p in found:
                if p["name"] in seen:
                    continue
                seen.add(p["name"])
                line = f"- **{p['name']}** — {p.get('designation', '')} · {p.get('school', '')}"
                if p.get("email"):
                    line += f"\n  📧 {p['email']}"
                if p.get("cabin"):
                    line += f" · 🚪 {p['cabin']}"
                cards.append(line)
            blocks.append("### Faculty (live VTOP directory)\n" + "\n".join(cards[:6]))
        else:
            fb = []
            for t in terms[:3]:
                fb.extend(app_state.faculty.search_faculty(t, limit=2))
            if fb:
                seen = set()
                cards = []
                for f in fb:
                    if f["name"] in seen:
                        continue
                    seen.add(f["name"])
                    cards.append(app_state.faculty.format_faculty_card(f))
                if cards:
                    blocks.append(_fallback_banner("Academics → Faculty Info") + "\n".join(cards[:3]))

    return "\n\n".join(b for b in blocks if b)


# ── chat endpoint ─────────────────────────────────────────────────────────
@app.get("/api/debug/vtop")
def debug_vtop(user_id: str, module: Optional[str] = None):
    """Diagnostics: dump what VTOP returns for each module, to tune parsers.
    Disabled unless VITOPIA_DEBUG is set; only ever returns the caller's own
    connected session (identified by user_id)."""
    if not os.environ.get("VITOPIA_DEBUG"):
        raise HTTPException(status_code=404, detail="Not found.")
    vtop: VTOPService = app.state.vtop
    live = vtop.get_session(user_id)
    if not live:
        return {"error": "No connected VTOP session for this user_id. Log in via the chat first."}
    return vtop.debug_probe(live, module)


def process_chat(user_id: str, msg: str, surface: str = "web") -> Dict[str, Any]:
    """Core chat pipeline, shared by the web API and the Telegram webhook.
    Returns {response, student_profile, captcha_image, sources}."""
    user_id = user_id.strip()
    msg = msg.strip()
    if not msg:
        return _reply("Please type a message.")
    msg_low = msg.lower()

    vtop: VTOPService = app.state.vtop
    memory: ConversationMemory = app.state.memory
    rag: RAGEngine = app.state.rag
    llm: GeminiChat = app.state.llm

    # 1. Logout
    if msg_low in ("logout", "log out", "signout", "sign out", "disconnect", "exit"):
        vtop.logout(user_id)
        PENDING_LOGINS.pop(user_id, None)
        return _reply("👋 Logged out of VTOP. I'm now in general campus mode — ask about "
                      "FFCS, grading, or campus rules, or `login <username> <password>` to reconnect.")

    # 2. "who am i" / switch-user attempts — single account only, no enumeration
    session = vtop.get_session(user_id)
    if msg_low in ("who am i", "whoami"):
        if session:
            who = session.whoami()
            return _reply(f"You're connected as **{who['student_name']}** (`{who['register_no']}`)"
                          f"{' — ' + who['active_semester'] if who.get('active_semester') else ''}.",
                          profile=session.profile)
        return _reply("No VTOP account is connected. Reply `login <username> <password>` to connect one.")
    if any(p in msg_low for p in ("switch user", "change user", "change account", "switch account",
                                  "who are users", "list users", "switch to ", "other account")):
        return _reply("I only work with the one VTOP account you log in with in this chat. "
                      "Use `logout` then `login <username> <password>` to connect a different one.",
                      profile=session.profile if session else None)

    # 3. New login credentials in the message
    creds = _match_credentials(msg)
    if creds:
        return _start_login(vtop, user_id, creds[0], creds[1])

    # 4. Pending CAPTCHA answer
    if user_id in PENDING_LOGINS and _looks_like_captcha(msg):
        pend = PENDING_LOGINS[user_id]
        result = vtop.login_student(pend["username"], pend["password"],
                                    msg.strip().replace(" ", "").upper(),
                                    pend["session_id"], user_id=user_id)
        if result.get("status") == "success":
            PENDING_LOGINS.pop(user_id, None)
            return _login_success_reply(vtop, user_id, result)
        if result.get("captcha_wrong"):
            cap = vtop.fetch_captcha()
            if cap.get("status") == "success":
                PENDING_LOGINS[user_id]["session_id"] = cap["session_id"]
                return _reply(_captcha_prompt(pend["username"], cap.get("attempts", 1), retry=True),
                              captcha_image=cap["captcha_image"])
        PENDING_LOGINS.pop(user_id, None)
        return _reply(f"❌ VTOP login failed: {result.get('message', 'unknown error')}. "
                      f"Reply `login <username> <password>` to try again.")

    # 5. Resolve the connected student (live session only — no cached defaults)
    session = vtop.get_session(user_id)
    profile = session.profile if session else None
    personal = _needs_personal_data(msg_low)

    # 6. Personal query but no live session → try silent re-login, else ask to log in
    if personal and not session:
        relogged = vtop.ensure_live_session(user_id)
        if relogged:
            session, profile = relogged, relogged.profile
        else:
            return _reply(LOGIN_HINT)

    # 7. Build live-data context (only when authenticated)
    dynamic_context = ""
    if session and personal:
        dynamic_context = _build_dynamic_context(app.state, user_id, msg_low, profile)

    # 8. RAG + LLM
    rag_chunks = rag.query(msg)
    response_text = llm.generate_response(
        user_message=msg,
        rag_context=rag.format_for_prompt(rag_chunks),
        student_profile=profile,
        dynamic_modules_context=dynamic_context,
        conversation_history=memory.get_history(user_id),
        surface=surface,
    )
    memory.add_message(user_id, "user", msg)
    memory.add_message(user_id, "assistant", response_text)

    return _reply(response_text, profile=profile,
                  sources=[{"source": c.source, "score": c.score} for c in rag_chunks[:3]])


@app.post("/api/chat")
def chat_endpoint(payload: ChatRequest):
    if not payload.message.strip():
        raise HTTPException(status_code=400, detail="Message cannot be empty.")
    return process_chat(payload.user_id, payload.message)


# ── Telegram ──────────────────────────────────────────────────────────────
def _tg_map_command(text: str) -> Optional[str]:
    """Translate a Telegram slash-command into a chat message, or return a
    canned reply string prefixed with '!' for the handler to send verbatim."""
    if not text.startswith("/"):
        return text
    head, _, rest = text.partition(" ")
    cmd = head.lstrip("/").split("@")[0].lower()
    rest = rest.strip()
    if cmd in ("start", "help"):
        return "!" + TG_WELCOME
    if cmd == "login":
        return f"login {rest}" if rest else (
            "!Send `login <your-vtop-username> <your-password>` to connect your VTOP account.")
    if cmd in ("logout", "whoami"):
        return cmd
    return f"{cmd} {rest}".strip()


def _handle_tg_update(u: dict) -> None:
    chat_id = u["chat_id"]
    mapped = _tg_map_command(u["text"])
    if mapped.startswith("!"):
        tg.send_message(chat_id, mapped[1:])
        return
    tg.send_chat_action(chat_id)
    try:
        result = process_chat(u["user_id"], mapped, surface="telegram")
    except Exception as e:
        logger.error(f"Telegram process_chat failed: {e}", exc_info=True)
        tg.send_message(chat_id, "⚠️ Something went wrong. Please try again.")
        return
    if result.get("captcha_image"):
        tg.send_photo(chat_id, result["captcha_image"], result.get("response", ""))
    else:
        tg.send_message(chat_id, result.get("response", "…"))


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request, bg: BackgroundTasks):
    if not tg.enabled():
        raise HTTPException(status_code=404, detail="Telegram not configured.")
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != TG_SECRET:
        raise HTTPException(status_code=403, detail="Bad secret token.")
    update = await request.json()
    parsed = tg.parse_update(update)
    if not parsed:
        return {"ok": True}
    uid = parsed.get("update_id")
    if uid in _TG_SEEN:
        return {"ok": True}
    _TG_SEEN.append(uid)
    bg.add_task(_handle_tg_update, parsed)
    return {"ok": True}


@app.get("/telegram/setup")
def telegram_setup(secret: str, url: Optional[str] = None):
    """One-off: (re)register the webhook. Guarded by the shared secret."""
    if not tg.enabled():
        raise HTTPException(status_code=404, detail="Telegram not configured.")
    if secret != TG_SECRET or not TG_SECRET:
        raise HTTPException(status_code=403, detail="Bad secret.")
    target = url or os.environ.get("TELEGRAM_WEBHOOK_URL")
    if not target:
        raise HTTPException(status_code=400, detail="No url given and TELEGRAM_WEBHOOK_URL unset.")
    return tg.set_webhook(target, TG_SECRET)


@app.get("/api/chat/history/{user_id}")
async def get_history(user_id: str):
    return {"history": app.state.memory.get_history(user_id)}


@app.post("/api/chat/clear/{user_id}")
async def clear_history(user_id: str):
    app.state.memory.clear_history(user_id)
    PENDING_LOGINS.pop(user_id, None)
    return {"status": "success", "message": "Conversation history cleared."}


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def serve_index():
    index_file = STATIC_DIR / "index.html"
    if index_file.exists():
        return FileResponse(index_file)
    return {"message": "VITopia AI backend is running."}


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)
