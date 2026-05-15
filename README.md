# GoHappy Club — AI Support Bot (WhatsApp + In-App)

An intelligent customer support chatbot for **GoHappy Club**, India's senior community platform for people aged 50+. The bot answers member queries about sessions, memberships, Happy Coins, trips, and more — powered by Vertex AI RAG and Gemini 2.5 Flash, backed by Firestore for conversation memory, and deployed fully serverless on Google App Engine Standard.

**Dual-channel:** The same AI brain serves both **WhatsApp** (via Meta webhook) and your **mobile/web app** (via REST API).

---

## Architecture

```
┌──────────────────┐     ┌──────────────────┐
│  WhatsApp User   │     │  App User        │
│  (Senior Citizen)│     │  (Mobile / Web)  │
└────────┬─────────┘     └────────┬─────────┘
         │ sends message          │ POST /api/chat
         ▼                        ▼
┌────────────────────┐   ┌────────────────────┐
│ Meta WhatsApp API  │   │ In-App REST API    │
│ POST /webhook      │   │ POST /api/chat     │
└────────┬───────────┘   └────────┬───────────┘
         │                        │
         └───────────┬────────────┘
                     ▼
┌──────────────────────────────────────────────────────────────┐
│  App Engine  (FastAPI) — Shared Core Pipeline                │
│                                                              │
│  1. Deduplicate incoming message                             │
│  2. Filter junk (links, social media, emojis, greetings)     │
│  3. Load conversation state from Firestore                   │
│  4. Indic-aware input moderation                             │
│  5. Rewrite user query (Gemini — handles Hinglish/typos)     │
│  6. Trip query interception (predefined reply + coordinator  │
│     notification — skips RAG and Gemini entirely)            │
│  7. Retrieve relevant knowledge chunks (Vertex AI RAG)       │
│  8. Generate response (Gemini — structured JSON output)      │
│  9. Output moderation (RAG leakage protection)               │
│ 10. Quality audit (async, fire-and-forget)                   │
│ 11. Persist turn to Firestore                                │
│ 12. Escalate to human admin if needed                        │
│ 13. Compress rolling summary when threshold is hit           │
│                                                              │
│  Endpoints:                                                  │
│    GET  /webhook                — Meta webhook verification  │
│    POST /webhook                — Incoming WhatsApp messages │
│    POST /api/chat               — In-app chat (sync reply)   │
│    GET  /api/chat/history/{id}  — Conversation history       │
│    GET  /health                 — Health check               │
│    GET  /cache/stats            — Cache hit/miss counters    │
│    POST /cache/invalidate       — Flush all cached entries   │
│    POST /insights_update        — Auto-update KB from insights│
└──────────────────────────────────────────────────────────────┘
         │                    │                    │
         ▼                    ▼                    ▼
   ┌──────────┐      ┌──────────────┐     ┌──────────────┐
   │ Firestore │      │ Vertex AI    │     │ Gemini 2.5   │
   │ (Memory)  │      │ RAG Engine   │     │ Flash        │
   └──────────┘      └──────────────┘     └──────────────┘
```

**No external cache servers** — the semantic cache runs entirely in-process on App Engine. No Redis, no VMs, no extra infrastructure.

---

## GCP Services Required

This project runs entirely on serverless/managed GCP services. **No VMs, no Redis, no additional servers.**

| Service | Purpose | Required? |
|---------|---------|-----------|
| **App Engine** | Hosts the FastAPI app (Standard, Python 3.11) | ✅ Yes |
| **Vertex AI (Gemini 2.5 Flash)** | Query rewrite, answer generation, summary compression | ✅ Yes |
| **Vertex AI RAG Engine** | Knowledge retrieval from your document corpus | ✅ Yes |
| **Cloud Firestore** | Conversation memory (per-user state) | ✅ Yes |
| **Secret Manager** | Stores WhatsApp API tokens securely | ✅ Yes |
| ~~Cloud Build~~ | ~~Docker container builds~~ | ❌ Not needed (App Engine deploys from source) |
| ~~Artifact Registry~~ | ~~Docker image storage~~ | ❌ Not needed |
| ~~Redis / Memorystore~~ | ~~Cache backend~~ | ❌ Not needed |
| ~~Compute Engine~~ | ~~Any VM~~ | ❌ Not needed |
| ~~VPC Connector~~ | ~~Network bridge~~ | ❌ Not needed |

---

## Project Structure

