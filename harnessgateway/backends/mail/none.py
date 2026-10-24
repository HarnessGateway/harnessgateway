"""No mail: nothing is sent, and the sign-in treats every email as verified."""
from __future__ import annotations


class NoMail:
    enabled = False

    def send(self, to: str, subject: str, text: str) -> None:
        raise RuntimeError("mail is not configured: set HG_SMTP_URL")
