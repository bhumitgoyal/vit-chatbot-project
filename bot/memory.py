"""
bot/memory.py
Firestore-backed conversation memory.

Schema (Firestore):
  Collection : "conversations"
  Document   : <phone_number>          ← one doc per user
    Fields:
      display_name   : str
      summary        : str             ← rolling AI-generated summary
      turn_count     : int
      last_seen      : timestamp
      recent_turns   : list[dict]      ← last N raw turns (ring buffer)
        each turn: { role: "user"|"assistant", content: str, ts: timestamp }
"""

import os
import logging
from datetime import datetime, timezone
from typing import List, Dict, Optional

from google.cloud import firestore

logger = logging.getLogger("gohappy.memory")

MAX_RECENT_TURNS  = int(os.environ.get("MAX_RECENT_TURNS", "10"))   # kept verbatim
SUMMARISE_EVERY   = int(os.environ.get("SUMMARISE_EVERY",   "6"))   # compress after N turns


class ConversationMemory:
    """
    Manages per-user conversation state in Firestore.

    Required env vars:
        GCP_PROJECT_ID   — Firestore project
        FIRESTORE_DB     — Firestore database id (default: "(default)")
    """

    def __init__(self):
        db_id = os.environ.get("FIRESTORE_DB", "(default)")
        firestore_project = os.environ.get("FIRESTORE_PROJECT_ID", os.environ["GCP_PROJECT_ID"])
        self.db = firestore.AsyncClient(
            project=firestore_project,
            database=db_id,
        )
        self._col = self.db.collection("conversations")
        self._admin_ref = self.db.collection("config").document("admins")
        logger.info("ConversationMemory connected to Firestore db='%s'", db_id)

    # ── Admin management ─────────────────────────────────────────────────────

    async def get_admin_numbers(self) -> list:
        """Return the list of dynamically added admin phone numbers from Firestore."""
        try:
            snap = await self._admin_ref.get()
            if snap.exists:
                return snap.to_dict().get("numbers", [])
        except Exception as exc:
            logger.error("Failed to fetch admin numbers: %s", exc)
        return []

    async def add_admin(self, phone: str) -> bool:
        """Add a phone number to the dynamic admin list. Returns True if added."""
        try:
            snap = await self._admin_ref.get()
            if snap.exists:
                current = snap.to_dict().get("numbers", [])
                if phone in current:
                    return False  # Already an admin
                current.append(phone)
                await self._admin_ref.update({"numbers": current})
            else:
                await self._admin_ref.set({"numbers": [phone]})
            logger.info("Added admin: %s", phone)
            return True
        except Exception as exc:
            logger.error("Failed to add admin %s: %s", phone, exc)
            return False

    async def remove_admin(self, phone: str) -> bool:
        """Remove a phone number from the dynamic admin list. Returns True if removed."""
        try:
            snap = await self._admin_ref.get()
            if not snap.exists:
                return False
            current = snap.to_dict().get("numbers", [])
            if phone not in current:
                return False
            current.remove(phone)
            await self._admin_ref.update({"numbers": current})
            logger.info("Removed admin: %s", phone)
            return True
        except Exception as exc:
            logger.error("Failed to remove admin %s: %s", phone, exc)
            return False

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _doc_ref(self, phone: str):
        return self._col.document(phone)

    @staticmethod
    def _now() -> datetime:
        return datetime.now(tz=timezone.utc)

    # ── Public API ────────────────────────────────────────────────────────────

    async def get_state(self, phone: str) -> Dict:
        """
        Return the full conversation state for a user.
        If the user is new, returns a clean default state dict.
        """
        snap = await self._doc_ref(phone).get()
        if not snap.exists:
            return {
                "display_name":  "",
                "summary":       "",
                "turn_count":    0,
                "recent_turns":  [],
                "last_seen":     None,
                "escalated_to_human": False,
            }
        return snap.to_dict()

    async def append_turn(
        self,
        phone:        str,
        display_name: str,
        user_text:    str,
        bot_text:     str,
    ) -> Dict:
        """
        Append a user+bot turn to Firestore atomically and return the updated state.
        Triggers summary compression when thresholds are hit.
        """
        ref   = self._doc_ref(phone)
        now   = self._now()
        
        turn1 = {"role": "user",      "content": user_text, "ts": now}
        turn2 = {"role": "assistant", "content": bot_text,  "ts": now}

        try:
            await ref.update({
                "display_name": display_name,
                "recent_turns": firestore.ArrayUnion([turn1, turn2]),
                "turn_count": firestore.Increment(1),
                "last_seen": now,
            })
            logger.debug("Appended turn for %s", phone)
        except Exception:
            # If doc doesn't exist yet, we create it cleanly.
            await ref.set({
                "display_name": display_name,
                "summary": "",
                "turn_count": 1,
                "recent_turns": [turn1, turn2],
                "last_seen": now,
                "escalated_to_human": False,
            })
            logger.debug("Created initial session state for %s", phone)

        # Refetch strongly consistent state for background summary checks
        return await self.get_state(phone)

    async def set_escalation_status(self, phone: str, status: bool):
        """Enable or disable the human handoff pause flag."""
        try:
            await self._doc_ref(phone).update({"escalated_to_human": status})
            logger.info("Escalation status for %s set to %s", phone, status)
        except Exception:
            pass

    async def update_summary(self, phone: str, new_summary: str):
        """Replace the stored rolling summary and trim the historical log."""
        state = await self.get_state(phone)
        turns = state.get("recent_turns", [])
        
        # Trim historical turns to ring buffer limit
        if len(turns) > MAX_RECENT_TURNS * 2:
            turns = turns[-(MAX_RECENT_TURNS * 2):]
            
        await self._doc_ref(phone).update({
            "summary": new_summary,
            "recent_turns": turns
        })
        logger.info("Updated summary and trimmed turns buffer for %s", phone)

    def should_summarise(self, state: Dict) -> bool:
        """True when a compression/summarisation cycle is due."""
        return state.get("turn_count", 0) % SUMMARISE_EVERY == 0 \
               and state.get("turn_count", 0) > 0

    def format_history_for_prompt(self, state: Dict) -> str:
        """
        Returns the recent turns formatted for LLM prompt injection.
        Oldest turns first.
        """
        turns: List[Dict] = state.get("recent_turns", [])
        if not turns:
            return "(No prior conversation history.)"

        lines = []
        for t in turns:
            label = "Customer" if t["role"] == "user" else "Support Agent"
            lines.append(f"{label}: {t['content']}")
        return "\n".join(lines)

    def build_customer_summary(self, state: Dict) -> str:
        """
        Builds the CUSTOMER_SUMMARY block for the LLM prompt.
        Combines the stored AI summary with basic profile info.
        """
        name    = state.get("display_name", "this member")
        summary = state.get("summary", "").strip()
        turns   = state.get("turn_count", 0)

        parts = [f"Member name: {name}.", f"Total turns in session so far: {turns}."]
        if summary:
            parts.append(f"Summary of prior conversation: {summary}")
        else:
            parts.append("This is the start of the conversation — no prior context.")
        return "  ".join(parts)
