"""
bot/memory.py
Conversation-history store for VITopia AI.

Only chat turns are persisted (data/sessions/<user_id>.json). The live academic
profile lives exclusively on an in-memory VTOP LiveSession, so it can never be
served stale after a restart.
"""

import os
import json
import logging
from pathlib import Path
from typing import Dict, Any, List
from datetime import datetime, timezone

logger = logging.getLogger("vit.memory")

SESSIONS_DIR = Path(__file__).resolve().parent.parent / "data" / "sessions"
SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

MAX_TURNS = 30


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ConversationMemory:
    def __init__(self):
        self.sessions: Dict[str, Dict[str, Any]] = {}
        for f in SESSIONS_DIR.glob("*.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                uid = data.get("user_id") or f.stem
                self.sessions[uid] = {
                    "user_id": uid,
                    "history": data.get("history", []),
                    "created_at": data.get("created_at", _now()),
                }
            except Exception as e:
                logger.error(f"Error loading session file {f}: {e}")
        logger.info(f"Loaded {len(self.sessions)} conversation histories from disk.")

    def _get(self, user_id: str) -> Dict[str, Any]:
        s = self.sessions.get(user_id)
        if s is None:
            s = self.sessions[user_id] = {"user_id": user_id, "history": [],
                                          "created_at": _now()}
        return s

    def _persist(self, user_id: str) -> None:
        s = self.sessions.get(user_id)
        if not s:
            return
        dst = SESSIONS_DIR / f"{user_id}.json"
        tmp = dst.with_suffix(".json.tmp")
        try:
            tmp.write_text(json.dumps(s, indent=2), encoding="utf-8")
            os.replace(tmp, dst)          # atomic
        except Exception as e:
            logger.error(f"Failed to persist session {user_id}: {e}")

    def add_message(self, user_id: str, role: str, content: str) -> None:
        s = self._get(user_id)
        s["history"].append({"role": role, "content": content, "timestamp": _now()})
        if len(s["history"]) > MAX_TURNS:
            s["history"] = s["history"][-MAX_TURNS:]
        self._persist(user_id)

    def get_history(self, user_id: str, limit: int = 10) -> List[Dict[str, str]]:
        s = self.sessions.get(user_id)
        return s["history"][-limit:] if s else []

    def clear_history(self, user_id: str) -> None:
        if user_id in self.sessions:
            self.sessions[user_id]["history"] = []
            self._persist(user_id)
