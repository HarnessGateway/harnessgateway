"""Backend: outgoing mail, for verifying a sign-up's email and resetting a password. `none` sends
nothing, and sign-ups are verified at once; `smtp` sends over the URL the deployer set."""
from __future__ import annotations

from typing import Protocol


class Mailer(Protocol):
    @property
    def enabled(self) -> bool: ...
    def send(self, to: str, subject: str, text: str) -> None: ...


def open_mailer(smtp_url: str, sender: str) -> Mailer:
    if smtp_url:
        from .smtp import SmtpMail
        return SmtpMail(smtp_url, sender)
    from .none import NoMail
    return NoMail()
