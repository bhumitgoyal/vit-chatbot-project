# VITopia AI — live VTOP academic & campus assistant

A FastAPI + Gemini chatbot that answers a VIT student's questions using **live data
pulled from `vtop.vit.ac.in` for the account they log in with** — plus a general
knowledge base (RAG) for FFCS / grading / campus rules.

## Core behaviour

| Situation | What the bot does |
| --- | --- |
| Question needs the student's own VTOP data **and no session is connected** | Asks the user to `login <username> <password>` — it never answers personal questions from cached or hardcoded data. |
| Question needs personal data **and a session is connected** | Fetches that module **fresh** from VTOP (5-minute per-module cache so one conversation doesn't hammer the portal). |
| Live fetch of a module fails | Shows a clearly-labelled **"unverified reference data — confirm on VTOP"** fallback, never a fabricated value. |
| General question (FFCS, grading scale, rules) | Answered from the RAG knowledge base, no login required. |
| Anything about other accounts / how login works internally | Declined — the bot only ever discusses the one connected student. |

## Login & CAPTCHA

`login <username> <password>` in chat starts authentication.
`VTOPService.login_with_autocaptcha()` runs the whole flow automatically:

1. `_prelogin_with_captcha()` refreshes `initialProcess` → `prelogin/setup` up to
   `CAPTCHA_MAX_ATTEMPTS` (12) times — VTOP only renders a CAPTCHA image on *some*
   attempts.
2. `_solve_captcha_with_vision()` reads the CAPTCHA with **Gemini on Vertex AI**
   (`thinkingBudget: 0` so the model actually answers) and submits the login.
3. Up to 4 solve+login attempts; a wrong CAPTCHA just retries with a fresh image.
4. Only if all 4 fail does the chat fall back to showing the image and asking the
   human to type the code.

On success the username/password are stored base64-encoded under
`data/credentials/<REG>.cred` and reused for **silent re-login** (same path) when
the live session later expires. Delete that file to disable it for an account.

## Live modules (`VTOPService.fetch_module`)

`attendance` (+ per-course **day-by-day session log** when a course is named),
`timetable` / `courses`, `marks`, `grades` (CGPA / credits / grade counts),
`exam_schedule` (dates, venue, seat), `curriculum`, `profile` (name, DOB, gender,
blood group, hosteller status, **hostel block/room/bed & mess**), `proctor`,
`proctor_messages`, `class_messages` (from faculty), `hod_dean`, `receipts`,
`library_dues`, `fee_intimations`, `assignments` (**pending** Digital
Assignments with due dates — VTOP's own "Forthcoming Digital Assignments"
dashboard widget; it does not expose a separate submitted-DA list, and the
bot says so rather than guessing), `additional_learning` (minor/honour),
`scholarships`, `biometric` (campus punch log), `project_work`
(capstone/project registration status + the resolved guide's real
email/cabin from the live faculty directory); plus `search_faculty_live` —
VTOP's real two-step "Faculty Info" search (name/designation list →
email/cabin/department detail). Just naming a faculty member ("meenakshi
email", "who is professor X") is enough, not only "faculty ...". Asking
"who teaches my courses" / "my teachers' email and cabin" resolves **every**
enrolled subject's faculty (from the timetable) through that same directory
in one shot.

Every module is confirmed against a live login —
see [`docs/VTOP_ENDPOINTS.md`](docs/VTOP_ENDPOINTS.md). Each
scrape returns `{"status": "unavailable", "vtop_path": …}` on failure; the chat
then shows a labelled "unverified — check VTOP" fallback rather than guessing.
Results are cached per module for 5 minutes per session, and a session's own
scrapes are serialised (`requests.Session` isn't concurrency-safe); different
users run in parallel on the sync request threadpool.

## Live mess menu (external feed)

`bot/messit.py` reads VinnovateIT's **MessIT** static feed —
`https://messit.vinnovateit.com/menu-data/hostel-{H}-mess-{M}.json`
(`H`: 1 Men's / 2 Ladies'; `M`: 1 Special / 2 Veg / 3 Non-Veg) — a whole
month per file, cached 1 h. `type` 1–4 = Breakfast/Lunch/Snacks/Dinner;
timings are the app's fixed slots (breakfast shifts 30 min on weekends).
"mess menu today" / "what's for dinner at LH veg mess tomorrow" (or `/mess`
on Telegram) work **without login**; a connected session auto-selects the
hostel + mess from the student's VTOP profile. Answers are labelled as coming
from MessIT, not VTOP.

## Study plan + assignment reminders

- **Study plan** (`/studyplan`, or "make me a study plan for my weak subjects") —
  groups the live `marks` rows per course, computes the **weighted standing**
  (Σ weighted ÷ Σ weightage%), and for every course under **70%** asks Gemini —
  with the `google_search` grounding tool — for weak-area topics, a 2-week
  day-by-day plan, and 4–6 real, current free resource links (NPTEL, YouTube,
  official docs, …). Returned verbatim, not re-summarised, so URLs stay intact.
- **Assignment reminders** (`/reminders`, Telegram only) — enrolment writes an
  encrypted record to **Firestore** (`assignment_reminders/{user_id}`). A
  **Cloud Scheduler** job pings `POST /cron/assignment-reminders`
  (`X-Cron-Secret` header) every 3 h; the handler re-logs each enrolled student
  in, reads their pending Digital Assignments, and pushes a Telegram nudge at
  **3 days / 1 day / a few hours** before each `Last Date`, de-duplicated so the
  same assignment+window is never sent twice. `/reminders off` disables it.

## Surfaces

- **Web** — `GET /` serves `static/index.html`; the chat client calls `POST /api/chat`
  (`{user_id, message}` → `{response, captcha_image?, student_profile, sources}`).
- **Telegram** — `POST /telegram/webhook`. Same `process_chat()` pipeline; each
  Telegram chat is its own session (`user_id = "tg<chat_id>"`). Markdown is
  down-converted to Telegram HTML, the CAPTCHA is sent as a photo, and long
  replies are chunked. Commands: `/start`, `/help`, `/login <u> <p>`, `/logout`,
  `/whoami`, `/mess`, `/reminders [off]`, `/studyplan`.

## Run

```bash
pip install -r requirements.txt
python -m uvicorn main:app --port 8000      # open http://localhost:8000
```

## Deploy (Cloud Run)

```bash
# optional: enable the Telegram bot in the same deploy
export TELEGRAM_BOT_TOKEN=123456:AA...              # from @BotFather
export TELEGRAM_WEBHOOK_SECRET=$(openssl rand -hex 16)
./deploy_gcp.sh
```

`deploy_gcp.sh` builds the image, deploys the `vitopia-agent` service, and — if
the Telegram vars are set — points `TELEGRAM_WEBHOOK_URL` at
`<service-url>/telegram/webhook` and calls Telegram `setWebhook`. `--no-cpu-throttling`
keeps the instance alive long enough to finish the background reply. It also
enables the Firestore + Cloud Scheduler APIs, creates the `(default)` Firestore
database if missing, generates/persists `CRON_SECRET`, and creates-or-updates the
`vitopia-assignment-reminders` scheduler job (every 3 h, Asia/Kolkata) — all
non-fatal, so a deploy still ships if that IAM isn't granted.

## Environment

`GCP_PROJECT_ID`, `GCP_LOCATION`, `GEMINI_MODEL` (default `gemini-2.5-flash`) for
the Vertex AI LLM + vision CAPTCHA solve; `VIT_KB_DIR` / `RAG_TOP_K` for the
knowledge base; `TELEGRAM_BOT_TOKEN` / `TELEGRAM_WEBHOOK_SECRET` /
`TELEGRAM_WEBHOOK_URL` for the bot; `VITOPIA_DEBUG=1` to expose
`GET /api/debug/vtop?user_id=…` (off by default). See `.env.example`.

## Layout

```
main.py                 process_chat() pipeline + web & Telegram endpoints
bot/vtop_service.py      per-user VTOP LiveSession, CAPTCHA retry + Vision solve, scrapers
bot/telegram.py          Telegram Bot API client + Markdown→Telegram-HTML
bot/llm.py               Gemini call + strict "only state fetched facts" prompt
bot/rag.py               local hybrid retrieval over vit_knowledge_base/
bot/memory.py            conversation history only (no profile persistence)
bot/timetable_mapper.py  FFCS slot → weekday schedule
bot/faculty_service.py   unverified faculty-directory fallback
bot/school_directory.py  unverified dean/HoD fallback
bot/proctor_service.py   unverified proctor-card fallback
bot/hostel_mess_service.py  unverified hostel-card fallback
static/                  single-page chat client
data/                    per-user runtime state — gitignored, never deployed
```
