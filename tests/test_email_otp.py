from datetime import UTC, datetime, timedelta
from email.message import EmailMessage

import pytest

from mirea_lecture_assistant.email_otp import (
    EmailAccount,
    ImapOtpReader,
    extract_fresh_otp,
    infer_email_provider,
)


def message(subject: str, body: str, sent_at: datetime, sender: str = "noreply@mirea.ru") -> bytes:
    item = EmailMessage()
    item["Subject"] = subject
    item["From"] = sender
    item["Date"] = sent_at
    item.set_content(body)
    return item.as_bytes()


def test_extracts_contextual_fresh_code():
    now = datetime.now(UTC)
    raw = message("Подтверждение входа", "Код подтверждения: 482913", now)
    assert extract_fresh_otp(raw, now) == "482913"


def test_extracts_mirea_hash_bc_email_and_keeps_leading_zero():
    now = datetime.now(UTC)
    raw = message(
        "012345 – ваш код #BC для входа в учётную запись РТУ МИРЭА",
        "Введите код 012345 (#BC) для подтверждения входа.",
        now,
        "sso@mirea.ru",
    )
    assert extract_fresh_otp(raw, now) == "012345"


def test_ignores_stale_code():
    now = datetime.now(UTC)
    raw = message("Код", "Код подтверждения: 482913", now - timedelta(minutes=5))
    assert extract_fresh_otp(raw, now) is None


def test_rejects_previous_code_even_if_it_is_less_than_a_minute_old():
    now = datetime.now(UTC)
    raw = message("Код", "Код подтверждения: 482913", now - timedelta(seconds=20))
    assert extract_fresh_otp(raw, now) is None


def test_ignores_unrelated_number():
    now = datetime.now(UTC)
    raw = message("Заказ", "Номер заказа 482913", now, "shop@example.org")
    assert extract_fresh_otp(raw, now) is None


def test_otp_polling_recovers_after_a_temporary_imap_failure(monkeypatch):
    now = datetime.now(UTC)
    raw = message("Подтверждение входа", "Код подтверждения: 482913", now)
    attempts = []

    class Mailbox:
        def __init__(self, *_args, **_kwargs):
            attempts.append(1)
            if len(attempts) == 1:
                raise OSError("temporary network failure")

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def login(self, *_args):
            return None

        def select(self, *_args, **_kwargs):
            return "OK", []

        def uid(self, command, *_args):
            if command == "search":
                return "OK", [b"1"]
            return "OK", [(b"header", raw)]

    from mirea_lecture_assistant import email_otp

    monkeypatch.setattr(email_otp.imaplib, "IMAP4_SSL", Mailbox)
    monkeypatch.setattr(email_otp.time, "sleep", lambda _seconds: None)

    account = EmailAccount("mail@gmail.com", "password").normalized()
    assert ImapOtpReader().wait_for_code(account, now, timeout=1) == "482913"
    assert len(attempts) == 2


def test_bad_gmail_app_password_is_reported_without_endless_polling(monkeypatch):
    from mirea_lecture_assistant import email_otp

    class Mailbox:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def login(self, *_args):
            raise email_otp.imaplib.IMAP4.error("AUTHENTICATION FAILED")

    monkeypatch.setattr(email_otp.imaplib, "IMAP4_SSL", Mailbox)

    with pytest.raises(RuntimeError, match="Gmail отклонил"):
        ImapOtpReader().wait_for_code(
            EmailAccount("mail@gmail.com", "wrong-password"), datetime.now(UTC), timeout=1
        )


def test_connection_check_translates_yandex_authentication_error(monkeypatch):
    from mirea_lecture_assistant import email_otp

    class Mailbox:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def login(self, *_args):
            raise email_otp.imaplib.IMAP4.error(
                b"[AUTHENTICATIONFAILED] LOGIN invalid credentials or IMAP is disabled"
            )

    monkeypatch.setattr(email_otp.imaplib, "IMAP4_SSL", Mailbox)

    with pytest.raises(RuntimeError, match="Яндекс.*пароль приложения"):
        ImapOtpReader().latest_uid(EmailAccount("mail@yandex.ru", "wrong-password"))


@pytest.mark.parametrize(
    ("address", "provider", "host"),
    [
        ("student@gmail.com", "gmail", "imap.gmail.com"),
        ("student@yandex.ru", "yandex", "imap.yandex.ru"),
        ("student@mail.ru", "mailru", "imap.mail.ru"),
        ("student@outlook.com", "microsoft", "outlook.office365.com"),
    ],
)
def test_common_email_providers_are_detected(address, provider, host):
    assert infer_email_provider(address) == provider
    account = EmailAccount(address, "app-password").normalized()
    assert account.provider == provider
    assert account.imap_host == host
    assert account.imap_port == 993


def test_unknown_provider_accepts_a_custom_secure_imap_server():
    account = EmailAccount(
        "student@example.org", "password with spaces", "custom", "imap.example.org", 1993
    ).normalized()

    assert account.imap_host == "imap.example.org"
    assert account.imap_port == 1993
    assert account.password == "password with spaces"


def test_unknown_provider_requires_a_custom_server():
    with pytest.raises(ValueError, match="IMAP-сервер"):
        EmailAccount("student@example.org", "password").normalized()


def test_personal_yandex_uses_login_before_full_address():
    account = EmailAccount("artem@yandex.ru", " app-password ").normalized()

    assert account.imap_usernames() == ("artem", "artem@yandex.ru")
    assert account.password == "app-password"


def test_yandex_uses_explicit_imap_login_for_an_alias():
    account = EmailAccount(
        "alias@yandex.ru", "app-password", imap_username=" main-login "
    ).normalized()

    assert account.imap_usernames() == ("main-login",)
    assert account.imap_username == "main-login"


def test_yandex_falls_back_to_full_address_when_login_is_rejected(monkeypatch):
    from mirea_lecture_assistant import email_otp

    usernames = []

    class Mailbox:
        def __init__(self, *_args, **_kwargs):
            pass

        def login(self, username, _password):
            usernames.append(username)
            if username == "artem":
                raise email_otp.imaplib.IMAP4.error(b"[AUTHENTICATIONFAILED] invalid credentials")

        def shutdown(self):
            return None

        def logout(self):
            return None

        def select(self, *_args, **_kwargs):
            return "OK", []

        def uid(self, *_args):
            return "OK", [b"7"]

    monkeypatch.setattr(email_otp.imaplib, "IMAP4_SSL", Mailbox)

    uid = ImapOtpReader().latest_uid(EmailAccount("artem@yandex.ru", "app-password"))

    assert uid == 7
    assert usernames == ["artem", "artem@yandex.ru"]


def test_connection_check_uses_explicit_yandex_login(monkeypatch):
    from mirea_lecture_assistant import email_otp

    usernames = []

    class Mailbox:
        def __init__(self, *_args, **_kwargs):
            pass

        def login(self, username, _password):
            usernames.append(username)

        def logout(self):
            return None

        def select(self, *_args, **_kwargs):
            return "OK", []

        def uid(self, *_args):
            return "OK", [b"7"]

    monkeypatch.setattr(email_otp.imaplib, "IMAP4_SSL", Mailbox)

    account = EmailAccount("alias@yandex.ru", "app-password", imap_username="main-login")
    assert ImapOtpReader().latest_uid(account) == 7
    assert usernames == ["main-login"]
