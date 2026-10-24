"""Conformance: no mail is disabled and refuses to send; SMTP parses its URL and refuses a bad one."""
import pytest

from harnessgateway.backends.mail import open_mailer
from harnessgateway.backends.mail.none import NoMail
from harnessgateway.backends.mail.smtp import SmtpMail


def test_none_and_smtp():
    none = open_mailer("", "")
    assert isinstance(none, NoMail) and not none.enabled
    with pytest.raises(RuntimeError):
        none.send("a@x.test", "s", "t")
    smtp = open_mailer("smtps://user:pw@mail.test:465", "hg@test")
    assert isinstance(smtp, SmtpMail) and smtp.enabled and smtp._u.port == 465
    for bad in ("mail.test", "http://mail.test", "smtp://"):
        with pytest.raises(ValueError):
            SmtpMail(bad, "")
