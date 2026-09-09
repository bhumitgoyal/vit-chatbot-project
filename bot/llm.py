"""
bot/llm.py
VITopia AI Agent — Powered by Google Vertex AI (Gemini 2.5 Flash).
Native Google Cloud IAM & Service Account authentication via google.auth.
"""

import os
import json
import logging
import requests
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional

import google.auth
from google.auth.transport.requests import Request as GoogleAuthRequest

logger = logging.getLogger("vit.llm")


def _loads_lenient(text: str):
    """json.loads with a few cheap repairs for LLM output: strip ``` fences,
    drop trailing commas, and if it was cut off mid-object, close it up to the
    last complete top-level entry."""
    import re as _re
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`")
        t = t[4:].strip() if t[:4].lower() == "json" else t.strip()
    start = t.find("{")
    if start > 0:
        t = t[start:]
    for attempt in (t,
                    _re.sub(r",\s*([}\]])", r"\1", t),
                    _re.sub(r",\s*([}\]])", r"\1", t[:t.rfind("}") + 1]) if "}" in t else t):
        try:
            return json.loads(attempt)
        except Exception:
            continue
    # last resort: keep only whole `"Key": { ... }` blocks and wrap them
    blocks = _re.findall(r'"[^"]+"\s*:\s*\{[^{}]*\}', t)
    if blocks:
        try:
            return json.loads("{" + ",".join(blocks) + "}")
        except Exception:
            pass
    return {}

VIT_SYSTEM_PROMPT = """You are VITopia AI — a live, context-aware academic & campus assistant for students of Vellore Institute of Technology.

What you can help with:
- The connected student's own VTOP records: attendance, timetable, marks, grade history / CGPA / credits, exam schedule & seating, registered courses, curriculum progress, proctor, hostel & mess, fee receipts, digital assignments.
- General VIT regulations from the knowledge base: FFCS, relative grading (S=10, A=9, B=8, C=7, D=6, E=5, F=0), the 75% attendance rule and the 9.0+ CGPA attendance waiver, and campus rules.

Strict data-integrity rules — follow these exactly:
1. Only state a student-specific fact (a name, mark, percentage, CGPA, email, cabin, room, date, seat, amount, etc.) if it appears in the [LIVE VTOP DATA] block below, or in a block explicitly labelled as unverified reference data. Never invent or guess these values.
2. If a block is labelled "unverified reference data", repeat that caveat to the student and tell them to confirm it on VTOP.
3. If the student asks for a personal fact that is NOT in the context, say plainly that it wasn't retrieved this time and point them to the relevant VTOP menu path. Do not fill the gap with a plausible-sounding value.
4. Never describe your own data access, credentials, sessions, scraping, or any account other than the one currently connected. Answer only about the connected student.
5. If no VTOP account is connected and the question needs personal data, tell the student to reply `login <username> <password>`.

Formatting — every reply must be scannable, never a wall of text:
- Lead with the direct answer in the first line. No preamble, no "Certainly!".
- Group into short sections: a **bold label**, then one-line bullets ("• ").
- One fact per line as **Label:** value.
- Keep it tight — a typical answer is 3–8 short lines. Only go long when the student explicitly asks for a full list.
- Close with at most one short next-step line, and only if it genuinely helps.
"""

WEB_FORMAT = ("Surface: web. Markdown renders — use a compact table for multi-row "
              "data (marks, courses, receipts, exam rows) and **bold** for figures.")
TELEGRAM_FORMAT = ("Surface: Telegram. Markdown tables do NOT render — for multi-row "
                   "data use ONE bullet per row, e.g. `• BCSE320L — 6/22 (28%)`. "
                   "No `#` headings; use **bold** labels only. Emojis sparingly. "
                   "Keep the whole reply under ~1600 characters.")


