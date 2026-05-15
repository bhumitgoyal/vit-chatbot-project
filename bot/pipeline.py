"""
bot/pipeline.py
Orchestrates the full message handling flow:
  Input → Filter → Dedup → Firestore → Cache? → RAG → Gemini → Cache store → Firestore → Output

Supports two channels:
  - WhatsApp: receives Meta webhook payloads, replies via WhatsApp Cloud API
  - In-App:   receives direct API calls, returns BotResponse to the caller

Also handles:
  - Message filtering (blocks links, social media forwards, emoji-only, greetings)
  - Deduplication (ignores repeated webhook deliveries for same message ID)
  - Rolling summary compression via Gemini when turn threshold is hit
  - Escalation flag — logs and (optionally) routes to a human queue
"""

import os
import uuid
import logging
import asyncio
from typing import Optional

from bot.whatsapp        import WhatsAppClient, IncomingMessage
from bot.rag             import RAGEngine
from bot.memory          import ConversationMemory
from bot.llm             import GeminiChat, BotResponse
from bot.moderation      import HinglishModerator
from bot.rag_cache       import RAGCache
from bot.message_filter  import filter_message
from bot.evaluator       import OutputValidator
from bot.sheets_logger   import SheetsAuditLogger
from bot.kb_insights     import KBInsightsGenerator
from bot.kb_manager      import KnowledgeBaseManager

logger = logging.getLogger("gohappy.pipeline")

# Simple in-process dedup cache (last 500 message IDs)
_SEEN_IDS: set = set()
_SEEN_MAX  = 500

_TRIP_KEYWORDS = {
    "trip", "trips", "tour", "tours", "travel", "travelling", "traveling",
    "yatra", "safar", "holiday", "holidays", "vacation", "excursion",
    "pilgrimage", "tirth", "destination", "itinerary", "package",
    "outing", "sightseeing", "picnic", "trek", "trekking",
}


