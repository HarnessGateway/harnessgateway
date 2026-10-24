"""Mail over SMTP: `smtp://user:pass@host:587` (STARTTLS when offered) or `smtps://…` (TLS from the
start)."""
from __future__ import annotations

import smtplib
from email.message import EmailMessage
from urllib.parse import urlsplit


class SmtpMail:
    enabled = True

    def __init__(self, url: str, sender: str):
        u = urlsplit(url)
        if u.scheme not in ("smtp", "smtps") or not u.hostname:
            raise ValueError("HG_SMTP_URL is smtp://user:pass@host:port or smtps://…")
        self.url, self.sender, self._u = url, sender, u

    def send(self, to: str, subject: str, text: str) -> None:
        u = self._u
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = self.sender or f"no-reply@{u.hostname}", to, subject
        msg.set_content(text)
        server = smtplib.SMTP_SSL(u.hostname, u.port or 465, timeout=20) if u.scheme == "smtps" else smtplib.SMTP(u.hostname, u.port or 587, timeout=20)
        with server:
            if u.scheme == "smtp":
                server.ehlo()
                if server.has_extn("starttls"):
                    server.starttls()
                    server.ehlo()
            if u.username:
                server.login(u.username, u.password or "")
            server.send_message(msg)
