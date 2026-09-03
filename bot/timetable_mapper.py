"""
bot/timetable_mapper.py
VIT Slot-to-Day Mapping Engine.

Maps raw VIT FFCS slot codes (A1, B1, C1, ..., TA1, TG1, L47+L48, etc.)
to specific weekdays with theory/lab timings, so the LLM can answer
"What classes do I have tomorrow?" or "Am I free on Wednesday?"
"""

import re
import logging
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional

logger = logging.getLogger("vit.timetable_mapper")

# ── VIT Vellore Theory Slot → Day Mapping ────────────────────────────────
# Each theory slot repeats twice a week. This maps slot → list of (day, time).
THEORY_SLOT_SCHEDULE = {
    # Slot: [(day_name, start_time, end_time), ...]
    "A1":   [("Monday", "08:00", "08:50"), ("Wednesday", "09:00", "09:50")],
    "B1":   [("Tuesday", "08:00", "08:50"), ("Thursday", "09:00", "09:50")],
    "C1":   [("Wednesday", "08:00", "08:50"), ("Friday", "09:00", "09:50")],
    "D1":   [("Monday", "10:00", "10:50"), ("Thursday", "08:00", "08:50")],
    "E1":   [("Tuesday", "10:00", "10:50"), ("Friday", "08:00", "08:50")],
    "F1":   [("Monday", "09:00", "09:50"), ("Wednesday", "10:00", "10:50")],
    "G1":   [("Tuesday", "09:00", "09:50"), ("Thursday", "10:00", "10:50")],
    # TA slots (third hour for 3-credit courses)
    "TA1":  [("Monday", "08:00", "08:50"), ("Friday", "10:00", "10:50")],
    "TAA1": [("Tuesday", "11:00", "11:50")],
    "TB1":  [("Tuesday", "08:00", "08:50")],
    "TC1":  [("Wednesday", "08:00", "08:50")],
    "TCC1": [("Thursday", "11:00", "11:50")],
    "TD1":  [("Friday", "11:00", "11:50")],
    "TE1":  [("Tuesday", "10:00", "10:50")],
    "TF1":  [("Monday", "09:00", "09:50")],
    "TG1":  [("Monday", "11:00", "11:50"), ("Tuesday", "09:00", "09:50")],
    # Afternoon slots
    "V2":   [("Wednesday", "11:00", "11:50")],
    "V3":   [("Monday", "14:00", "14:50")],
    "V4":   [("Tuesday", "14:00", "14:50")],
    "V5":   [("Wednesday", "14:00", "14:50")],
    "V6":   [("Thursday", "14:00", "14:50")],
    "V7":   [("Friday", "14:00", "14:50")],
}

# ── Lab Slot → Day Mapping ───────────────────────────────────────────────
# Lab slots are 2-hour blocks. L1+L2 = Monday 08:00-09:40, etc.
LAB_SLOT_SCHEDULE = {
    # Monday labs
    "L1":  ("Monday", "08:00", "08:50"),
    "L2":  ("Monday", "08:51", "09:40"),
    "L3":  ("Monday", "09:51", "10:40"),
    "L4":  ("Monday", "10:41", "11:30"),
    "L5":  ("Monday", "11:40", "12:30"),
    "L6":  ("Monday", "12:31", "13:20"),
    "L31": ("Monday", "14:00", "14:50"),
    "L32": ("Monday", "14:51", "15:40"),
    # Tuesday labs
    "L7":  ("Tuesday", "08:00", "08:50"),
    "L8":  ("Tuesday", "08:51", "09:40"),
    "L9":  ("Tuesday", "09:51", "10:40"),
    "L10": ("Tuesday", "10:41", "11:30"),
    "L11": ("Tuesday", "11:40", "12:30"),
    "L12": ("Tuesday", "12:31", "13:20"),
    "L37": ("Tuesday", "14:00", "14:50"),
    "L38": ("Tuesday", "14:51", "15:40"),
    # Wednesday labs
    "L13": ("Wednesday", "08:00", "08:50"),
    "L14": ("Wednesday", "08:51", "09:40"),
    "L15": ("Wednesday", "09:51", "10:40"),
    "L16": ("Wednesday", "10:41", "11:30"),
    "L17": ("Wednesday", "11:40", "12:30"),
    "L18": ("Wednesday", "12:31", "13:20"),
    "L43": ("Wednesday", "14:00", "14:50"),
    "L44": ("Wednesday", "14:51", "15:40"),
    # Thursday labs
    "L19": ("Thursday", "08:00", "08:50"),
    "L20": ("Thursday", "08:51", "09:40"),
    "L21": ("Thursday", "09:51", "10:40"),
    "L22": ("Thursday", "10:41", "11:30"),
    "L23": ("Thursday", "11:40", "12:30"),
    "L24": ("Thursday", "12:31", "13:20"),
    "L49": ("Thursday", "14:00", "14:50"),
    "L50": ("Thursday", "14:51", "15:40"),
    # Friday labs
    "L25": ("Friday", "08:00", "08:50"),
    "L26": ("Friday", "08:51", "09:40"),
    "L27": ("Friday", "09:51", "10:40"),
    "L28": ("Friday", "10:41", "11:30"),
    "L29": ("Friday", "11:40", "12:30"),
    "L30": ("Friday", "12:31", "13:20"),
    "L55": ("Friday", "14:00", "14:50"),
    "L56": ("Friday", "14:51", "15:40"),
    # Saturday labs
    "L71": ("Saturday", "08:00", "08:50"),
    "L72": ("Saturday", "08:51", "09:40"),
    "L73": ("Saturday", "09:51", "10:40"),
    "L74": ("Saturday", "10:41", "11:30"),
    "L75": ("Saturday", "11:40", "12:30"),
    "L76": ("Saturday", "12:31", "13:20"),
    # Combined lab pairs
    "L1+L2":   ("Monday", "08:00", "09:40"),
    "L3+L4":   ("Monday", "09:51", "11:30"),
    "L5+L6":   ("Monday", "11:40", "13:20"),
    "L7+L8":   ("Tuesday", "08:00", "09:40"),
    "L9+L10":  ("Tuesday", "09:51", "11:30"),
    "L11+L12": ("Tuesday", "11:40", "13:20"),
    "L13+L14": ("Wednesday", "08:00", "09:40"),
    "L15+L16": ("Wednesday", "09:51", "11:30"),
    "L17+L18": ("Wednesday", "11:40", "13:20"),
    "L19+L20": ("Thursday", "08:00", "09:40"),
    "L21+L22": ("Thursday", "09:51", "11:30"),
    "L23+L24": ("Thursday", "11:40", "13:20"),
    "L25+L26": ("Friday", "08:00", "09:40"),
    "L27+L28": ("Friday", "09:51", "11:30"),
    "L29+L30": ("Friday", "11:40", "13:20"),
    "L31+L32": ("Monday", "14:00", "15:40"),
    "L37+L38": ("Tuesday", "14:00", "15:40"),
    "L41+L42": ("Thursday", "08:00", "09:40"),
    "L43+L44": ("Wednesday", "14:00", "15:40"),
    "L47+L48": ("Friday", "08:00", "09:40"),
    "L49+L50": ("Thursday", "14:00", "15:40"),
    "L55+L56": ("Friday", "14:00", "15:40"),
}


