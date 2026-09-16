"""
Telegram Notifier - Send notifications about account creation status
"""
import html
import logging
import re
import requests

from core.secret_safety import redact_text

logger = logging.getLogger('gmail_creator_telegram')


_SAFE_HTML_TOKEN = re.compile(
    r"</?(?:b|strong|i|em|code|pre|u|s|strike|tg-spoiler|blockquote)>"
    r"|&(?:amp|lt|gt|quot|#x27|#39);",
    re.IGNORECASE,
)


def _escape_dynamic(value, secrets=()):
    """Redact credential forms before putting a value into Telegram HTML."""
    return html.escape(redact_text(value, secrets=secrets), quote=True)


def _sanitize_message(value, secrets=()):
    """Keep only the small HTML tag subset used by our fixed templates."""
    text = redact_text(value, secrets=secrets)
    chunks = []
    cursor = 0
    for match in _SAFE_HTML_TOKEN.finditer(text):
        chunks.append(html.escape(text[cursor:match.start()], quote=True))
        chunks.append(match.group(0))
        cursor = match.end()
    chunks.append(html.escape(text[cursor:], quote=True))
    return "".join(chunks)


class TelegramNotifier:
    def __init__(self, bot_token=None, chat_id=None):
        from config.settings import Config
        self.bot_token = bot_token or getattr(Config, 'TELEGRAM_BOT_TOKEN', '')
        self.chat_id = chat_id or getattr(Config, 'TELEGRAM_CHAT_ID', '')
        self.enabled = bool(self.bot_token and self.chat_id)

    def send(self, message, silent=False):
        if not self.enabled:
            return False
        try:
            url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
            safe_message = _sanitize_message(
                message, secrets=(self.bot_token, self.chat_id)
            )
            resp = requests.post(url, json={
                "chat_id": self.chat_id,
                "text": safe_message,
                "parse_mode": "HTML",
                "disable_notification": silent,
            }, timeout=10)
            return resp.status_code == 200
        except Exception as e:
            logger.warning("Telegram send failed: %s", type(e).__name__)
            return False

    def notify_account_created(self, email, password=None, strategy="", proxy=""):
        """Notify about creation without sending credentials or proxy userinfo.

        ``password`` and ``proxy`` remain accepted for old callers, but are
        deliberately ignored so a stale integration cannot reintroduce the
        secret-bearing message format.
        """
        msg = (
            f"<b>Account Created</b>\n"
            f"<b>Email:</b> <code>{_escape_dynamic(email, (self.bot_token, self.chat_id))}</code>\n"
        )
        if strategy:
            msg += (
                f"<b>Strategy:</b> "
                f"{_escape_dynamic(strategy, (self.bot_token, self.chat_id))}\n"
            )
        return self.send(msg)

    def notify_account_failed(self, username, error_type="", strategy=""):
        msg = (
            f"<b>Account Failed</b>\n"
            f"<b>Username:</b> "
            f"{_escape_dynamic(username, (self.bot_token, self.chat_id))}\n"
        )
        if error_type:
            msg += (
                f"<b>Error:</b> "
                f"{_escape_dynamic(error_type, (self.bot_token, self.chat_id))}\n"
            )
        if strategy:
            msg += (
                f"<b>Strategy:</b> "
                f"{_escape_dynamic(strategy, (self.bot_token, self.chat_id))}\n"
            )
        return self.send(msg, silent=True)

    def notify_batch_complete(self, total, successes, failures, duration):
        rate = (successes / total * 100) if total > 0 else 0
        msg = (
            f"<b>Batch Complete</b>\n"
            f"Total: {total} | Success: {successes} | Failed: {failures}\n"
            f"Rate: {rate:.1f}% | Duration: {duration:.0f}s"
        )
        return self.send(msg)

    def test_connection(self):
        if not self.enabled:
            return False, "Bot token or chat ID not configured"
        try:
            url = f"https://api.telegram.org/bot{self.bot_token}/getMe"
            resp = requests.get(url, timeout=10)
            if resp.status_code == 200:
                bot_name = resp.json().get("result", {}).get("username", "Unknown")
                return True, f"Connected to @{bot_name}"
            return False, f"API error: {resp.status_code}"
        except Exception as e:
            return False, "Telegram connectivity check failed (%s)" % type(e).__name__


notifier = TelegramNotifier()
