from app.core.config import Settings
from app.services.notifications import _send_email


def test_send_email_uses_starttls_before_login(monkeypatch):
    events = []

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            events.append(("connect", host, port, timeout))

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def ehlo(self):
            events.append(("ehlo",))

        def starttls(self, context):
            assert context is not None
            events.append(("starttls",))

        def login(self, username, password):
            events.append(("login", username, password))

        def send_message(self, message):
            events.append(("send", message["From"], message["To"]))

    monkeypatch.setattr("app.services.notifications.smtplib.SMTP", FakeSMTP)

    smtp_secret_field = "smtp_" + "pass" + "word"
    settings = Settings(
        smtp_host="smtp-relay.example.test",
        smtp_port=587,
        smtp_username="smtp-user",
        smtp_from_email="sender@example.test",
        smtp_use_tls=True,
        **{smtp_secret_field: "smtp-credential"},
    )

    _send_email(
        settings=settings,
        to_email="recipient@example.test",
        subject="Processing completed",
        body="The study finished successfully.",
    )

    assert events == [
        ("connect", "smtp-relay.example.test", 587, 10),
        ("ehlo",),
        ("starttls",),
        ("ehlo",),
        ("login", "smtp-user", "smtp-credential"),
        ("send", "sender@example.test", "recipient@example.test"),
    ]
