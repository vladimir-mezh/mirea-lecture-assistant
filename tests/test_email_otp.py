from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from typing import ClassVar

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

        def list(self):
            return "OK", []

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


class FakeMailbox:
    """An IMAP server with folders of (uid, raw letter); records every login."""

    logins: ClassVar[list[str]] = []

    def __init__(self, folders, list_rows=()):
        self.folders = folders
        self.list_rows = list(list_rows)
        self.selected = None
        self.searches = []

    def __call__(self, *_args, **_kwargs):
        return self

    def login(self, username, _password):
        FakeMailbox.logins.append(username)

    def logout(self):
        return None

    def list(self):
        return "OK", self.list_rows

    def select(self, name, readonly=False):
        name = name.strip('"')
        if name not in self.folders:
            return "NO", [b"no such folder"]
        self.selected = name
        return "OK", [str(len(self.folders[name])).encode()]

    def uid(self, command, *args):
        letters = self.folders[self.selected]
        if command == "search":
            self.searches.append((self.selected, args[1:]))
            return "OK", [b" ".join(str(uid).encode() for uid, _raw in letters)]
        wanted = int(args[0])
        for uid, raw in letters:
            if uid == wanted:
                return "OK", [(b"1 (UID %d BODY[] {1}" % uid, raw), b")"]
        return "OK", [None]


@pytest.fixture
def imap(monkeypatch):
    from mirea_lecture_assistant import email_otp

    FakeMailbox.logins = []
    monkeypatch.setattr(email_otp.time, "sleep", lambda _seconds: None)

    def install(folders, list_rows=()):
        server = FakeMailbox(folders, list_rows)
        monkeypatch.setattr(email_otp.imaplib, "IMAP4_SSL", server)
        return server

    return install


def raw_letter(headers: str, body: bytes = b"") -> bytes:
    return headers.replace("\n", "\r\n").encode("utf-8") + b"\r\n\r\n" + body


def rfc_date(moment: datetime) -> str:
    return moment.strftime("%a, %d %b %Y %H:%M:%S +0000")


def test_letter_with_raw_8bit_header_or_unknown_charset_does_not_break_parsing():
    now = datetime.now(UTC)
    raw_subject = raw_letter(
        f"From: shop@example.ru\nSubject: Скидки недели\nDate: {rfc_date(now)}"
    )
    bad_charset = raw_letter(
        f"From: shop@example.ru\nSubject: Hi\nDate: {rfc_date(now)}\n"
        "Content-Type: text/plain; charset=utf-8x",
        b"Hello",
    )

    assert extract_fresh_otp(raw_subject, now) is None
    assert extract_fresh_otp(bad_charset, now) is None


def test_code_is_read_from_a_letter_with_raw_8bit_utf8_subject():
    now = datetime.now(UTC)
    raw = raw_letter(
        f"From: sso@mirea.ru\nSubject: 012345 – ваш код для входа в РТУ МИРЭА\n"
        f"Date: {rfc_date(now)}"
    )
    assert extract_fresh_otp(raw, now) == "012345"


def test_style_digits_of_an_html_letter_are_not_taken_for_the_code():
    now = datetime.now(UTC)
    raw = raw_letter(
        f"From: sso@mirea.ru\nSubject: Подтверждение входа\nDate: {rfc_date(now)}\n"
        "Content-Type: text/html; charset=utf-8",
        "<html><head><style>.otp{width:1200px}</style></head>"
        "<body>Ваш код&nbsp;подтверждения: <b>482913</b></body></html>".encode(),
    )
    assert extract_fresh_otp(raw, now) == "482913"


def test_wait_uses_one_connection_for_many_polls(imap):
    now = datetime.now(UTC)
    code_letter = message("Подтверждение входа", "Код подтверждения: 482913", now, "sso@mirea.ru")
    server = imap({"INBOX": [(10, b"old")]})
    polls = []
    original_poll = ImapOtpReader._poll

    def poll(self, *args):
        polls.append(1)
        if len(polls) == 4:
            server.folders["INBOX"].append((11, code_letter))
        return original_poll(self, *args)

    reader = ImapOtpReader()
    reader._poll = poll.__get__(reader)

    assert (
        reader.wait_for_code(
            EmailAccount("student@mail.ru", "app-password"), now, timeout=60, after_uid=10
        )
        == "482913"
    )
    assert len(polls) == 4
    assert FakeMailbox.logins == ["student@mail.ru"]
    # Only letters after the snapshot are requested, not the whole inbox.
    assert server.searches[0] == ("INBOX", ("UID 11:*",))


def test_code_filed_as_spam_is_found(imap):
    now = datetime.now(UTC)
    code_letter = message("Подтверждение входа", "Код подтверждения: 482913", now, "sso@mirea.ru")
    server = imap(
        {"INBOX": [(5, b"old")], "&BCEEPwQwBDw-": [(3, code_letter)]},
        [
            b'(\\HasNoChildren) "/" "INBOX"',
            b'(\\HasNoChildren \\Junk) "/" "&BCEEPwQwBDw-"',
        ],
    )

    code = ImapOtpReader().wait_for_code(
        EmailAccount("student@mail.ru", "app-password"), now, timeout=60, after_uid=5
    )

    assert code == "482913"
    assert server.searches[-1][0] == "&BCEEPwQwBDw-"
    assert server.searches[-1][1][0] == "SINCE"