```
cloudrun_gcp_initial/
├── main.py                           # FastAPI app — webhook + in-app chat + insights update + cache + health
├── gohappy_club_knowledge_base.md    # Structured Q&A knowledge base (uploaded to Vertex AI RAG)
├── bot/
│   ├── __init__.py
│   ├── whatsapp.py                   # WhatsApp Cloud API client (send/receive)
│   ├── rag.py                        # Vertex AI RAG Engine retrieval wrapper
│   ├── memory.py                     # Firestore-backed conversation state manager
│   ├── llm.py                        # Gemini prompt engineering + JSON output parser
│   ├── pipeline.py                   # Dual-channel message pipeline (WhatsApp + In-App)
│   ├── moderation.py                 # Indic-aware Hinglish tone/abuse classifier (scope: profanity only)
│   ├── evaluator.py                  # Post-response quality audit (Gemini grader with name hallucination checks)
│   ├── sheets_logger.py              # Google Sheets audit trail logger
│   ├── rag_cache.py                  # Serverless in-memory semantic cache
│   ├── message_filter.py             # Filters links, social media, emojis, greetings
│   ├── kb_manager.py                 # Knowledge base update automation
│   └── kb_insights.py                # KB improvement insights generator + latest insight fetcher
├── .env                              # Environment variables (local dev only)
├── requirements.txt                  # Python dependencies
├── app.yaml                          # App Engine configuration
├── DEPLOY.md                         # Step-by-step GCP deployment guide
├── run_dev.sh                        # Local dev launcher (cloudflared tunnel)
├── run.sh                            # Simple launcher with ngrok
├── sync_kb_to_rag.py                 # One-shot utility: push local KB file directly to Vertex AI RAG
├── Test/
│   ├── test_app_chat.py              # In-app chat + insights_update API tests (15 tests)
│   ├── test_rag_cache.py             # Cache + filter tests (17 tests)
│   ├── test_cache_pipeline.py        # End-to-end pipeline integration tests (18 tests)
│   ├── test_pipeline.py              # Pipeline unit tests
│   ├── test_rag.py                   # RAG retrieval test
│   ├── test_bad_queries.py           # Query rewriter test (Hinglish, typos, shortforms)
│   ├── test_send_receive.py          # End-to-end WhatsApp API test
│   ├── test_kb_insights.py           # KB Insights mock test
│   └── test_full_simulation.py       # Full conversation simulation test
```

---

## Modules

### `main.py` — Application Entry Point

FastAPI application with dual-channel endpoints and admin tooling:

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/webhook` | `GET` | One-time Meta webhook verification (responds with `hub.challenge`) |
| `/webhook` | `POST` | Receives all incoming WhatsApp messages; responds `200` immediately and processes in a background task |
| **`/api/chat`** | **`POST`** | **In-app chatbot — send a message, get the bot’s reply synchronously** |
| **`/api/chat/history/{user_id}`** | **`GET`** | **Retrieve conversation history for the app chat UI** |
| `/health` | `GET` | Health check — returns `{"status": "ok"}` |
| `/cache/stats` | `GET` | Cache hit/miss counters, hit rate, and total cached entries |
| `/cache/invalidate` | `POST` | Flush all cached entries (admin use) |
| **`/insights_update`** | **`POST`** | **One-click KB update: reads latest insights from Google Sheets → Gemini applies them to the KB → syncs to RAG** |

On startup, initialises the `MessagePipeline` with all dependencies (WhatsApp client, RAG engine, Firestore memory, Gemini LLM, semantic cache). CORS middleware is enabled for mobile/web app clients.

---

### `bot/message_filter.py` — Message Filter

Filters out non-actionable messages **before** they enter the pipeline, saving Gemini and RAG tokens:

| Blocked | Examples |
|---------|----------|
| **Facebook links** | `https://www.facebook.com/share/...` |
| **Instagram links** | `https://www.instagram.com/reel/...` |
| **YouTube links** | `https://youtu.be/...` |
| **Random URLs** | Any `http://` or `https://` link with no question text |
| **Short links** | `bit.ly`, `goo.gl`, `t.co`, `tinyurl.com` |
| **Emoji-only** | `🙏🙏🙏`, `👍`, `😀😀` |
| **Greeting-only** | `Good morning 🌸`, `Namaste 🙏`, `Ram Ram`, `Jai Shri Krishna` |
| **Too short** | Single characters, empty messages |

**Passes through:** Real questions ("How do I join GoHappy Club?"), Hinglish queries ("membership lena hai"), and questions that include a link alongside real text.

---

### `bot/rag_cache.py` — Semantic Cache (In-Memory, Serverless)

