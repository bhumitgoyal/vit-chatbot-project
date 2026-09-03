"""
bot/proctor_service.py
Dynamic Proctor & Faculty Mentor Management Service for VITopia AI.
Reads directly from student's verified profile and allows live updates.
"""

from typing import Dict, Any, Optional

class ProctorService:
    def __init__(self):
        pass

    def get_proctor(self, student_profile: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not student_profile:
            return None
        return student_profile.get("proctor")

    def format_proctor_card(self, p: Optional[Dict[str, Any]]) -> str:
        if not p or not p.get("name"):
            return "ℹ️ No proctor details were retrieved. Check VTOP → My Info → Student Profile."

        lines = ["### 🧑‍🏫 Assigned Faculty Proctor / Mentor", f"- **Name:** {p['name']}"]
        for label, key in (("School / Dept", "school"), ("Cabin", "cabin"),
                           ("Intercom", "intercom"), ("Mobile", "mobile"),
                           ("Remarks", "remarks")):
            if p.get(key):
                lines.append(f"- **{label}:** {p[key]}")
        if p.get("email"):
            lines.append(f"- **Email:** [{p['email']}](mailto:{p['email']})")
        return "\n".join(lines)