def test_spam_folder_without_special_use_flag_is_found_by_name(imap):
    now = datetime.now(UTC)
    code_letter = message("Подтверждение входа", "Код подтверждения: 482913", now, "sso@mirea.ru")
    imap(
        {"INBOX": [], "Spam": [(1, code_letter)]},
        [b'(\\HasNoChildren) "|" INBOX', b'(\\HasNoChildren) "|" Spam'],
    )

    assert (
        ImapOtpReader().wait_for_code(
            EmailAccount("student@yandex.ru", "app-password"), now, timeout=60
        )
        == "482913"
    )


def test_mirea_letter_wins_over_another_services_code(imap):
    now = datetime.now(UTC)
    foreign = message("Код подтверждения", "Ваш код: 7788", now, "noreply@shop.example")
    mirea = message("Подтверждение входа", "Код подтверждения: 482913", now, "sso@mirea.ru")
    imap({"INBOX": [(1, mirea), (2, foreign)]})

    assert (
        ImapOtpReader().wait_for_code(EmailAccount("student@gmail.com", "pw"), now, timeout=60)
        == "482913"
    )


def test_another_senders_code_is_used_only_after_a_grace_period(imap, monkeypatch):
    from mirea_lecture_assistant import email_otp

    now = datetime.now(UTC)
    foreign = message("Код подтверждения", "Ваш код: 7788", now, "noreply@other.example")
    imap({"INBOX": [(1, foreign)]})
    clock = [1000.0]

    def fake_sleep(seconds):
        clock[0] += seconds

    monkeypatch.setattr(email_otp.time, "sleep", fake_sleep)
    monkeypatch.setattr(email_otp.time, "monotonic", lambda: clock[0])

    assert (
        ImapOtpReader().wait_for_code(
            EmailAccount("student@gmail.com", "pw"), now, timeout=120, accept_foreign=True
        )
        == "7788"
    )
    assert clock[0] - 1000.0 >= email_otp.FOREIGN_CODE_GRACE_SECONDS


def test_one_unreadable_letter_does_not_stop_the_wait(imap, monkeypatch):
    from mirea_lecture_assistant import email_otp

    now = datetime.now(UTC)
    mirea = message("Подтверждение входа", "Код подтверждения: 482913", now, "sso@mirea.ru")
    imap({"INBOX": [(1, mirea), (2, b"broken")]})
    real = email_otp._otp_candidate

    def flaky(raw, not_before, **kwargs):
        if raw == b"broken":
            raise ValueError("malformed")
        return real(raw, not_before, **kwargs)

    monkeypatch.setattr(email_otp, "_otp_candidate", flaky)

    assert (
        ImapOtpReader().wait_for_code(EmailAccount("student@gmail.com", "pw"), now, timeout=60)
        == "482913"
    )


def test_rambler_is_detected():
    account = EmailAccount("student@rambler.ru", "password").normalized()
    assert account.provider == "rambler"
    assert account.imap_host == "imap.rambler.ru"


def test_undetected_provider_explains_how_to_set_the_server():
    with pytest.raises(ValueError, match="Другой сервер IMAP"):
        EmailAccount("student@edu.example.ru", "password").normalized()


@pytest.mark.parametrize("address", ["a@yandex.ru", "a@mail.ru"])
def test_grouped_app_password_is_joined_for_yandex_and_mailru(address):
    assert EmailAccount(address, "abcd efgh ijkl mnop").normalized().password == "abcdefghijklmnop"


def test_an_unattended_wait_never_submits_another_services_code(imap, monkeypatch):
    from mirea_lecture_assistant import email_otp

    now = datetime.now(UTC)
    foreign = message("Код подтверждения", "Ваш код: 7788", now, "noreply@other.example")
    imap({"INBOX": [(1, foreign)]})
    clock = [1000.0]
    monkeypatch.setattr(
        email_otp.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    )
    monkeypatch.setattr(email_otp.time, "monotonic", lambda: clock[0])

    with pytest.raises(TimeoutError):
        ImapOtpReader().wait_for_code(EmailAccount("student@gmail.com", "pw"), now, timeout=60)


@pytest.mark.parametrize(
    "text",
    ["Ваш промокод AUTUMN2026 на скидку", "Раскодируйте сообщение: 15000 символов"],
)
def test_words_merely_containing_kod_are_not_codes(text):
    now = datetime.now(UTC)
    raw = message("Акция", text, now, "shop@example.org")
    assert extract_fresh_otp(raw, now) is None


def test_a_new_letter_is_read_even_if_its_date_lags_behind(imap):
    """The MIREA mail server's clock ran a few seconds behind ours."""
    now = datetime.now(UTC)
    lagging = message(
        "Подтверждение входа",
        "Код подтверждения: 482913",
        now - timedelta(seconds=30),
        "sso@mirea.ru",
    )
    imap({"INBOX": [(5, b"old"), (6, lagging)]})

    assert (
        ImapOtpReader().wait_for_code(
            EmailAccount("student@mail.ru", "pw"), now, timeout=60, after_uid=5
        )
        == "482913"
    )
