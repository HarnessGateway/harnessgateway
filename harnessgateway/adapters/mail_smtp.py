"""Mail over SMTP from the standard library: `smtp://user:pass@host:port` (STARTTLS when the server offers it) or
`smtps://…` (TLS from the first byte)."""
from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage
from urllib.parse import unquote, urlsplit


class SmtpMailer:
    def __init__(self, url: str, sender: str):
        u = urlsplit(url)
        if u.scheme not in ("smtp", "smtps") or not u.hostname:
            raise ValueError("HG_SMTP_URL is smtp://[user:password@]host[:port] or smtps://…")
        self.host, self.port, self.tls = u.hostname, u.port or (465 if u.scheme == "smtps" else 587), u.scheme == "smtps"
        self.user, self.password = unquote(u.username or ""), unquote(u.password or "")
        self.sender = sender

    def send(self, to: str, subject: str, body: str) -> None:
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = self.sender, to, subject
        msg.set_content(body)
        context = ssl.create_default_context()
        with (smtplib.SMTP_SSL(self.host, self.port, context=context, timeout=30) if self.tls
              else smtplib.SMTP(self.host, self.port, timeout=30)) as smtp:
            if not self.tls:
                smtp.ehlo()
                if smtp.has_extn("starttls"):
                    smtp.starttls(context=context)
            if self.user:
                smtp.login(self.user, self.password)
            smtp.send_message(msg)