class GeminiChat:
    def __init__(self):
        self.project_id = os.environ.get("GCP_PROJECT_ID", "nuveroai")
        self.location = os.environ.get("GCP_LOCATION", "us-central1")
        self.model_name = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
        self.credentials = None
        self._init_auth()

    def _init_auth(self):
        try:
            self.credentials, proj = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )
            if proj:
                self.project_id = proj
            logger.info(f"Google Cloud Auth initialized for project: {self.project_id}")
        except Exception as e:
            logger.warning(f"Could not initialize google.auth: {e}")

    def _get_auth_header(self) -> Dict[str, str]:
        if not self.credentials:
            self._init_auth()

        if self.credentials:
            try:
                if not self.credentials.valid:
                    self.credentials.refresh(GoogleAuthRequest())
                return {
                    "Authorization": f"Bearer {self.credentials.token}",
                    "Content-Type": "application/json"
                }
            except Exception as e:
                logger.error(f"Error refreshing Google Auth token: {e}")

        # Fallback to API Key if present
        api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if api_key:
            return {"Content-Type": "application/json"}

        return {"Content-Type": "application/json"}

    def generate_response(
        self,
        user_message: str,
        rag_context: str = "",
        student_profile: Optional[Dict[str, Any]] = None,
        dynamic_modules_context: str = "",
        conversation_history: Optional[List[Dict[str, str]]] = None,
        surface: str = "web",
    ) -> str:
        student_context = self._format_student_context(student_profile)

        # Inject current date/time so the LLM knows what day it is
        now = datetime.now()
        date_context = (
            f"Current Date: {now.strftime('%A, %B %d, %Y')}\n"
            f"Current Time: {now.strftime('%I:%M %p IST')}\n"
            f"Day of Week: {now.strftime('%A')}\n"
            f"Tomorrow: {(now + timedelta(days=1)).strftime('%A, %B %d, %Y')}"
        )

        fmt = TELEGRAM_FORMAT if surface == "telegram" else WEB_FORMAT
        system_instruction = (
            f"{VIT_SYSTEM_PROMPT}\n\n"
            f"[OUTPUT TARGET]\n{fmt}\n\n"
            f"[CURRENT DATE & TIME]\n{date_context}\n\n"
            f"[STUDENT ACADEMIC RECORD]\n{student_context}\n\n"
            f"[LIVE VTOP MODULES & CAMPUS DATA]\n{dynamic_modules_context}\n\n"
            f"[CAMPUS KNOWLEDGE BASE (RAG CHUNKS)]\n{rag_context}"
        )

        contents = []
        if conversation_history:
            for turn in conversation_history[-6:]:
                role = "user" if turn.get("role") == "user" else "model"
                contents.append({
                    "role": role,
                    "parts": [{"text": turn.get("content", "")}]
                })

        contents.append({
            "role": "user",
            "parts": [{"text": f"[CONTEXT FOR THIS REQUEST]\n{system_instruction}\n\n[STUDENT QUERY]\n{user_message}"}]
        })

        headers = self._get_auth_header()
        url = f"https://{self.location}-aiplatform.googleapis.com/v1/projects/{self.project_id}/locations/{self.location}/publishers/google/models/{self.model_name}:generateContent"
        
        payload = {
            "contents": contents,
            "generationConfig": {
                "temperature": 0.3,
                "maxOutputTokens": 2048,
                "topP": 0.9,
            },
        }

        try:
            res = requests.post(url, headers=headers, json=payload, timeout=40)
            if res.status_code == 200:
                data = res.json()
                candidates = data.get("candidates", [])
                if candidates and "content" in candidates[0]:
                    parts = candidates[0]["content"].get("parts", [])
                    text = "".join(p.get("text", "") for p in parts).strip()
                    if text:
                        logger.info("Vertex AI response OK.")
                        return text
                    logger.warning(f"Vertex AI empty parts "
                                   f"(finishReason={candidates[0].get('finishReason')}).")
            else:
                logger.error(f"Vertex AI API error {res.status_code}: {res.text[:300]}")
        except Exception as e:
            logger.error(f"Vertex AI generation exception: {e}")

        # Graceful degradation: if we already have live VTOP data, show it as-is;
        # otherwise say the language model is unavailable.
        if dynamic_modules_context.strip():
            return ("_(AI summary unavailable right now — here's the raw data from VTOP.)_\n\n"
                    + dynamic_modules_context)
        return ("⚠️ I couldn't reach the language model just now. Please try again in a moment. "
                "For your own records, `login <username> <password>` and ask about attendance, "
                "marks, CGPA, timetable, exam schedule, proctor, hostel or fees.")

    def generate_study_plan(self, weak_courses: List[Dict[str, Any]]) -> str:
        """Grounded study plan for the courses the student is weak in. Uses the
        Gemini `google_search` tool so the resource links are real and current;
        retries once without the tool if grounding isn't available."""
        if not weak_courses:
            return ""

        listing = "\n".join(
            f"- {c['course']} — current weighted standing {c['standing']}%"
            for c in weak_courses
        )
        prompt = (
            "You are an academic coach for a student at VIT (Vellore Institute of "
            "Technology). The student is currently BELOW 70% weighted standing in "
            "these courses:\n"
            f"{listing}\n\n"
            "For EACH course, produce this Markdown and nothing else:\n"
            "### <course code and name>\n"
            "**Likely weak areas** — 2–4 bullets naming the specific topics that "
            "usually cost VIT students marks in this subject.\n"
            "**2-week recovery plan** — a compact day-by-day list (~1–1.5 hrs/day) "
            "with concrete topics per day.\n"
            "**Free resources** — 4–6 specific, currently-working links: YouTube "
            "playlists, NPTEL / SWAYAM courses, official documentation, "
            "GeeksforGeeks, or MIT OpenCourseWare. Use Google Search to confirm "
            "each link resolves; give the real page title and full URL.\n\n"
            "Be terse and practical. No motivational paragraphs, no preamble."
        )

        contents = [{"role": "user", "parts": [{"text": prompt}]}]
        url = (f"https://{self.location}-aiplatform.googleapis.com/v1/projects/"
               f"{self.project_id}/locations/{self.location}/publishers/google/"
               f"models/{self.model_name}:generateContent")
        base = {
            "contents": contents,
            "generationConfig": {"temperature": 0.4, "maxOutputTokens": 4096},
        }
        for payload in ({**base, "tools": [{"googleSearch": {}}]}, base):
            try:
                res = requests.post(url, headers=self._get_auth_header(),
                                    json=payload, timeout=90)
                if res.status_code == 200:
                    cands = res.json().get("candidates", [])
                    if cands and "content" in cands[0]:
                        parts = cands[0]["content"].get("parts", [])
                        text = "".join(p.get("text", "") for p in parts).strip()
                        if text:
                            return text
                    logger.warning("Study-plan: empty parts "
                                   f"(finishReason={cands[0].get('finishReason') if cands else '?'}).")
                else:
                    logger.error(f"Study-plan API {res.status_code}: {res.text[:300]}")
            except Exception as e:
                logger.error(f"Study-plan generation exception: {e}")
        return ""

    def estimate_meal_nutrition(self, meals: List[Dict[str, str]]) -> Dict[str, Any]:
        """Rough per-serving nutrition for each mess meal, estimated by the model.
        `meals` = [{"meal": "Breakfast", "items": "dish, dish, ..."}, ...].
        Returns {meal_name: {calories, protein_g, carbs_g, fat_g, fiber_g, note}}
        — empty dict if the estimate can't be produced/parsed."""
        meals = [m for m in meals if m.get("items")]
        if not meals:
            return {}
        names = [m["meal"] for m in meals]
        listing = "\n".join(f'{m["meal"]} — items: {m["items"]}' for m in meals)
        prompt = (
            "Estimate the nutrition of ONE realistic single serving of each meal below "
            "— a normal plate a student actually takes in an Indian college hostel mess "
            "(the mains plus a couple of sides, NOT every listed item, NOT unlimited "
            "quantity). Indian food.\n\n"
            "Output ONLY this JSON shape and nothing else:\n"
            '{"<MealName>": {"calories": <int kcal>, "protein_g": <int>, '
            '"carbs_g": <int>, "fat_g": <int>, "fiber_g": <int>, "note": "<=12 words"}}\n\n'
            f"Use EXACTLY these strings as the top-level keys: {', '.join(names)}. "
            "Each value is ONE object of totals for that whole plate — do NOT break it "
            "down per dish, do NOT nest.\n\n"
            f"{listing}"
        )
        url = (f"https://{self.location}-aiplatform.googleapis.com/v1/projects/"
               f"{self.project_id}/locations/{self.location}/publishers/google/"
               f"models/{self.model_name}:generateContent")
        payload = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.2,
                "maxOutputTokens": 2048,
                "responseMimeType": "application/json",
                "thinkingConfig": {"thinkingBudget": 0},
            },
        }
        try:
            res = requests.post(url, headers=self._get_auth_header(),
                                json=payload, timeout=45)
            if res.status_code != 200:
                logger.error(f"Nutrition API {res.status_code}: {res.text[:200]}")
                return {}
            cands = res.json().get("candidates", [])
            if not cands or "content" not in cands[0]:
                return {}
            text = "".join(p.get("text", "")
                           for p in cands[0]["content"].get("parts", [])).strip()
            data = _loads_lenient(text)
            return data if isinstance(data, dict) else {}
        except Exception as e:
            logger.warning(f"Nutrition estimate failed: {e}")
            return {}

    def _format_student_context(self, profile: Optional[Dict[str, Any]]) -> str:
        """Only emit fields that were actually fetched — never substitute defaults."""
        if not profile:
            return "No VTOP account is connected. Do not state any personal academic facts."

        lines: List[str] = []

        def add(label: str, value: Any):
            if value not in (None, "", [], {}):
                lines.append(f"{label}: {value}")

        add("Student Name", profile.get("student_name"))
        add("Registration No", profile.get("register_no"))
        add("School", profile.get("school"))
        add("Program", profile.get("program"))
        add("Current Semester", profile.get("active_semester") or profile.get("semester"))
        add("CGPA", profile.get("cgpa"))
        add("Credits Earned", profile.get("total_credits_earned"))
        add("Blood Group", profile.get("blood_group"))
        add("Residential Status", profile.get("residential_status"))

        hostel = profile.get("hostel") or {}
        if hostel:
            add("Hostel", ", ".join(f"{k}={v}" for k, v in hostel.items() if v))
        proctor = profile.get("proctor") or {}
        if proctor.get("name") or proctor.get("email"):
            add("Assigned Proctor", f"{proctor.get('name', '')} {proctor.get('email', '')}".strip())

        courses = profile.get("registered_courses") or []
        if courses:
            lines.append("Registered Courses:")
            for c in courses:
                if c.get("course"):
                    lines.append(f"  - {c.get('course')} | {c.get('slot_venue', '')} | "
                                 f"{c.get('faculty', '')} | {c.get('status', '')}")

        attendance = profile.get("attendance") or []
        if attendance:
            lines.append("Attendance:")
            for a in attendance:
                lines.append(f"  - {a.get('course', '')}: {a.get('attended', '?')}/{a.get('total', '?')} "
                             f"({a.get('percentage', '?')}) debar={a.get('debar', '-')}")

        if not lines:
            return ("A VTOP account is connected but no records were fetched for this query. "
                    "Do not guess any values.")
        return "\n".join(lines)