Intercepts after query rewriting, before Vertex AI RAG retrieval. Caches responses for semantically identical queries.

**How it works:**
1. Exact string match search against all cached entries using the rewritten query
2. If match found → **HIT** — return cached response, skip RAG + Gemini
3. If not found → **MISS** — run full pipeline, cache the result

**Key properties:**
- **No external dependencies** — runs entirely in App Engine process memory
- **Max 1,000 entries** (~1 MB total — negligible for App Engine)
- **TTL: 24 hours** — entries auto-expire so knowledge updates propagate
- **Escalation-safe** — responses with `escalation: true` are never cached

**Configuration** (env vars):

| Variable | Description | Default |
|----------|-------------|---------|
| `CACHE_SIMILARITY_THRESHOLD` | Cosine similarity for a cache hit | `0.75` |
| `CACHE_TTL_SECONDS` | Entry expiry in seconds | `86400` (24h) |
| `CACHE_MAX_ENTRIES` | Max cached entries | `1000` |

---

### `bot/whatsapp.py` — WhatsApp Cloud API Client

- **`IncomingMessage`** — dataclass representing a parsed incoming text message (message ID, sender phone, display name, phone number ID, text body).
- **`WhatsAppClient`** — handles:
  - `parse_message(payload)` — extracts text messages from Meta's nested webhook JSON. Returns `None` for status updates, reactions, images, videos, and non-text messages.
  - `send_text(to, body)` — sends a plain-text reply via the Graph API (`v19.0`).
- Uses `httpx.AsyncClient` for non-blocking HTTP.

---

### `bot/rag.py` — Vertex AI RAG Engine

- **`RAGEngine`** — retrieves the most relevant knowledge chunks from a pre-built Vertex AI RAG Corpus.
  - `query(query_text)` — calls `rag.retrieval_query()` with configurable `top_k` and `distance_threshold`. Returns a list of `RetrievedChunk` objects.
  - `format_for_prompt(chunks)` — serialises chunks into a labelled `[DOC_N]` block for LLM prompt injection.
- Degrades gracefully — if RAG fails, returns an empty list and the LLM answers from its system prompt alone.

**Configuration** (env vars):

| Variable | Description | Default |
|----------|-------------|---------|
| `VERTEX_RAG_CORPUS` | Full corpus resource name | Required |
| `RAG_TOP_K` | Number of chunks to retrieve | `8` |
| `RAG_DISTANCE_THRESHOLD` | Max vector distance | `0.5` |

---

### `bot/memory.py` — Firestore Conversation Memory

- **`ConversationMemory`** — per-user conversation state stored in a `conversations` collection.
  - `get_state(phone)` — returns the user's full state, or a clean default for new users.
  - `append_turn(phone, display_name, user_text, bot_text)` — atomically appends a user+bot turn pair using `ArrayUnion` and `Increment`. Creates the document if it doesn't exist.
  - `update_summary(phone, new_summary)` — replaces the rolling summary and trims the turn buffer.
  - `set_escalation_status(phone, status)` — toggles the human handoff pause flag.
  - `should_summarise(state)` — returns `True` when a compression cycle is due (every `SUMMARISE_EVERY` turns).
  - `build_customer_summary(state)` / `format_history_for_prompt(state)` — format state into prompt-ready strings.

**Configuration** (env vars):

| Variable | Description | Default |
|----------|-------------|---------|
| `FIRESTORE_DB` | Firestore database ID | `(default)` |
| `MAX_RECENT_TURNS` | Max raw turns kept in ring buffer | `10` |
| `SUMMARISE_EVERY` | Compress summary after N turns | `6` |

---

### `bot/llm.py` — Gemini LLM Wrapper

- **`GeminiChat`** — manages three Gemini model instances:

  | Instance | Purpose |
  |----------|---------|
  | `self.model` | Main chat — generates customer support replies with structured JSON output |
  | `self.rewrite_model` | Canonical Normalizer — rewrites broken English and Hinglish into standard English queries (20+ canonical rewrites) |
  | `self.summary_model` | Conversation compressor — generates rolling summaries |

