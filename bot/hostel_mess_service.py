"""
bot/hostel_mess_service.py
Unverified-fallback formatter for hostel details, used only when the live VTOP
profile page can't be fetched. Live data comes from bot/vtop_service.py.
"""

from typing import Dict, Any, Optional


class HostelMessService:
    def format_hostel_card(self, h: Optional[Dict[str, Any]]) -> str:
        if not h:
            return ("ℹ️ No hostel room allotment was retrieved (you may be a day "
                    "scholar, or VTOP didn't return it).")
        lines = ["### 🏢 Hostel Room Allotment"]
        for label, key in (("Block", "block"), ("Room No", "room_no"),
                           ("Room Type", "bed_type"), ("In-Time / Curfew", "curfew_time"),
                           ("Warden", "warden_name"), ("Mess", "mess")):
            if h.get(key):
                lines.append(f"- **{label}:** {h[key]}")
        if len(lines) == 1:
            lines.append("- _No specific fields available — check VTOP directly._")
        return "\n".join(lines)