def parse_slot_codes(slot_venue_str: str) -> List[str]:
    """
    Parse a slot_venue string like 'A1+TA1 -SJT617' into slot codes ['A1', 'TA1']
    and venue 'SJT617'.
    """
    if not slot_venue_str:
        return []
    # Split on ' -' to separate slots from venue
    parts = slot_venue_str.split(" -")
    slot_part = parts[0].strip() if parts else slot_venue_str
    # Split on '+' to get individual slots
    slots = [s.strip() for s in slot_part.split("+") if s.strip()]
    return slots


def extract_venue(slot_venue_str: str) -> str:
    """Extract venue from 'A1+TA1 -SJT617' -> 'SJT617'"""
    if not slot_venue_str:
        return "TBD"
    parts = slot_venue_str.split(" -")
    return parts[1].strip() if len(parts) > 1 else "TBD"


def build_day_schedule(registered_courses: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, str]]]:
    """
    Given a list of registered courses (each with 'course', 'slot_venue', 'faculty'),
    build a day-wise schedule:
    {
        "Monday": [{"time": "08:00-08:50", "course": "BCSE320L", "title": "Web App Security", "venue": "SJT617", "faculty": "MEENAKSHI S P", "type": "Theory"}, ...],
        "Tuesday": [...],
        ...
    }
    """
    days = {"Monday": [], "Tuesday": [], "Wednesday": [], "Thursday": [], "Friday": [], "Saturday": [], "Sunday": []}

    for course_entry in registered_courses:
        sl = str(course_entry.get("sl", ""))
        # Skip non-course rows (timetable grid rows, time headers, etc.)
        if not sl.isdigit():
            continue

        course_full = course_entry.get("course", "")
        slot_venue = course_entry.get("slot_venue", "")
        faculty_raw = course_entry.get("faculty", "")

        if (not course_full or not slot_venue
                or "NIL" in slot_venue.upper() or slot_venue.strip() in ("-", "")):
            continue

        # Parse course code and title
        course_code = course_full.split(" - ")[0].strip() if " - " in course_full else course_full.split("(")[0].strip()
        course_title = course_full.split(" - ")[1].strip() if " - " in course_full else course_full

        # Clean up title (remove "( Theory Only )" etc.)
        course_title = re.sub(r"\(\s*(Theory Only|Lab Only|Embedded Lab|Project|ETL|Soft Skill)\s*\)", "", course_title).strip()

        # Determine type
        is_lab = "Lab" in course_full or course_code.endswith("P")
        course_type = "Lab" if is_lab else "Theory"

        # Extract venue
        venue = extract_venue(slot_venue)

        # Clean faculty name
        faculty = faculty_raw.split(" -")[0].strip() if " -" in faculty_raw else faculty_raw.strip()

        # Parse slot codes
        slot_codes = parse_slot_codes(slot_venue)

        # Check combined lab slots first (e.g., "L47+L48")
        combined_slot = "+".join(slot_codes)
        if combined_slot in LAB_SLOT_SCHEDULE:
            day, start, end = LAB_SLOT_SCHEDULE[combined_slot]
            days[day].append({
                "time": f"{start} - {end}",
                "course": course_code,
                "title": course_title,
                "venue": venue,
                "faculty": faculty,
                "type": course_type
            })
            continue

        # Map individual slots
        for slot in slot_codes:
            slot_upper = slot.upper().strip()

            # Check theory slots
            if slot_upper in THEORY_SLOT_SCHEDULE:
                for day, start, end in THEORY_SLOT_SCHEDULE[slot_upper]:
                    days[day].append({
                        "time": f"{start} - {end}",
                        "course": course_code,
                        "title": course_title,
                        "venue": venue,
                        "faculty": faculty,
                        "type": course_type
                    })

            # Check lab slots
            elif slot_upper in LAB_SLOT_SCHEDULE:
                day, start, end = LAB_SLOT_SCHEDULE[slot_upper]
                days[day].append({
                    "time": f"{start} - {end}",
                    "course": course_code,
                    "title": course_title,
                    "venue": venue,
                    "faculty": faculty,
                    "type": course_type
                })

    # Sort each day by start time and deduplicate
    for day in days:
        days[day].sort(key=lambda x: x["time"])
        # Remove duplicates (same course at same time on same day)
        seen = set()
        deduped = []
        for cls in days[day]:
            key = (cls["course"], cls["time"])
            if key not in seen:
                seen.add(key)
                deduped.append(cls)
        days[day] = deduped

    return days


