from __future__ import annotations

import json

import pytest

from mirea_lecture_assistant import security
from mirea_lecture_assistant.email_otp import EmailAccount
from mirea_lecture_assistant.security import SERVICE_NAME, SESSION_USER, SessionStore

# A Keycloak session is far larger than the 2560 bytes Windows allows per credential.
BIG_SESSION = {
    "access_token": "a" * 2400,
    "refresh_token": "r" * 1800,
    "KEYCLOAK_IDENTITY": "k" * 600,
}


class FakeKeyring:
    """Stand-in for the OS keyring, with the Windows blob size limit enforced."""

    MAX_BLOB_BYTES = 2560

    def __init__(self):
        self.values: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, account: str) -> str | None:
        return self.values.get((service, account))

    def set_password(self, service: str, account: str, value: str) -> None:
        if len(value.encode("utf-16-le")) > self.MAX_BLOB_BYTES:
            raise OSError(1783, "CredWrite", "Заглушке переданы неправильные данные")
        self.values[(service, account)] = value

    def delete_password(self, service: str, account: str) -> None:
        if (service, account) not in self.values:
            raise KeyError(account)
        del self.values[(service, account)]


@pytest.fixture
def fake_keyring(monkeypatch):
    keyring = FakeKeyring()
    monkeypatch.setitem(__import__("sys").modules, "keyring", keyring)
    return keyring


def test_large_session_round_trips(tmp_path, fake_keyring):
    store = SessionStore(tmp_path)
    store.save(BIG_SESSION)
    assert SessionStore(tmp_path).load() == BIG_SESSION


def test_session_is_not_written_in_plain_text(tmp_path, fake_keyring):
    store = SessionStore(tmp_path)
    store.save(BIG_SESSION)
    blob = store.session_path.read_bytes()
    assert BIG_SESSION["access_token"].encode() not in blob
    assert b"access_token" not in blob


def test_only_the_small_key_reaches_the_keyring(tmp_path, fake_keyring):
    SessionStore(tmp_path).save(BIG_SESSION)
    accounts = {account for _service, account in fake_keyring.values}
    assert accounts == {security.SESSION_KEY_USER}


def test_load_returns_none_without_a_stored_session(tmp_path, fake_keyring):
    assert SessionStore(tmp_path).load() is None


def test_damaged_session_file_is_reported_as_missing(tmp_path, fake_keyring):
    store = SessionStore(tmp_path)
    store.save(BIG_SESSION)
    store.session_path.write_bytes(b"not a fernet token")
    assert store.load() is None


def test_clear_removes_the_session_file(tmp_path, fake_keyring):
    store = SessionStore(tmp_path)
    store.save(BIG_SESSION)
    store.clear()
    assert not store.session_path.exists()
    assert store.load() is None


def test_session_from_the_keyring_version_is_migrated(tmp_path, fake_keyring):
    small_session = {"KEYCLOAK_IDENTITY": "legacy"}
    fake_keyring.values[(SERVICE_NAME, SESSION_USER)] = json.dumps(small_session)
    store = SessionStore(tmp_path)
    assert store.load() == small_session
    assert store.session_path.exists()
    assert (SERVICE_NAME, SESSION_USER) not in fake_keyring.values


def test_login_and_email_credentials_round_trip_in_the_os_keyring(tmp_path, fake_keyring):
    store = SessionStore(tmp_path)
    store.save_credentials("student@edu.mirea.ru", "mirea-password")
    store.save_email_credentials(EmailAccount("student@yandex.ru", "mail-password"))

    assert store.load_credentials() == ("student@edu.mirea.ru", "mirea-password")
    assert store.load_email_credentials() == EmailAccount(
        "student@yandex.ru", "mail-password", "yandex", "imap.yandex.ru", 993
    )


def test_imap_login_override_survives_restart(tmp_path, fake_keyring):
    store = SessionStore(tmp_path)
    account = EmailAccount(
        "alias@yandex.ru", "mail-password", "yandex", "imap.yandex.ru", 993, "main-login"
    )

    store.save_email_credentials(account)

    assert SessionStore(tmp_path).load_email_credentials() == account


def test_old_gmail_credentials_are_migrated_by_inference(tmp_path, fake_keyring):
    fake_keyring.values[(SERVICE_NAME, security.EMAIL_LOGIN_USER)] = "student@gmail.com"
    fake_keyring.values[(SERVICE_NAME, security.EMAIL_PASSWORD_USER)] = "abcd efgh ijkl mnop"

    account = SessionStore(tmp_path).load_email_credentials()

    assert account == EmailAccount(
        "student@gmail.com", "abcdefghijklmnop", "gmail", "imap.gmail.com", 993
    )


def test_obsolete_pending_attendance_is_deleted(tmp_path, fake_keyring):
    store = SessionStore(tmp_path)
    stale = tmp_path / "pending-attendance.bin"
    stale.write_bytes(b"obsolete-secret-qr-token")

    store.discard_obsolete_pending_attendance()

    assert not stale.exists()


def test_a_redirect_to_the_sso_sign_in_page_means_the_session_expired():
    from mirea_lecture_assistant.domain import SessionState
    from mirea_lecture_assistant.mirea_service import classify_session_response

    url = "https://sso.mirea.ru/realms/mirea/protocol/openid-connect/auth?client_id=attendance-app"
    assert classify_session_response(200, url) is SessionState.EXPIRED
