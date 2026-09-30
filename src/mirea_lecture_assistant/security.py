from __future__ import annotations

import json
import logging
from pathlib import Path

from .email_otp import EmailAccount
from .paths import data_dir

SERVICE_NAME = "MireaLectureAssistant"
SESSION_USER = "mirea-session"
SESSION_KEY_USER = "session-encryption-key"
SESSION_FILE_NAME = "session.bin"
LOGIN_USER = "mirea-login"
PASSWORD_USER = "mirea-password"
EMAIL_LOGIN_USER = "gmail-login"
EMAIL_PASSWORD_USER = "gmail-app-password"
EMAIL_CONFIG_USER = "email-imap-config"

log = logging.getLogger(__name__)


class SessionStore:
    """Secrets live in the OS keyring; the session blob lives encrypted on disk.

    Windows Credential Manager caps one secret at 2560 bytes, and a MIREA session
    carries an access and a refresh token, so it does not fit. The blob is stored
    as a file instead, encrypted with a key that is small enough for the keyring.
    """

    def __init__(self, root: Path | None = None):
        self._root = root

    @property
    def session_path(self) -> Path:
        root = self._root if self._root is not None else data_dir()
        root.mkdir(parents=True, exist_ok=True)
        return root / SESSION_FILE_NAME

    def _cipher(self):
        import keyring
        from cryptography.fernet import Fernet

        key = keyring.get_password(SERVICE_NAME, SESSION_KEY_USER)
        if not key:
            key = Fernet.generate_key().decode("ascii")
            keyring.set_password(SERVICE_NAME, SESSION_KEY_USER, key)
        return Fernet(key.encode("ascii"))

    def save(self, session: dict) -> None:
        payload = self._cipher().encrypt(json.dumps(session).encode("utf-8"))
        path = self.session_path
        path.write_bytes(payload)
        try:
            path.chmod(0o600)
        except OSError:  # best effort: filesystems without POSIX permissions
            pass
        self._clear_legacy_session()

    def load(self) -> dict | None:
        path = self.session_path
        if path.exists():
            from cryptography.fernet import InvalidToken

            try:
                raw = self._cipher().decrypt(path.read_bytes())
            except (InvalidToken, OSError, ValueError):
                log.warning("stored_session_unreadable")
                return None
            return self._as_session(raw.decode("utf-8"))
        return self._load_legacy_session()

    def clear(self) -> None:
        self.session_path.unlink(missing_ok=True)
        self._clear_legacy_session()

    def discard_obsolete_pending_attendance(self) -> None:
        """Remove the retry file created by older builds.

        Attendance QR codes rotate every few seconds, so retaining one across an
        application restart can only cause a submission of an expired code.
        """
        root = self._root if self._root is not None else data_dir()
        (root / "pending-attendance.bin").unlink(missing_ok=True)

    @staticmethod
    def _as_session(value: str) -> dict | None:
        try:
            data = json.loads(value)
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) else None

    def _load_legacy_session(self) -> dict | None:
        """Read (and migrate) a session written by versions that used the keyring."""
        import keyring

        value = keyring.get_password(SERVICE_NAME, SESSION_USER)
        if not value:
            return None
        session = self._as_session(value)
        if session is None:
            return None
        try:
            self.save(session)
            log.info("stored_session_migrated_to_file")
        except Exception:  # migration is optional; the session itself is valid
            log.warning("stored_session_migration_failed", exc_info=True)
        return session

    @staticmethod
    def _clear_legacy_session() -> None:
        import keyring

        try:
            keyring.delete_password(SERVICE_NAME, SESSION_USER)
        except Exception:  # backends differ on how they report "not found"
            log.debug("legacy_session_delete_skipped", exc_info=True)

    def save_credentials(self, username: str, password: str) -> None:
        import keyring

        keyring.set_password(SERVICE_NAME, LOGIN_USER, username)
        keyring.set_password(SERVICE_NAME, PASSWORD_USER, password)

    def load_credentials(self) -> tuple[str, str] | None:
        import keyring

        username = keyring.get_password(SERVICE_NAME, LOGIN_USER)
        password = keyring.get_password(SERVICE_NAME, PASSWORD_USER)
        if not username or not password:
            return None
        return username, password

    def clear_credentials(self) -> None:
        import keyring

        for account in (LOGIN_USER, PASSWORD_USER):
            try:
                keyring.delete_password(SERVICE_NAME, account)
            except keyring.errors.PasswordDeleteError:
                pass

    def save_email_credentials(self, account: EmailAccount) -> None:
        import keyring

        account = account.normalized()
        keyring.set_password(SERVICE_NAME, EMAIL_LOGIN_USER, account.address)
        keyring.set_password(SERVICE_NAME, EMAIL_PASSWORD_USER, account.password)
        keyring.set_password(
            SERVICE_NAME,
            EMAIL_CONFIG_USER,
            json.dumps(
                {
                    "provider": account.provider,
                    "imap_host": account.imap_host,
                    "imap_port": account.imap_port,
                    "imap_username": account.imap_username,
                }
            ),
        )

    def load_email_credentials(self) -> EmailAccount | None:
        import keyring

        address = keyring.get_password(SERVICE_NAME, EMAIL_LOGIN_USER)
        password = keyring.get_password(SERVICE_NAME, EMAIL_PASSWORD_USER)
        if not address or not password:
            return None
        raw_config = keyring.get_password(SERVICE_NAME, EMAIL_CONFIG_USER)
        try:
            config = json.loads(raw_config) if raw_config else {}
        except (TypeError, json.JSONDecodeError):
            config = {}
        return EmailAccount(
            address=address,
            password=password,
            provider=str(config.get("provider", "auto")),
            imap_host=str(config.get("imap_host", "")),
            imap_port=int(config.get("imap_port", 993)),
            imap_username=str(config.get("imap_username", "")),
        ).normalized()

    def clear_email_credentials(self) -> None:
        import keyring

        for account in (EMAIL_LOGIN_USER, EMAIL_PASSWORD_USER, EMAIL_CONFIG_USER):
            try:
                keyring.delete_password(SERVICE_NAME, account)
            except keyring.errors.PasswordDeleteError:
                pass
