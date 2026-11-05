"""The SMTP mailer's address parsing (SPEC §9.3): what HG_SMTP_URL may be."""
import pytest

from harnessgateway.adapters.mail_smtp import SmtpMailer


def test_smtp_urls_name_host_port_credentials_and_tls():
    m = SmtpMailer("smtp://user%40example.com:p%40ss@mail.example.com", "HarnessGateway <noreply@example.com>")
    assert (m.host, m.port, m.tls, m.user, m.password) == ("mail.example.com", 587, False, "user@example.com", "p@ss")
    m = SmtpMailer("smtps://mail.example.com", "noreply@example.com")
    assert (m.port, m.tls, m.user) == (465, True, "")
    with pytest.raises(ValueError):
        SmtpMailer("http://mail.example.com", "noreply@example.com")
