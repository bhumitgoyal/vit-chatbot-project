"""
bot/reminders.py
Telegram assignment-deadline reminders, backed by Firestore so they survive
Cloud Run cold starts.

A Cloud Scheduler job pings ``POST /cron/assignment-reminders`` every few hours.
For each enrolled student this module re-logs them into VTOP, reads their
pending Digital Assignments, and pushes a Telegram nudge at fixed lead times
(3 days / 1 day / a few hours before the due date), de-duplicating so the same
assignment+window is never sent twice.

Firestore document — collection ``assignment_reminders``, id = chat ``user_id``:
    { chat_id, register_no, u (b64 username), p (b64 password),
      enabled: bool, sent: { "<course>|<title>|<date>|<window>": iso_ts } }
"""

import os
import base64
import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Iterator, List, Optional, Tuple

logger = logging.getLogger("vit.reminders")

IST = timezone(timedelta(hours=5, minutes=30))
COLLECTION = "assignment_reminders"

# (label, hours-before-deadline at which this reminder becomes due).
# Ordered tightest-first: the scan fires the closest window whose lead time
# has been reached and hasn't been sent, so the label naturally advances
# 3-day → 1-day → due-soon as the deadline approaches.
WINDOWS: List[Tuple[str, int]] = [("due-soon", 6), ("1-day", 24), ("3-day", 72)]


# ── Firestore plumbing (imported lazily so the app still boots without it) ──
def _db():
    from google.cloud import firestore
    return firestore.Client(project=os.environ.get("GCP_PROJECT_ID") or None)


def _enc(s: str) -> str:
    return base64.b64encode(s.encode()).decode()


def _dec(s: str) -> str:
    return base64.b64decode(s.encode()).decode()


# ── registry ──────────────────────────────────────────────────────────────
def enroll(user_id: str, chat_id: int, register_no: str,
           username: str, password: str) -> None:
    _db().collection(COLLECTION).document(user_id).set({
        "chat_id": chat_id,
        "register_no": register_no,
        "u": _enc(username),
        "p": _enc(password),
        "enabled": True,
        "sent": {},
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }, merge=True)


def set_enabled(user_id: str, enabled: bool) -> None:
    _db().collection(COLLECTION).document(user_id).set(
        {"enabled": bool(enabled)}, merge=True)


def get(user_id: str) -> Optional[Dict[str, Any]]:
    snap = _db().collection(COLLECTION).document(user_id).get()
    return snap.to_dict() if snap.exists else None


# ── due-date maths (pure, unit-testable) ──────────────────────────────────
def parse_due(last_date: str) -> Optional[datetime]:
    """VTOP gives a bare date (DD-MM-YYYY), no time — treat the deadline as
    23:59 IST on that day."""
    for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%d-%b-%Y", "%d %b %Y"):
        try:
            d = datetime.strptime((last_date or "").strip(), fmt)
            return d.replace(hour=23, minute=59, second=0, tzinfo=IST)
        except ValueError:
            continue
    return None


def _human_left(due: datetime, now: datetime) -> str:
    secs = (due - now).total_seconds()
    days = int(secs // 86400)
    if days >= 1:
        return f"in {days} day{'s' if days > 1 else ''}"
    hours = int(secs // 3600)
    if hours >= 1:
        return f"in {hours} hour{'s' if hours > 1 else ''}"
    return "very soon"


def due_reminders(assignments: List[Dict[str, str]], sent: Dict[str, str],
                  now: Optional[datetime] = None
                  ) -> Iterator[Tuple[str, str, Dict[str, str], datetime]]:
    """Yield (window_label, dedupe_key, assignment, due_dt) for every reminder
    that should fire now and has not already been sent."""
    now = now or datetime.now(IST)
    for a in assignments:
        due = parse_due(a.get("last_date", ""))
        if not due or due < now:
            continue
        hours_left = (due - now).total_seconds() / 3600
        for label, lead in WINDOWS:
            if hours_left <= lead:
                key = (f"{a.get('course', '')}|{a.get('title', '')}|"
                       f"{a.get('last_date', '')}|{label}")
                if key not in sent:
                    yield label, key, a, due
                break  # only the tightest window that applies


def _reminder_text(a: Dict[str, str], due: datetime, now: datetime) -> str:
    return (f"⏰ **Assignment due {_human_left(due, now)}**\n"
            f"• **{a.get('course', '')}** — {a.get('title', '')}\n"
            f"• Last date: {a.get('last_date', '')} (by end of day)\n"
            f"Upload it on VTOP → Digital Assignment Upload. "
            f"_Say “reminders off” to stop these._")


# ── the scan Cloud Scheduler triggers ────────────────────────────────────
def run_scan(vtop, tg) -> Dict[str, Any]:
    """Re-check every enrolled student and push any reminders now due."""
    try:
        db = _db()
    except Exception as e:
        logger.error(f"Reminder scan: Firestore unavailable: {e}")
        return {"ok": False, "error": "firestore_unavailable"}

    stats = {"ok": True, "users": 0, "reminders_sent": 0, "errors": 0}
    now = datetime.now(IST)

    from google.cloud.firestore_v1.base_query import FieldFilter
    query = db.collection(COLLECTION).where(filter=FieldFilter("enabled", "==", True))
    for doc in query.stream():
        rec = doc.to_dict() or {}
        user_id = doc.id
        chat_id = rec.get("chat_id")
        stats["users"] += 1
        try:
            live = vtop.get_session(user_id)
            if not live:
                res = vtop.login_with_autocaptcha(user_id, _dec(rec["u"]), _dec(rec["p"]))
                if res.get("status") != "success":
                    logger.warning(f"Reminder scan: login failed for {user_id}: "
                                   f"{res.get('message')}")
                    stats["errors"] += 1
                    continue

            mod = vtop.fetch_module(user_id, "assignments", force=True)
            assignments = mod.get("data") or [] if mod.get("status") == "ok" else []
            sent = dict(rec.get("sent", {}))
            fired = False
            for _label, key, a, due in due_reminders(assignments, sent, now=now):
                tg.send_message(chat_id, _reminder_text(a, due, now))
                sent[key] = datetime.now(timezone.utc).isoformat()
                stats["reminders_sent"] += 1
                fired = True
            if fired:
                doc.reference.set({"sent": sent}, merge=True)
        except Exception as e:
            logger.error(f"Reminder scan error for {user_id}: {e}", exc_info=True)
            stats["errors"] += 1

    return stats