- **System Prompt** — a comprehensive, production-grade prompt baked into the module as `SYSTEM_PROMPT` (~13K chars). Includes:
  - **Role & Identity** — GoHappy Club customer support assistant persona
  - **Company Context** — platform overview, direct app download URLs (Apple App Store & Google Play Store), key offerings, support hours
  - **Response Instructions** — HANDLE_GREETING, REJECT (with 10+ explicit categories: biodata, matrimonial, political, chain messages, personal tasks, etc.), ANSWER, ESCALATE
  - **Multi-Question Handling** — when a user asks multiple questions in one message, the bot identifies each question and answers them separately with numbered points; if any part requires escalation, the entire response is flagged
  - **Tone & Style Rules** — senior-friendly language, language matching, no jargon, name fabrication prohibition, stick-to-query discipline, app onboarding patience
  - **Key Policies & Guardrails** — member privacy, session recording access tiers, app onboarding guidance, trip discount coupon rules, strict escalation-on-doubt
  - **Strict JSON Output Schema** — `{answer, escalation}` format

- **Query Rewrite Prompt** — 20+ canonical rewrite examples covering memberships, Happy Coins, recordings, referrals, login/OTP, language change, payments, refunds, and greeting+question combos.

- **Output Parsing** — enforces JSON output via Gemini's `response_mime_type="application/json"` and a response schema. Falls back to best-effort text extraction if parsing fails.

- **Language Policy** — the bot **matches the user's language**. If the user writes in English, it replies in English. Hindi → Hindi. Hinglish → Hinglish. This ensures senior members can communicate naturally in their preferred language.

---

### `bot/pipeline.py` — Dual-Channel Message Pipeline

The core orchestration layer. Supports both **WhatsApp** and **In-App** channels through a shared core pipeline.

**Key methods:**

| Method | Purpose |
|--------|---------|
| `handle(payload)` | WhatsApp entry point — parses webhook, handles admin commands, then delegates to core |
| `handle_app_message(user_id, display_name, text, message_id)` | In-app entry point — returns `BotResponse` directly |
| `_process_core(...)` | Shared brain — dedup, filter, moderation, trip interception, RAG, Gemini, audit, persist |
| `_is_trip_query(text, polished_query)` | Detects trip-related queries via keyword matching across original and rewritten text |
| `_notify_trips_coordinator(...)` | Sends a WhatsApp alert to the trips coordinator with the user's name, query, and quick-reply link |

**Key features:**

- **Dual-Channel** — the same AI pipeline serves both WhatsApp and in-app users. WhatsApp-specific I/O (typing indicators, mark-as-read, admin commands) only runs for WhatsApp messages.
- **Message Filtering** — blocks links, social media URLs, emoji-only, and greeting-only messages before they consume any Gemini or RAG tokens.
- **Deduplication** — an in-memory set of the last 500 message IDs prevents duplicate processing.
- **Trip Query Interception** — any query containing trip-related keywords (trip, tour, travel, yatra, safar, holiday, vacation, trek, etc.) is intercepted before RAG and Gemini. The user receives a predefined redirect message with a WhatsApp link to the trips coordinator (`TRIPS_CONTACT_NUMBER`). Simultaneously, the coordinator receives a WhatsApp notification containing the user's name, exact query, and a `wa.me` quick-reply link back to the user. RAG and Gemini are skipped entirely for these queries.
- **Admin Override** — the `ADMIN_PHONE_NUMBER` can send `/resolve <PHONE>` to unpause a user (WhatsApp-only).
- **Escalation** — when Gemini flags `escalation: true`, the bot pauses itself for that user. On WhatsApp, it alerts the admin. On the app, the `escalation` flag is returned in the API response. **Auto-unpause after 15 minutes** if no admin action is taken.
- **Multi-Question Handling** — when a user sends a message containing multiple questions, the bot identifies and answers each one individually, escalating if any part is unanswerable.
- **Language Matching** — the bot replies in the same language the user writes in (English, Hindi, Hinglish, etc.).
- **Stick-to-Query Guardrail** — the bot only answers what was asked and escalates when it's not 100% confident, rather than guessing or volunteering tangential information.
- **Background Summary Compression** — compresses recent turns into a rolling summary every N turns (async, doesn't block the reply).


---

## In-App Chat API

The in-app chat API lets your mobile or web application use the same AI chatbot that serves WhatsApp users. The bot processes messages synchronously and returns the reply in the HTTP response body.

### `POST /api/chat` — Send a Message

**Request:**

```json
{
  "user_id": "firebase_uid_or_phone",
  "display_name": "Ramesh Kumar",
  "message": "What is GoHappy Club?",
  "message_id": "optional-uuid-for-dedup"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `user_id` | string | Yes | Stable user identifier (Firebase UID, phone number, or custom ID) |
| `display_name` | string | No | User's name (default: `"App User"`) |
| `message` | string | Yes | The chat message text |
| `message_id` | string | No | Client-side dedup ID (auto-generated UUID if omitted) |

**Response:**

```json
{
  "reply": "GoHappy Club is India's senior community platform for people aged 50+. We offer daily live sessions, workshops, trips, and more!",
  "escalation": false
}
```

| Field | Type | Description |
|-------|------|-------------|
| `reply` | string | Bot's reply text (empty if message was filtered) |
| `escalation` | bool | `true` if the query was escalated to human support |

**Example (curl):**

```bash
curl -X POST https://your-app.appspot.com/api/chat \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "user_abc123",
    "display_name": "Ramesh",
    "message": "How do I join GoHappy Club?"
  }'