def get_schedule_for_date(
    registered_courses: List[Dict[str, Any]],
    target_date: Optional[datetime] = None
) -> Dict[str, Any]:
    """
    Get the schedule for a specific date. If target_date is None, uses today.
    Returns:
    {
        "date": "Saturday, August 30, 2026",
        "day": "Saturday",
        "classes": [...],
        "is_free": True/False
    }
    """
    if target_date is None:
        target_date = datetime.now()

    day_name = target_date.strftime("%A")
    date_str = target_date.strftime("%A, %B %d, %Y")

    full_schedule = build_day_schedule(registered_courses)
    classes = full_schedule.get(day_name, [])

    return {
        "date": date_str,
        "day": day_name,
        "classes": classes,
        "is_free": len(classes) == 0
    }


def format_day_schedule_block(
    registered_courses: List[Dict[str, Any]],
    target_label: str = "today"
) -> str:
    """
    Generate a formatted markdown block for today's or tomorrow's schedule.
    target_label: 'today', 'tomorrow', 'monday', 'tuesday', etc.
    """
    now = datetime.now()
    target_label_lower = target_label.lower().strip()

    day_map = {
        "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
        "friday": 4, "saturday": 5, "sunday": 6
    }

    if target_label_lower == "today":
        target_date = now
    elif target_label_lower == "tomorrow":
        target_date = now + timedelta(days=1)
    elif target_label_lower in day_map:
        target_weekday = day_map[target_label_lower]
        current_weekday = now.weekday()
        days_ahead = (target_weekday - current_weekday) % 7
        if days_ahead == 0:
            days_ahead = 7  # Next occurrence
        target_date = now + timedelta(days=days_ahead)
    else:
        target_date = now

    result = get_schedule_for_date(registered_courses, target_date)

    if result["is_free"]:
        return (
            f"### 📅 Schedule for {result['date']}\n"
            f"**No classes scheduled for {result['day']}!** You have a free day. 🎉"
        )

    lines = [f"### 📅 Schedule for {result['date']} ({result['day']})"]
    lines.append("")
    lines.append("| Time | Course | Title | Venue | Faculty | Type |")
    lines.append("|:-----|:-------|:------|:------|:--------|:-----|")

    for cls in result["classes"]:
        lines.append(
            f"| {cls['time']} | `{cls['course']}` | {cls['title']} | "
            f"`{cls['venue']}` | {cls['faculty']} | {cls['type']} |"
        )

    lines.append(f"\n**Total classes: {len(result['classes'])}**")
    return "\n".join(lines)


def format_full_week_schedule(registered_courses: List[Dict[str, Any]]) -> str:
    """Generate a formatted full week schedule."""
    full_schedule = build_day_schedule(registered_courses)
    blocks = ["### 📅 Full Week Schedule"]

    for day in ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]:
        classes = full_schedule[day]
        if not classes:
            blocks.append(f"\n**{day}:** No classes (Free day)")
            continue

        blocks.append(f"\n**{day}:**")
        for cls in classes:
            blocks.append(
                f"  - `{cls['time']}` — **{cls['course']}** {cls['title']} "
                f"@ `{cls['venue']}` ({cls['faculty']}) [{cls['type']}]"
            )

    return "\n".join(blocks)
