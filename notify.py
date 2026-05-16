"""Direct Telegram Bot API notifier.

Sends a single message via HTTPS. Requires TELEGRAM_BOT_TOKEN to be set;
if it isn't, send() logs a warning and returns False.
"""

from __future__ import annotations

import logging
import urllib.parse
import urllib.request

import config

log = logging.getLogger(__name__)


def send(chat_id: str, text: str, reply_to: int | None = None) -> bool:
    """Send a Telegram message. Returns True on ok."""
    token = config.TELEGRAM_BOT_TOKEN
    if not token:
        log.warning("TELEGRAM_BOT_TOKEN not set; skipping notification")
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = {"chat_id": chat_id, "text": text}
    if reply_to is not None:
        data["reply_to_message_id"] = str(reply_to)
    body = urllib.parse.urlencode(data).encode("utf-8")
    try:
        with urllib.request.urlopen(url, data=body, timeout=15) as resp:
            return resp.status == 200
    except Exception as e:
        log.warning("telegram send failed: %s", e)
        return False