```

### `GET /api/chat/history/{user_id}` — Conversation History

Returns recent conversation turns for rendering in the app's chat UI.

**Response:**

```json
{
  "display_name": "Ramesh Kumar",
  "turns": [
    { "role": "user", "content": "What is GoHappy Club?", "ts": "2026-05-10T10:00:00Z" },
    { "role": "assistant", "content": "GoHappy Club is India's senior community platform...", "ts": "2026-05-10T10:00:01Z" }
  ],
  "summary": "Ramesh asked about GoHappy Club membership options."
}
```

### CORS Configuration

CORS is enabled by default (all origins). To restrict origins in production, set the `CORS_ORIGINS` environment variable:

```env
CORS_ORIGINS=https://your-app.com,https://staging.your-app.com
```

### Conversation Merging

If you use the same `user_id` as the WhatsApp phone number (e.g. `"919876543210"`), conversations will be **merged** across both channels — the app user will see their WhatsApp history and vice versa. Use a different ID scheme (e.g. Firebase UID) for separate threads.

---

## Firestore Data Model

```
conversations/                         # Collection
  └── <phone_number>/                  # Document (e.g. "1234567890")
        display_name:       str        # "Ramesh Kumar"
        summary:            str        # AI-generated rolling summary
        turn_count:         int        # Total turns in this session
        last_seen:          timestamp  # Last interaction time (UTC)
        escalated_to_human: bool       # Human handoff pause flag
        recent_turns:       list       # Ring buffer of recent turns
          [
            { role: "user",      content: "...", ts: <timestamp> },
            { role: "assistant", content: "...", ts: <timestamp> },
            ...
          ]
```

---

## Environment Variables

Create a `.env` file for local development:

```env
# GCP
GCP_PROJECT_ID=ghc-chatbot
GCP_LOCATION=asia-south1
VERTEX_RAG_CORPUS=projects/ghc-chatbot/locations/asia-south1/ragCorpora/<CORPUS_ID>
GEMINI_MODEL=gemini-2.5-flash

# RAG Tuning
RAG_TOP_K=4
RAG_DISTANCE_THRESHOLD=0.5

# WhatsApp Cloud API (from Meta Business Suite)
WHATSAPP_ACCESS_TOKEN=<your-access-token>
WHATSAPP_PHONE_NUMBER_ID=<your-phone-number-id>
WHATSAPP_BUSINESS_ACCOUNT_ID=<your-business-account-id>
WHATSAPP_VERIFY_TOKEN=<your-webhook-verify-token>

# Admin Routing
ADMIN_PHONE_NUMBER=<admin-phone-in-E164-format>
TRIPS_CONTACT_NUMBER=<trips-coordinator-phone-in-E164-format>

# Semantic Cache (in-memory, serverless)
CACHE_SIMILARITY_THRESHOLD=0.92
CACHE_TTL_SECONDS=86400
CACHE_MAX_ENTRIES=1000

# Optional
PORT=8080
FIRESTORE_DB=(default)
MAX_RECENT_TURNS=10
SUMMARISE_EVERY=6

# In-App Chat API (CORS)
CORS_ORIGINS=*    # Comma-separated list of allowed origins, or * for all
```

> **⚠️ Never commit `.env` to version control.** For production, use GCP Secret Manager (see [DEPLOY.md](DEPLOY.md)).

---

## Getting Started

### Prerequisites

- Python 3.11+
- GCP project with billing enabled
- APIs enabled: **Vertex AI**, **Firestore**, **App Engine**, **Cloud Build**, **Secret Manager**
- Meta Developer account with a **WhatsApp Business App**
- A **Vertex AI RAG Corpus** created and populated with your knowledge base documents
- A **Firestore (Native mode)** database created in your GCP project
- `gcloud` CLI authenticated (`gcloud auth login`)
- `cloudflared` installed for local tunnelling (`brew install cloudflared`)

### Local Development

```bash
# 1. Clone the repo
git clone <repo-url> && cd cloudrun_gcp_initial

