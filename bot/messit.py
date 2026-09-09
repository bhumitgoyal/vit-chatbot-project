"""
bot/messit.py
Live VIT hostel mess menu, from VinnovateIT's MessIT feed.

MessIT publishes one static JSON per (hostel type, mess type):

    https://messit.vinnovateit.com/menu-data/hostel-{H}-mess-{M}.json
        H: 1 = Men's Hostel (MH), 2 = Ladies' Hostel (LH)
        M: 1 = Special Mess, 2 = Veg Mess, 3 = Non-Veg Mess

    { "hostel": H, "mess": M,
      "menu": [ { "date": "YYYY-MM-DD",
                  "menu": [ { "type": T, "menu": "comma, separated, items" } ] } ] }
        type: 1 Breakfast, 2 Lunch, 3 Snacks, 4 Dinner

The whole calendar month sits in one file and it's refreshed monthly, so a
one-hour cache is plenty.
"""

import time
import logging
from datetime import date as date_cls

import requests

logger = logging.getLogger("vit.messit")

_URL = "https://messit.vinnovateit.com/menu-data/hostel-{h}-mess-{m}.json"
_TTL = 3600
_CACHE: dict = {}  # (h, m) -> {"ts": float, "data": dict}

HOSTEL_NAMES = {1: "Men's Hostel (MH)", 2: "Ladies' Hostel (LH)"}
MESS_NAMES = {1: "Special Mess", 2: "Veg Mess", 3: "Non-Veg Mess"}
MEALS = {1: "Breakfast", 2: "Lunch", 3: "Snacks", 4: "Dinner"}
MEAL_TYPE_BY_NAME = {"breakfast": 1, "lunch": 2, "snacks": 3, "snack": 3,
                     "evening snacks": 3, "dinner": 4, "supper": 4}


def resolve_hostel(text: str, default: int = 1) -> int:
    t = f" {(text or '').lower()} "
    if any(w in t for w in ("ladies", "women", "woman", "girls", "girl", "female",
                            " lh ", "lh-", "lhc")):
        return 2
    if any(w in t for w in ("mens", "men's", " men ", "boys", "boy", "male",
                            " mh ", "mh-", "gents")):
        return 1
    return default


def resolve_mess(text: str, default: int = 1) -> int:
    t = (text or "").lower()
    if "non-veg" in t or "non veg" in t or "nonveg" in t:
        return 3
    if "special" in t:
        return 1
    if "veg" in t:
        return 2
    return default


def mess_from_profile_string(mess_str: str) -> int:
    """VTOP profile 'Mess Information' looks like
    'Special Mess - MAX-S-... [T BLOCK]' → 1/2/3."""
    s = (mess_str or "").lower()
    if "non-veg" in s or "non veg" in s:
        return 3
    if "veg mess" in s:
        return 2
    return 1  # Special is the common default


def hostel_from_gender(gender: str) -> int:
    return 2 if (gender or "").strip().lower().startswith("f") else 1


def _timing(meal_type: int, d: date_cls) -> str:
    weekend = d.weekday() >= 5  # Sat=5, Sun=6
    return {
        1: "7:30 AM - 9:30 AM" if weekend else "7:00 AM - 9:00 AM",
        2: "12:30 PM - 2:30 PM",
        3: "4:30 PM - 6:15 PM",
        4: "7:00 PM - 9:00 PM",
    }.get(meal_type, "")


def _fetch(h: int, m: int):
    key = (h, m)
    cached = _CACHE.get(key)
    if cached and time.time() - cached["ts"] < _TTL:
        return cached["data"]
    try:
        r = requests.get(_URL.format(h=h, m=m), timeout=10)
        r.raise_for_status()
        data = r.json()
        if data and data.get("menu"):
            _CACHE[key] = {"ts": time.time(), "data": data}
            return data
    except Exception as e:
        logger.warning(f"MessIT fetch hostel-{h}-mess-{m} failed: {e}")
    return cached["data"] if cached else None


def menu_for(h: int, m: int, target: date_cls, only_meal: int = 0):
    """Return the day's meals for one hostel/mess, or None if unavailable.
    `only_meal` (1–4) narrows to a single meal."""
    data = _fetch(h, m)
    if not data or not data.get("menu"):
        return None
    iso = target.isoformat()
    day = next((x for x in data["menu"] if x.get("date") == iso), None)
    if not day or not day.get("menu"):
        return None
    meals = []
    for item in sorted(day["menu"], key=lambda x: x.get("type", 9)):
        t = item.get("type")
        if only_meal and t != only_meal:
            continue
        if not (item.get("menu") or "").strip():
            continue
        meals.append({"meal": MEALS.get(t, "Meal"),
                      "timing": _timing(t, target),
                      "items": item["menu"].strip()})
    if not meals:
        return None
    return {"hostel_name": HOSTEL_NAMES.get(h, f"Hostel {h}"),
            "mess_name": MESS_NAMES.get(m, f"Mess {m}"),
            "date": iso, "meals": meals}
