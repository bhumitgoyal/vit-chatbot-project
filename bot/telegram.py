"""
bot/telegram.py
Thin Telegram Bot API client + Markdown→Telegram-HTML conversion.

Env:
  TELEGRAM_BOT_TOKEN      — from @BotFather (required to enable the bot)
  TELEGRAM_WEBHOOK_SECRET — shared secret; Telegram echoes it in the
                            X-Telegram-Bot-Api-Secret-Token header on every update
  TELEGRAM_WEBHOOK_URL    — public https URL of POST /telegram/webhook
                            (if set, the webhook is registered on startup)
"""

import os
import re
import html
import base64
import logging
from typing import Optional, List

import requests

logger = logging.getLogger("vit.telegram")

API = "https://api.telegram.org/bot{token}/{method}"
MAX_LEN = 3900  # Telegram hard limit is 4096; leave headroom for tags


def enabled() -> bool:
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN"))


def _token() -> str:
    return os.environ["TELEGRAM_BOT_TOKEN"]


def _call(method: str, **kwargs) -> dict:
    try:
        r = requests.post(API.format(token=_token(), method=method), timeout=20, **kwargs)
        data = r.json()
        if not data.get("ok"):
            logger.warning(f"Telegram {method} failed: {data}")
        return data
    except Exception as e:
        logger.warning(f"Telegram {method} error: {e}")
        return {"ok": False, "error": str(e)}


# ── Markdown → Telegram HTML ──────────────────────────────────────────────
def to_telegram_html(md: str) -> str:
    """Best-effort, always-valid conversion. Telegram HTML only needs & < >
    escaped and a handful of balanced tags."""
    # links: [text](url) / [text](mailto:x)  →  text (url)   (before escaping)
    md = re.sub(r"\[([^\]]+)\]\((?:mailto:)?([^)]+)\)", r"\1 (\2)", md)

    out_lines: List[str] = []
    for ln in md.splitlines():
        s = ln.rstrip()
        st = s.strip()
        if re.fullmatch(r"\|?[\s:\-|]+\|?", st) and "-" in st:   # table separator row
            continue
        if st.startswith("|") and st.endswith("|"):              # table row → plain
            s = " · ".join(c.strip() for c in st.strip("|").split("|"))
        s = re.sub(r"^\s*>\s?", "", s)                           # blockquote marker
        s = re.sub(r"^\s*[-*]\s+", "• ", s)                      # bullets
        out_lines.append(s)
    md = "\n".join(out_lines)

    md = html.escape(md, quote=False)
    md = re.sub(r"\*\*([^*\n]+)\*\*", r"<b>\1</b>", md)          # **bold**
    md = re.sub(r"(?m)^\s*#{1,6}\s*(.+?)\s*$", r"<b>\1</b>", md)  # ### heading
    md = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", md)          # `code`
    md = re.sub(r"\n{3,}", "\n\n", md)
    return md.strip()


def _chunks(text: str) -> List[str]:
    if len(text) <= MAX_LEN:
        return [text]
    parts, buf = [], ""
    for line in text.split("\n"):
        if len(buf) + len(line) + 1 > MAX_LEN:
            if buf:
                parts.append(buf)
            buf = line[:MAX_LEN]
        else:
            buf = f"{buf}\n{line}" if buf else line
    if buf:
        parts.append(buf)
    return parts


# ── public helpers ───────────────────────────────────────────────────────
def send_message(chat_id: int, markdown: str) -> None:
    html_text = to_telegram_html(markdown)
    for part in _chunks(html_text):
        res = _call("sendMessage", json={
            "chat_id": chat_id, "text": part,
            "parse_mode": "HTML", "disable_web_page_preview": True,
        })
        if not res.get("ok"):  # bad markup → resend as plain text
            _call("sendMessage", json={
                "chat_id": chat_id,
                "text": re.sub(r"<[^>]+>", "", part),
                "disable_web_page_preview": True,
            })


def send_photo(chat_id: int, data_uri: str, caption: str = "") -> None:
    try:
        b64 = data_uri.split(",", 1)[1] if data_uri.startswith("data:") else data_uri
        img = base64.b64decode(b64)
    except Exception as e:
        logger.warning(f"send_photo decode failed: {e}")
        send_message(chat_id, caption or "CAPTCHA image unavailable.")
        return
    _call("sendPhoto",
          data={"chat_id": chat_id,
                "caption": to_telegram_html(caption)[:1024], "parse_mode": "HTML"},
          files={"photo": ("captcha.jpg", img, "image/jpeg")})


def send_chat_action(chat_id: int, action: str = "typing") -> None:
    _call("sendChatAction", json={"chat_id": chat_id, "action": action})


def set_webhook(url: str, secret: str) -> dict:
    return _call("setWebhook", json={
        "url": url,
        "secret_token": secret,
        "allowed_updates": ["message"],
        "drop_pending_updates": True,
    })


def delete_webhook() -> dict:
    return _call("deleteWebhook", json={"drop_pending_updates": True})


# ── update parsing ───────────────────────────────────────────────────────
def parse_update(update: dict) -> Optional[dict]:
    """Extract the bits we care about from a Telegram update, or None to skip."""
    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return None
    chat = msg.get("chat") or {}
    text = (msg.get("text") or msg.get("caption") or "").strip()
    if not text or not chat.get("id"):
        return None
    return {
        "update_id": update.get("update_id"),
        "chat_id": chat["id"],
        "user_id": f"tg{chat['id']}",
        "text": text,
        "first_name": (msg.get("from") or {}).get("first_name", ""),
    }