# 2. Install dependencies
pip install -r requirements.txt

# 3. Set up .env
cp .env.example .env   # or create from the template above
# Fill in all values

# 4. Run with the dev launcher (starts server + Cloudflare tunnel)
chmod +x run_dev.sh && ./run_dev.sh
```

The dev script will:
1. Load `.env` variables
2. Run pre-flight checks (Python, cloudflared, required env vars)
3. Install pip dependencies
4. Start the FastAPI server on port 8080
5. Start a Cloudflare Quick Tunnel (no account needed)
6. Print the public webhook URL to paste into Meta Developer Console

### Register the Webhook

1. Go to [developers.facebook.com](https://developers.facebook.com) → your App → WhatsApp → Configuration
2. Set **Callback URL** to the tunnel URL printed by `run_dev.sh` (e.g. `https://xxx.trycloudflare.com/webhook`)
3. Set **Verify Token** to the value of `WHATSAPP_VERIFY_TOKEN` in your `.env`
4. Subscribe to the **messages** webhook field

---

## Production Deployment (App Engine)

See [DEPLOY.md](DEPLOY.md) for the full step-by-step guide covering:

1. Creating the Vertex AI RAG Corpus
2. Setting up Firestore
3. Setting local config values in env_variables.yaml
4. Creating a service account with IAM roles
5. Building and deploying the app configuration to App Engine
6. Registering the WhatsApp webhook

### Quick Deploy

```bash
gcloud app deploy app.yaml --project=ghc-chatbot --quiet
```

> **Memory**: The app is highly optimized and requires very little memory. A basic F1 or F2 App Engine instance is typically sufficient.

---

## Escalation & Rejection Flow

When the bot processes a message, it enforces strict guardrails to prevent hallucinations, spam, and off-topic content:

### 1. Out-of-Context Responses (Rejections)
The bot **rejects** messages that are completely unrelated to GoHappy Club. This includes:
- Random trivia (e.g. "what are the top 10 schools in India?", "how do I fix my car?")
- Personal biodata or matrimonial/marriage bureau inquiries
- External news articles, political messages, or forwarded chain messages
- Unsolicited advertisements
- Requests for the bot to perform personal tasks (making payments, taking contact details)
- Medical or financial advice

The bot politely states it can only help with GoHappy Club and explicitly **avoids** alerting humans (escalation: false).

### 2. Standard Escalations (Missing context or account issues)
When the query *is* related to GoHappy Club, but the bot cannot answer accurately (e.g. missing policies), or the user asks for a human:

```
1. Gemini returns { "escalation": true }
             │
             ▼
2. Bot sends an escalation message to the user
   ("Let me connect you with our team...")
             │
             ▼
3. Bot sets escalated_to_human = true in Firestore
   → All further messages from this user are silently ignored by the bot
             │
             ▼
4. Admin receives a WhatsApp alert:
   🚨 ESCALATION REQUIRED
   User: <name> (<phone>)
   Issue: <their message>
   Bot replied: <what the bot said>
             │
             ▼
5. Admin replies to the user directly from their WhatsApp app
             │
             ▼
6. When resolved, admin sends:
   /resolve <PHONE_NUMBER>
   → Bot resumes handling messages for that user
   
   (Auto-Unpause: If 15 minutes pass, the bot will automatically unpause the user when they send their next message).
```

---

## Admin WhatsApp Commands & Automation

The bot includes an extensive suite of commands available to admin numbers via WhatsApp. There are two tiers of admins:

| Tier | Who | Can do |
|------|-----|--------|
| **Super Admin** | Hardcoded number (`919818646823`) + `ADMIN_PHONE_NUMBER` env var | All commands, including adding/removing admins |
| **Dynamic Admin** | Numbers added via `/admin_add` (stored in Firestore `config/admins`) | All commands **except** `/admin_add` and `/admin_remove` |

### 1. Update Knowledge Base (`/update_kb`)
Whenever a user asks a question the bot doesn't know, or you launch a new feature/policy, you can update the brain of the chatbot by sending a message:
*   **Command:** `/update_kb <your instructions>`
*   **Example:** `/update_kb We are launching a new Platinum Membership for ₹5000/year. It includes unlimited free trips.`
*   **What Happens:** The bot uses Gemini to surgically edit the master Knowledge Base document and sends you back the updated Markdown text as a preview message.

