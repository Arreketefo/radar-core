"""Notification TRANSPORTS shared by every radar.

What a message says is domain (each app renders its own alerts); how it is
delivered lives here. Rendering lessons already encoded app-side: never emit
`/word` in Telegram text (renders as a bot command) and use stats.drop_display
for signed percentages.
"""

from __future__ import annotations

import asyncio
import smtplib
from email.message import EmailMessage
from urllib.parse import unquote, urlsplit

import httpx


def escape_markdown(text: str) -> str:
    """Minimal escaping for Telegram's legacy Markdown parse_mode."""
    for char in ("_", "*", "[", "`"):
        text = text.replace(char, f"\\{char}")
    return text


class TelegramTransport:
    """Bare sendMessage transport; the app builds the text."""

    def __init__(self, bot_token: str, chat_id: str) -> None:
        self._url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        self._chat_id = chat_id
        self._client = httpx.AsyncClient(timeout=10.0)

    async def send_text(self, text: str, markdown: bool = True) -> None:
        payload: dict[str, object] = {
            "chat_id": self._chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if markdown:
            payload["parse_mode"] = "Markdown"
        resp = await self._client.post(self._url, json=payload)
        resp.raise_for_status()

    async def aclose(self) -> None:
        await self._client.aclose()


class SmtpSender:
    """SMTP delivery. smtp_url: smtp://user:pass@host:587 (STARTTLS when
    offered) or smtps://user:pass@host:465 (implicit TLS)."""

    def __init__(self, smtp_url: str, to_addr: str, app_name: str = "radar") -> None:
        parts = urlsplit(smtp_url)
        if parts.scheme not in ("smtp", "smtps") or not parts.hostname:
            raise ValueError(f"invalid smtp_url: {smtp_url!r}")
        self._host = parts.hostname
        self._port = parts.port or (465 if parts.scheme == "smtps" else 587)
        self._ssl = parts.scheme == "smtps"
        self._user = unquote(parts.username) if parts.username else None
        self._password = unquote(parts.password) if parts.password else None
        # app_name keeps each radar's historical sender (vigia@host, ...):
        # relays with sender allowlists care about this.
        self.from_addr = self._user or f"{app_name}@{self._host}"
        self._to = to_addr

    async def send(self, subject: str, body: str) -> None:
        await asyncio.to_thread(self._send_sync, subject, body)

    def _send_sync(self, subject: str, body: str) -> None:
        msg = EmailMessage()
        msg["From"] = self.from_addr
        msg["To"] = self._to
        msg["Subject"] = subject
        msg.set_content(body)
        smtp_cls = smtplib.SMTP_SSL if self._ssl else smtplib.SMTP
        with smtp_cls(self._host, self._port, timeout=15) as smtp:
            if not self._ssl:
                smtp.ehlo()
                # Plain local relays (port 25) may not offer STARTTLS.
                if smtp.has_extn("starttls"):
                    smtp.starttls()
                    smtp.ehlo()
            if self._user and self._password:
                smtp.login(self._user, self._password)
            smtp.send_message(msg)