class MessagePipeline:

    def __init__(
        self,
        whatsapp:      WhatsAppClient = None,
        rag:           RAGEngine      = None,
        memory:        ConversationMemory = None,
        llm:           GeminiChat     = None,
        cache:         RAGCache       = None,
        evaluator:     OutputValidator  = None,
        sheets_logger: SheetsAuditLogger = None,
        kb_insights:   KBInsightsGenerator = None,
        kb_manager:    KnowledgeBaseManager = None,
    ):
        self.wa            = whatsapp
        self.rag           = rag
        self.memory        = memory
        self.llm           = llm
        self.cache         = cache
        self.evaluator     = evaluator
        self.sheets_logger = sheets_logger
        self.kb_insights   = kb_insights
        self.kb_manager    = kb_manager
        self.moderator     = HinglishModerator()

    # ══════════════════════════════════════════════════════════════════════════
    #  WHATSAPP ENTRY POINT (existing behaviour, unchanged)
    # ══════════════════════════════════════════════════════════════════════════

    async def handle(self, payload: dict):
        """
        Full pipeline for one incoming webhook payload.
        Any uncaught exception is logged but not re-raised (background task).
        """
        try:
            await self._process_whatsapp(payload)
        except Exception as exc:
            logger.error("Unhandled pipeline error: %s", exc, exc_info=True)

    # ══════════════════════════════════════════════════════════════════════════
    #  IN-APP ENTRY POINT (new)
    # ══════════════════════════════════════════════════════════════════════════

    async def handle_app_message(
        self,
        user_id:      str,
        display_name: str,
        text:         str,
        message_id:   str = None,
    ) -> Optional[BotResponse]:
        """
        Process a message from the in-app chatbot.
        Returns the BotResponse directly (answer + escalation flag).
        Returns None if the message was filtered or deduplicated.
        """
        if not message_id:
            message_id = str(uuid.uuid4())

        try:
            return await self._process_core(
                user_id=user_id,
                display_name=display_name,
                text=text,
                message_id=message_id,
                channel="app",
            )
        except Exception as exc:
            logger.error("Unhandled app pipeline error: %s", exc, exc_info=True)
            return BotResponse(
                answer="I'm having a little trouble right now. Please try again in a moment.",
                escalation=False,
            )

    # ══════════════════════════════════════════════════════════════════════════
    #  WHATSAPP-SPECIFIC FLOW
    # ══════════════════════════════════════════════════════════════════════════

    async def _process_whatsapp(self, payload: dict):
        """
        WhatsApp-specific wrapper: parses the webhook payload, handles admin
        commands, then delegates to _process_core for the shared brain logic.
        """
        # 1. Parse
        msg: Optional[IncomingMessage] = self.wa.parse_message(payload)
        if msg is None:
            return   # Not an actionable message (status update, etc.)

        admin_phone = os.environ.get("ADMIN_PHONE_NUMBER")

        # 1.5 Admin Override & Insights Logic
        # Super admins: hardcoded + env var (can manage admin list)
        super_admin_numbers = {"919818646823", "+919818646823"}
        if admin_phone:
            super_admin_numbers.add(admin_phone)
        is_super_admin = msg.from_number in super_admin_numbers

        # Dynamic admins: stored in Firestore (can use all commands except admin management)
        dynamic_admins = await self.memory.get_admin_numbers()
        is_admin = is_super_admin or msg.from_number in dynamic_admins
        
        if is_admin:
            if msg.text.strip().lower() == "insights":
                await self.wa.send_text(
                    to=msg.from_number,
                    body="⏳ Generating KB Insights... This may take a minute.",
                    phone_number_id=msg.phone_number_id
                )
                asyncio.create_task(self._run_insights_generator(msg))
            elif msg.text.startswith("/resolve "):
                target_phone = msg.text.split(" ")[1].strip()
                await self.memory.set_escalation_status(target_phone, False)
                await self.wa.send_text(
                    to=msg.from_number, 
                    body=f"✅ Resolved escalation for {target_phone}. Bot is back in control.",
                    phone_number_id=msg.phone_number_id
                )
            elif msg.text.startswith("/admin_add "):
                if not is_super_admin:
                    await self.wa.send_text(
                        to=msg.from_number,
                        body="❌ Only super admins can add or remove admins.",
                        phone_number_id=msg.phone_number_id
                    )
                else:
                    target = msg.text.split(" ")[1].strip().replace("+", "")
                    import re as _re
                    if not _re.fullmatch(r"91\d{10}", target):
                        await self.wa.send_text(
                            to=msg.from_number,
                            body="⚠️ Invalid number. Must be a 12-digit number starting with 91.\n\nUsage: `/admin_add 91XXXXXXXXXX`\nExample: `/admin_add 919876543210`",
                            phone_number_id=msg.phone_number_id
                        )
                    else:
                        added = await self.memory.add_admin(target)
                        if added:
                            await self.wa.send_text(
                                to=msg.from_number,
                                body=f"✅ {target} has been added as an admin.",
                                phone_number_id=msg.phone_number_id
                            )
                        else:
                            await self.wa.send_text(
                                to=msg.from_number,
                                body=f"ℹ️ {target} is already an admin.",
                                phone_number_id=msg.phone_number_id
                            )
            elif msg.text.startswith("/admin_remove "):
                if not is_super_admin:
                    await self.wa.send_text(
                        to=msg.from_number,
                        body="❌ Only super admins can add or remove admins.",
                        phone_number_id=msg.phone_number_id
                    )
                else:
                    target = msg.text.split(" ")[1].strip().replace("+", "")
                    import re as _re
                    if not _re.fullmatch(r"91\d{10}", target):
                        await self.wa.send_text(
                            to=msg.from_number,
                            body="⚠️ Invalid number. Must be a 12-digit number starting with 91.\n\nUsage: `/admin_remove 91XXXXXXXXXX`\nExample: `/admin_remove 919876543210`",
                            phone_number_id=msg.phone_number_id
                        )
                    elif target in super_admin_numbers or f"+{target}" in super_admin_numbers:
                        await self.wa.send_text(
                            to=msg.from_number,
                            body="❌ Cannot remove a super admin.",
                            phone_number_id=msg.phone_number_id
                        )
                    else:
                        removed = await self.memory.remove_admin(target)
                        if removed:
                            await self.wa.send_text(
                                to=msg.from_number,
                                body=f"✅ {target} has been removed from the admin list.",
                                phone_number_id=msg.phone_number_id
                            )
                        else:
                            await self.wa.send_text(
                                to=msg.from_number,
                                body=f"ℹ️ {target} is not in the admin list.",
                                phone_number_id=msg.phone_number_id
                            )
            elif msg.text.startswith("/update_kb"):
                admin_input = msg.text[len("/update_kb"):].strip()
                if not admin_input:
                    await self.wa.send_text(
                        to=msg.from_number,
                        body="⚠️ Please include your update instructions.\n\nUsage: `/update_kb <your instructions>`\nExample: `/update_kb We are launching a new Platinum Membership for ₹5000/year.`",
                        phone_number_id=msg.phone_number_id
                    )
                else:
                    await self.wa.send_text(
                        to=msg.from_number, 
                        body="⏳ Processing your KB update request with Gemini... Please wait.",
                        phone_number_id=msg.phone_number_id
                    )
                    asyncio.create_task(self._handle_update_kb(msg))
            elif msg.text.strip().lower() == "/approve_kb":
                await self.wa.send_text(
                    to=msg.from_number, 
                    body="⏳ Approving update and syncing to Vertex AI... Please wait.",
                    phone_number_id=msg.phone_number_id
                )
                asyncio.create_task(self._handle_approve_kb(msg))
            elif msg.text.strip().lower() == "/insights_update":
                await self.wa.send_text(
                    to=msg.from_number,
                    body="⏳ Fetching latest insights and applying to Knowledge Base... Please wait.",
                    phone_number_id=msg.phone_number_id
                )
                asyncio.create_task(self._handle_insights_update(msg))
            else:
                admin_commands = (
                    "Hello Admin! Here are the available commands:\n\n"
                    "📝 `/update_kb <instructions>` — Update the Knowledge Base\n"
                    "✅ `/approve_kb` — Approve & sync KB draft to Vertex AI\n"
                    "🔓 `/resolve <PHONE>` — Unpause bot for a user\n"
                    "📊 `insights` — Generate KB improvement insights\n"
                    "🚀 `/insights_update` — Auto-update KB from latest insights\n"
                )
                if is_super_admin:
                    admin_commands += (
                        "\n👤 `/admin_add 91XXXXXXXXXX` — Add a new admin\n"
                        "🚫 `/admin_remove 91XXXXXXXXXX` — Remove an admin\n"
                    )
                    if dynamic_admins:
                        admin_commands += f"\n📋 Current dynamic admins: {', '.join(dynamic_admins)}\n"
                admin_commands += "\nAny other message from you is ignored by the bot."
                await self.wa.send_text(
                    to=msg.from_number,
                    body=admin_commands,
                    phone_number_id=msg.phone_number_id
                )
            return

        # Mark message as read (WhatsApp-specific)
        asyncio.create_task(self.wa.mark_as_read(msg.wa_message_id, msg.phone_number_id))

        # Show typing indicator (WhatsApp-specific)
        asyncio.create_task(self.wa.send_typing_indicator(msg.from_number, msg.phone_number_id))

        # Delegate to core pipeline
        bot_response = await self._process_core(
            user_id=msg.from_number,
            display_name=msg.display_name,
            text=msg.text,
            message_id=msg.wa_message_id,
            channel="whatsapp",
            phone_number_id=msg.phone_number_id,
            bot_phone_number=msg.bot_phone_number,
        )

        if bot_response is None:
            return  # Filtered / deduped / escalated-to-human

        # Send reply via WhatsApp
        sent = await self.wa.send_text(
            to             = msg.from_number,
            body           = bot_response.answer,
            phone_number_id= msg.phone_number_id,
        )
        if not sent:
            logger.error("Failed to deliver message to %s", msg.from_number)

    # ══════════════════════════════════════════════════════════════════════════
    #  SHARED CORE PIPELINE (channel-agnostic brain)
    # ══════════════════════════════════════════════════════════════════════════

    async def _process_core(
        self,
        user_id:           str,
        display_name:      str,
        text:              str,
        message_id:        str,
        channel:           str,            # "whatsapp" or "app"
        phone_number_id:   str = None,     # WhatsApp-only
        bot_phone_number:  str = None,     # WhatsApp-only
    ) -> Optional[BotResponse]:
        """
        Shared pipeline logic used by both WhatsApp and in-app channels.
        Returns a BotResponse, or None if the message should be silently dropped.
        """
        # 1. Deduplicate
        if message_id in _SEEN_IDS:
            logger.info("Duplicate message %s — skipping.", message_id)
            return None
        _SEEN_IDS.add(message_id)
        if len(_SEEN_IDS) > _SEEN_MAX:
            _SEEN_IDS.pop()

        # 2. Filter out junk messages (links, forwards, emojis, greetings)
        filter_result = filter_message(text)
        if not filter_result.is_actionable:
            logger.info(
                "Filtered out message from %s: %s — %.80s",
                user_id, filter_result.reason, text,
            )
            return None

        logger.info("[%s] Processing message from %s: %.80s", channel, user_id, text)

        # 3. Load conversation state from Firestore
        state = await self.memory.get_state(user_id)

        if state.get("escalated_to_human", False):
            last_seen = state.get("last_seen")
            if last_seen:
                from datetime import datetime, timezone
                now = datetime.now(timezone.utc)
                if (now - last_seen).total_seconds() > 15 * 60:
                    logger.info("User %s was escalated but 15 mins have passed. Auto-unpausing.", user_id)
                    await self.memory.set_escalation_status(user_id, False)
                    state["escalated_to_human"] = False
                else:
                    logger.info("Ignoring message from %s, currently escalated to human.", user_id)
                    return None
            else:
                logger.info("Ignoring message from %s, currently escalated to human.", user_id)
                return None

        # 3.4 Indic-aware Input Moderation
        mod_result = await self.moderator.analyze_message(text)
        logger.info("Moderation input result: %s", mod_result)
        
        severity = mod_result.get("severity", "none")
        if severity == "targeted_abuse":
            bot_response = BotResponse(
                answer="Your message violates our community guidelines. An admin will review this conversation.",
                escalation=True
            )
            # Escalation handling
            await self._handle_escalation_generic(
                user_id=user_id,
                display_name=display_name,
                text=text,
                bot_response=bot_response,
                state=state,
                channel=channel,
                phone_number_id=phone_number_id,
                bot_phone_number=bot_phone_number,
            )
            return bot_response
            
        is_frustrated = (severity == "frustration")
        if severity in ["conversational", "frustration"]:
            # Override text to remove filler profanity so RAG/LLM isn't distracted
            text = mod_result.get("stripped_text", text)

        # 3.5 Polish query for better RAG retrieval
        polished_query = await self.llm.rewrite_query(text)
        if not polished_query:
            polished_query = text # Fallback to original text if the LLM completely stripped a greeting
        logger.info("Polished query: %s", polished_query)

        # 3.6 Trip query interception
        if self._is_trip_query(text, polished_query):
            trips_number = os.environ.get("TRIPS_CONTACT_NUMBER", "917973578469")
            predefined_reply = (
                f"For the latest trip options and offers, please reach out directly on WhatsApp: "
                f"https://wa.me/{trips_number} 🧳\n\n"
                f"Our trips coordinator will share all current packages and details with you!"
            )
            bot_response = BotResponse(answer=predefined_reply, escalation=False)
            asyncio.create_task(
                self._notify_trips_coordinator(
                    user_id=user_id,
                    display_name=display_name,
                    text=text,
                    channel=channel,
                    phone_number_id=phone_number_id,
                )
            )
            await self.memory.append_turn(
                phone=user_id,
                display_name=display_name,
                user_text=text,
                bot_text=predefined_reply,
            )
            return bot_response

        # ── 3.7 Semantic cache check ─────────────────────────────────────────
        # cached = await self.cache.get(polished_query)
        # if cached is not None:
        #     bot_response = BotResponse(
        #         answer     = cached["answer"],
        #         escalation = cached.get("escalation", False),
        #     )
        #     logger.info("Serving cached response for: %.80s", polished_query)
        # else:
        # 4. RAG retrieval (run in thread pool — blocking SDK call)
        chunks = await asyncio.get_event_loop().run_in_executor(
            None, self.rag.query, polished_query
        )
        retrieved_context = self.rag.format_for_prompt(chunks)

        # 5. Build prompt components
        customer_summary    = self.memory.build_customer_summary(state)
        conversation_history = self.memory.format_history_for_prompt(state)

        # 6. Call Gemini
        bot_response: BotResponse = await self.llm.chat(
            customer_summary     = customer_summary,
            conversation_history = conversation_history,
            user_query           = text,
            retrieved_context    = retrieved_context,
            is_frustrated        = is_frustrated,
        )

        # 6.2 Output Moderation (RAG leakage protection)
        out_mod_result = await self.moderator.analyze_message(bot_response.answer)
        out_severity = out_mod_result.get("severity", "none")
        if out_severity in ["targeted_abuse", "frustration"]:
            logger.warning("Blocked unsafe bot output: %s", bot_response.answer)
            bot_response.answer = "I apologize, but I am unable to process this request at the moment. Please let me know if you need help with anything else."


        # 6.5 Cache the response (skips escalation responses automatically)
        # await self.cache.set(polished_query, {
        #     "answer":     bot_response.answer,
        #     "escalation": bot_response.escalation,
        # })

        # 7. Quality audit (async, doesn't block the reply)
        if self.evaluator and self.sheets_logger:
            asyncio.create_task(
                self._evaluate_and_log_generic(
                    user_id=user_id,
                    original_text=text,
                    message_id=message_id,
                    polished_query=polished_query,
                    bot_response=bot_response,
                    retrieved_context=retrieved_context,
                )
            )

        # 8. Persist turn to Firestore
        updated_state = await self.memory.append_turn(
            phone        = user_id,
            display_name = display_name,
            user_text    = text,
            bot_text     = bot_response.answer,
        )

        # 9. Escalation handling
        if bot_response.escalation:
            await self._handle_escalation_generic(
                user_id=user_id,
                display_name=display_name,
                text=text,
                bot_response=bot_response,
                state=state,
                channel=channel,
                phone_number_id=phone_number_id,
                bot_phone_number=bot_phone_number,
            )

        # 10. Rolling summary compression (async, doesn't block the reply)
        if self.memory.should_summarise(updated_state):
            asyncio.create_task(
                self._compress_summary(user_id, updated_state)
            )

        return bot_response

    # ══════════════════════════════════════════════════════════════════════════
    #  TRIP QUERY HANDLING
    # ══════════════════════════════════════════════════════════════════════════

    @staticmethod
    def _is_trip_query(text: str, polished_query: str) -> bool:
        combined = (text + " " + polished_query).lower()
        return any(kw in combined.split() or kw in combined for kw in _TRIP_KEYWORDS)

    async def _notify_trips_coordinator(
        self,
        user_id:        str,
        display_name:   str,
        text:           str,
        channel:        str,
        phone_number_id: str = None,
    ):
        trips_number = os.environ.get("TRIPS_CONTACT_NUMBER", "917973578469")
        if not self.wa:
            return

        if channel == "whatsapp":
            user_link = f"https://wa.me/{user_id}"
            user_ref = f"Quick reply: {user_link}"
        else:
            user_ref = f"App user ID: {user_id}"

        alert = (
            f"🧳 *New Trip Enquiry*\n\n"
            f"*Name:* {display_name}\n"
            f"*Query:* {text}\n\n"
            f"{user_ref}"
        )
        await self.wa.send_text(
            to=trips_number,
            body=alert,
            phone_number_id=phone_number_id or self.wa.phone_number_id,
        )

    # ══════════════════════════════════════════════════════════════════════════
    #  ESCALATION (channel-aware)
    # ══════════════════════════════════════════════════════════════════════════

    async def _handle_escalation_generic(
        self,
        user_id:          str,
        display_name:     str,
        text:             str,
        bot_response:     BotResponse,
        state:            dict,
        channel:          str,
        phone_number_id:  str = None,
        bot_phone_number: str = None,
    ):
        """
        Called when the bot flags a conversation for human review.
        Works for both WhatsApp and in-app channels.
        """
        logger.warning(
            "ESCALATION REQUIRED | channel=%s | user=%s | name=%s | query=%s | turn=%d",
            channel,
            user_id,
            display_name,
            text[:120],
            state.get("turn_count", 0),
        )
        
        # 1. Pause the bot for this user
        await self.memory.set_escalation_status(user_id, True)

        # 2. Alert the Admin via WhatsApp (admin always uses WhatsApp)
        admin_phone = os.environ.get("ADMIN_PHONE_NUMBER")
        if admin_phone and self.wa:
            channel_label = "WhatsApp" if channel == "whatsapp" else "In-App"

            if channel == "whatsapp" and bot_phone_number:
                clean_bot_phone = "".join(filter(str.isdigit, bot_phone_number))
                resolve_link = f"https://wa.me/{clean_bot_phone}?text=%2Fresolve%20{user_id}"
            else:
                resolve_link = f"(Send `/resolve {user_id}` to the bot via WhatsApp)"

            alert_text = (
                f"🚨 *ESCALATION REQUIRED* [{channel_label}]\n"
                f"User: {display_name} ({user_id})\n\n"
                f"Issue: {text}\n\n"
                f"Bot replied: {bot_response.answer}\n\n"
                f"To unpause the bot for this user:\n"
                f"{resolve_link}"
            )
            await self.wa.send_text(
                to=admin_phone,
                body=alert_text,
                phone_number_id=phone_number_id or self.wa.phone_number_id,
            )

    # ── Rolling summary compression ───────────────────────────────────────────

    async def _compress_summary(self, phone: str, state: dict):
        """
        Ask Gemini to compress recent turns into a tighter rolling summary.
        This keeps the CUSTOMER_SUMMARY block concise as conversations grow.
        """
        history_text = self.memory.format_history_for_prompt(state)
        existing     = state.get("summary", "")

        compression_prompt = f"""
You are summarising a customer service conversation for GoHappy Club.

EXISTING SUMMARY (may be empty):
{existing or "(none)"}

RECENT CONVERSATION:
{history_text}

Write a new, concise summary (3–5 sentences max) covering:
- Who the customer is and what they care about
- Any unresolved issues or follow-up items
- Key facts established (membership plan, preferences, etc.)

Output only the summary text. No preamble. No bullet points.
""".strip()

        try:
            response = await self.llm.summary_model.generate_content_async(compression_prompt)
            new_summary = response.text.strip()
            await self.memory.update_summary(phone, new_summary)
            logger.info("Summary compressed for %s (%d chars)", phone, len(new_summary))
        except Exception as exc:
            logger.error("Summary compression failed for %s: %s", phone, exc)

    # ── Quality audit (channel-agnostic) ──────────────────────────────────────
    
    async def _evaluate_and_log_generic(
        self,
        user_id:           str,
        original_text:     str,
        message_id:        str,
        polished_query:    str,
        bot_response:      BotResponse,
        retrieved_context: str,
    ):
        """
        Runs the Gemini grader on the bot's response and appends to Google Sheets.
        This entire task is fire-and-forget. Works for both channels.
        """
        import datetime
        
        logger.info("Starting background quality audit for %s...", user_id)
        
        # 1. Run LLM evaluation
        result = await self.evaluator.evaluate(
            original_query=original_text,
            polished_query=polished_query,
            bot_answer=bot_response.answer,
            retrieved_context=retrieved_context,
        )
        
        if not result:
            return # Grader failed, nothing to log

        # 2. Append to Sheets
        timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
        
        await self.sheets_logger.log_to_audit_sheet(
            timestamp=timestamp,
            phone=user_id,
            original_query=original_text,
            polished_query=polished_query,
            bot_answer=bot_response.answer,
            accuracy=result.accuracy_score,
            hallucination=result.hallucination_check,
            escalation=result.required_escalation,
            empathy=result.empathy_score,
            reasoning=result.reasoning,
            message_id=message_id,
        )

    # ── Legacy quality audit wrapper (kept for backward compatibility) ────────
    
    async def _evaluate_and_log(
        self,
        msg:               IncomingMessage,
        polished_query:    str,
        bot_response:      BotResponse,
        retrieved_context: str,
    ):
        """Legacy wrapper that delegates to the channel-agnostic version."""
        await self._evaluate_and_log_generic(
            user_id=msg.from_number,
            original_text=msg.text,
            message_id=msg.wa_message_id,
            polished_query=polished_query,
            bot_response=bot_response,
            retrieved_context=retrieved_context,
        )

    # ── KB Insights Trigger ──────────────────────────────────────────────────
    async def _run_insights_generator(self, msg: IncomingMessage):
        """
        Background task to generate KB Insights.
        """
        if self.kb_insights:
            result_text = await self.kb_insights.generate_insights()
            await self.wa.send_text(
                to=msg.from_number,
                body=result_text,
                phone_number_id=msg.phone_number_id
            )
        else:
            logger.error("KBInsightsGenerator not initialized.")

    # ── KB Automation Handlers ───────────────────────────────────────────────
    async def _handle_update_kb(self, msg: IncomingMessage):
        if not self.kb_manager:
            logger.error("KnowledgeBaseManager not initialized.")
            return

        admin_input = msg.text[len("/update_kb "):].strip()
        try:
            new_kb = await self.kb_manager.generate_update(admin_input)
            await self.kb_manager.save_pending_update(new_kb)
            
            # Save file locally to upload to whatsapp
            import os
            file_path = "/tmp/GoHappyClub_KnowledgeBase.md"
            with open(file_path, "w") as f:
                f.write(new_kb)

            # Upload to WhatsApp Media
            media_id = await self.wa.upload_media(file_path, mime_type="text/plain")
            if media_id:
                await self.wa.send_document(
                    to=msg.from_number,
                    media_id=media_id,
                    filename="Updated_KB_Draft.md",
                    caption="📄 Here is the updated Knowledge Base draft. Please review it. Reply with `/approve_kb` to confirm and sync to Vertex AI, or send another `/update_kb` command to make further changes."
                )
            else:
                await self.wa.send_text(
                    to=msg.from_number,
                    body="❌ Failed to upload the document to WhatsApp. Check server logs.",
                    phone_number_id=msg.phone_number_id
                )
        except Exception as exc:
            logger.error("Error in KB update: %s", exc, exc_info=True)
            await self.wa.send_text(
                to=msg.from_number,
                body=f"❌ Failed to process KB update: {exc}",
                phone_number_id=msg.phone_number_id
            )

    async def _handle_approve_kb(self, msg: IncomingMessage):
        if not self.kb_manager:
            logger.error("KnowledgeBaseManager not initialized.")
            return
            
        success = await self.kb_manager.approve_and_sync()
        if success:
            await self.wa.send_text(
                to=msg.from_number,
                body="✅ Successfully approved! The new Knowledge Base has been synced to Vertex AI RAG Corpus.",
                phone_number_id=msg.phone_number_id
            )
        else:
            await self.wa.send_text(
                to=msg.from_number,
                body="❌ Failed to sync to Vertex AI RAG Corpus. Please check the server logs.",
                phone_number_id=msg.phone_number_id
            )

    async def _handle_insights_update(self, msg: IncomingMessage):
        if not self.kb_insights or not self.kb_manager:
            await self.wa.send_text(
                to=msg.from_number,
                body="❌ KB Insights or KB Manager not initialised.",
                phone_number_id=msg.phone_number_id
            )
            return

        try:
            insight_text = await self.kb_insights.get_latest_insight()
        except ValueError as e:
            await self.wa.send_text(
                to=msg.from_number,
                body=f"❌ No insights found: {e}",
                phone_number_id=msg.phone_number_id
            )
            return
        except Exception as e:
            logger.error("Failed to fetch latest insight: %s", e, exc_info=True)
            await self.wa.send_text(
                to=msg.from_number,
                body=f"❌ Failed to read insights sheet: {e}",
                phone_number_id=msg.phone_number_id
            )
            return

        try:
            update_instruction = (
                "Based on the following insights from analyzing recent customer conversations, "
                "update the knowledge base to address the identified gaps. "
                "Add any missing facts, policies, or instructions suggested below. "
                "Do NOT remove any existing content unless it directly contradicts the insight.\n\n"
                f"INSIGHTS:\n{insight_text}"
            )
            new_kb = await self.kb_manager.generate_update(update_instruction)
        except Exception as e:
            logger.error("Gemini KB update generation failed: %s", e, exc_info=True)
            await self.wa.send_text(
                to=msg.from_number,
                body=f"❌ Failed to generate KB update: {e}",
                phone_number_id=msg.phone_number_id
            )
            return

        try:
            await self.kb_manager.save_pending_update(new_kb)
            success = await self.kb_manager.approve_and_sync()
        except Exception as e:
            logger.error("RAG sync failed: %s", e, exc_info=True)
            await self.wa.send_text(
                to=msg.from_number,
                body=f"❌ Failed to sync to RAG: {e}",
                phone_number_id=msg.phone_number_id
            )
            return

        if success:
            await self.wa.send_text(
                to=msg.from_number,
                body="✅ Knowledge base successfully updated from latest insights and synced to RAG.",
                phone_number_id=msg.phone_number_id
            )
        else:
            await self.wa.send_text(
                to=msg.from_number,
                body="❌ RAG sync returned failure. Check server logs.",
                phone_number_id=msg.phone_number_id
            )