### 2. Approve KB Update (`/approve_kb`)
Once you review the generated draft from `/update_kb` and confirm it looks correct:
*   **Command:** `/approve_kb`
*   **What Happens:** The system promotes the draft to the Master Copy in Firestore. It then securely connects to Google Cloud Vertex AI, deletes the old files in the RAG Corpus, and uploads the new `.md` file. The chatbot instantly begins using the new facts!

### 3. Generate Automated Insights (`/insights`)
Help continuously improve the knowledge base by analyzing recent problematic conversations (frustrations, hallucinations) logged in the Audit Spreadsheet.
*   **Command:** `/insights`
*   **What Happens:** The bot reads the `Audit_Logs` Google Sheet, uses Gemini to analyze what went wrong, and generates actionable recommendations of exactly what paragraphs or facts to add to your knowledge base to prevent future issues. It logs these into the `Insights` tab and replies to you on WhatsApp.

### 4. Resolve Escalation (`/resolve`)
When the bot escalates a conversation to a human, it automatically mutes itself for that user so you can step in and chat. Once you're done helping the user, you can unmute the bot.
*   **Command:** `/resolve <PHONE_NUMBER>`
*   **Example:** `/resolve 919876543210`
*   **What Happens:** The bot resumes handling automated messages for that user.

### 5. Add Admin (`/admin_add`) — Super Admin only
Grant admin access to another phone number so they can use all bot commands (except admin management).
*   **Command:** `/admin_add <12-digit number starting with 91>`
*   **Example:** `/admin_add 919876543210`
*   **Validation:** The number must be exactly 12 digits and start with `91` (Indian country code).
*   **What Happens:** The number is added to the dynamic admin list in Firestore (`config/admins` document). They will immediately be able to use all admin commands except `/admin_add` and `/admin_remove`.

### 6. Remove Admin (`/admin_remove`) — Super Admin only
Revoke admin access from a dynamically added admin.
*   **Command:** `/admin_remove <12-digit number starting with 91>`
*   **Example:** `/admin_remove 919876543210`
*   **What Happens:** The number is removed from Firestore. They will no longer have access to any admin commands. Super admins (hardcoded + env var) cannot be removed.

### 7. One-Click Insights-to-KB Update (`POST /insights_update`)
Automates the full cycle of reading insights → updating the knowledge base → syncing to RAG in a single HTTP call.
*   **Endpoint:** `POST /insights_update`
*   **Prerequisites:** You must have run `/insights` at least once to populate the "KB Insights" tab.
*   **What Happens:**
    1. Reads the **last row** from the "KB Insights" tab in the Google Audit Sheet
    2. Feeds the insight text to **Gemini** to surgically update the master Knowledge Base
    3. Saves the updated KB to Firestore
    4. **Deletes old files** from the Vertex AI RAG Corpus and **uploads the new KB**
    5. Returns a success response with the insight and new KB character counts

*   **Example (curl):**
    ```bash
    curl -X POST https://your-app.appspot.com/insights_update
    ```

*   **Response:**
    ```json
    {
      "status": "ok",
      "message": "Knowledge base updated from latest insights and synced to RAG.",
      "insight_length": 2456,
      "new_kb_length": 19830
    }
    ```

*   **Error Cases:**
    - `404` — No insights found (run `/insights` first)
    - `500` — Gemini update or RAG sync failure


---

## Test Suite

| Script | Tests | Purpose |
|--------|-------|---------|
| `test_app_chat.py` | 15 | In-app chat API endpoints + /insights_update (POST /api/chat, GET /api/chat/history, POST /insights_update, validation) |
| `test_rag_cache.py` | 17 | In-memory cache (hit/miss/semantic/TTL/escalation) + message filter (links/emoji/greetings) |
| `test_cache_pipeline.py` | 18 | End-to-end pipeline: proves RAG + Gemini are SKIPPED on cache HIT |
| `test_pipeline.py` | — | Pipeline unit tests — simulates webhook payloads locally |
| `test_rag.py` | — | Tests RAG retrieval quality against the knowledge corpus |
| `test_bad_queries.py` | — | Tests the query rewriter with broken English, Hinglish, and shortforms |
| `test_full_simulation.py` | — | Full conversation simulation — multi-turn flows, escalation, dedup, summary compression |
| `test_send_receive.py` | — | End-to-end test — sends real messages via WhatsApp API |
| `test_kb_insights.py` | — | KB Insights generation mock test |
| `test_evaluator.py` | — | Evaluator/grader output parsing and JSON repair tests |

```bash
# Run all API tests (chat + insights_update) — no GCP creds needed
python -m pytest Test/test_app_chat.py -v

# Run cache + filter tests
python Test/test_rag_cache.py

# Run pipeline integration tests (no GCP creds needed)
python Test/test_cache_pipeline.py

# Run the full simulation
python Test/test_full_simulation.py

# One-shot: sync local KB to Vertex AI RAG corpus (requires GCP auth)
python sync_kb_to_rag.py
```

---

## Tech Stack

| Component | Technology | Version |
|-----------|------------|---------|
| **Runtime** | Python | 3.11 |
| **Web Framework** | FastAPI + Uvicorn | 0.111.0 / 0.30.1 |
| **LLM** | Google Gemini 2.5 Flash (via Vertex AI) | SDK 1.71.1 |
| **Knowledge Retrieval** | Vertex AI RAG Engine | SDK 1.71.1 |
| **Conversation Memory** | Google Cloud Firestore (Native mode) | SDK 2.16.0 |
| **Messaging** | WhatsApp Business Cloud API (Meta) | Graph API v19.0 |
| **Exact Match Cache** | Python Dict (in-memory) | — |
| **HTTP Client** | httpx (async) | 0.27.0 |
| **Deployment** | GCP App Engine Standard (F2 Instance) | — |
| **Secrets** | GCP Secret Manager | — |
| **Local Tunnelling** | Cloudflare Quick Tunnel (`cloudflared`) | — |

---

## Key Design Decisions

1. **Background processing** — webhook POSTs return `200` immediately. The actual pipeline runs as a FastAPI `BackgroundTask` to avoid Meta timeout retries.

2. **Message filtering** — seniors frequently forward Facebook posts, YouTube videos, "Good morning" images, and random links. The filter blocks these at the gate, saving Gemini and RAG tokens.

3. **In-memory semantic cache** — no external Redis or Memorystore needed. The cache runs inside App Engine process memory (~2.5 MB for 1,000 entries). On cache HIT, response time drops from 2-4s to <50ms and RAG+Gemini calls are skipped entirely. With automatic scaling, the cache stays warm across requests.

4. **Query rewriting** — senior users often type in Hinglish, broken English, or shortforms. A lightweight Gemini call polishes the query before RAG retrieval for much better chunk matching.

5. **Strict JSON output** — the main Gemini call uses `response_mime_type="application/json"` with a schema to guarantee parseable `{answer, escalation}` output. A regex fallback handles rare edge cases.

6. **Rolling summary compression** — instead of passing the full conversation history to every LLM call (which would hit context limits and increase cost), the bot compresses older turns into a summary every N turns. Only the summary + last 10 raw turns are sent.

7. **In-process deduplication** — Meta's webhook delivery can fire multiple times for the same message. A simple in-memory set (capped at 500 entries) prevents duplicate processing.

8. **Graceful degradation** — if RAG fails, the bot still answers from its system prompt. If Gemini fails, it returns a polite error with a phone number. If Firestore fails, the error is logged but the webhook still returns 200. If the cache fails, the pipeline runs normally without it.

9. **Language-matching replies** — the bot detects the language of each incoming message and replies in the same language (English, Hindi, Hinglish, or other Indian languages). This makes the experience natural for senior members who prefer their native language.

10. **Multi-question decomposition** — when a user packs several questions into one message ("Gold plan ka price kya hai aur mera payment status check karo"), the bot identifies each question, answers what it can from context, and escalates the rest — all within a single reply.

11. **Anti-Hallucination Guardrails** — The prompt explicitly forces the bot to reject unrelated trivia, forbids answering medical or financial inquiries, and escalates whenever the answer is not 100% supported by retrieved context. The bot sticks strictly to what was asked and never volunteers tangential facts.

12. **Name Fabrication Prevention** — The system prompt, evaluator, and quality auditor all explicitly prohibit inventing or assuming customer names. Only names present in the conversation context or customer summary may be used.

13. **Insights-to-KB Automation** — The `/insights_update` endpoint automates the full cycle of analyzing audit insights → applying them to the knowledge base → syncing to Vertex AI RAG, eliminating manual copy-paste steps.

14. **Trip Query Interception** — Trip enquiries are intercepted after query rewriting but before RAG retrieval. This avoids burning Gemini + RAG tokens on queries that can never be answered from the knowledge base (live trip availability, pricing, and booking depend on human coordination). The bot gives the user an instant redirect, while the trips coordinator gets a WhatsApp notification with the user's name, exact query, and a one-tap `wa.me` link to reply directly to that user.

---

## License

Private — GoHappy Club internal project.
