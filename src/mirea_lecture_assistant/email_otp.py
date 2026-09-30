from __future__ import annotations

import email
import imaplib
import logging
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.header import decode_header
from email.message import Message
from email.utils import parsedate_to_datetime

CONTEXT_CODE = re.compile(
    r"(?:код(?:а|ом)?(?:\s+(?:подтверждения|авторизации|входа))?|"
    r"verification\s+code|one[- ]time\s+(?:code|password)|otp)"
    r"[^0-9]{0,80}([0-9]{4,8})",
    re.IGNORECASE,
)
SIX_DIGITS = re.compile(r"(?<!\d)(\d{6})(?!\d)")
HTML_TAG = re.compile(r"<[^>]+>")
log = logging.getLogger(__name__)

EMAIL_PROVIDERS = {
    "auto": ("Определить автоматически", "", 993),
    "gmail": ("Gmail", "imap.gmail.com", 993),
    "yandex": ("Яндекс", "imap.yandex.ru", 993),
    "mailru": ("Mail.ru", "imap.mail.ru", 993),
    "microsoft": ("Microsoft / Outlook", "outlook.office365.com", 993),
    "custom": ("Другой сервер IMAP", "", 993),
}
YANDEX_PERSONAL_DOMAINS = {"yandex.ru", "yandex.com", "ya.ru", "yandex.kz", "yandex.by"}


def _is_authentication_error(error: BaseException) -> bool:
    message = str(error).casefold()
    return any(
        marker in message
        for marker in (
            "authentication failed",
            "authenticationfailed",
            "invalid credentials",
            "login failed",
            "imap is disabled",
        )
    )


def _authentication_help(account: EmailAccount) -> str:
    instructions = {
        "gmail": (
            "Gmail отклонил вход. Используйте 16-значный пароль приложения Google, "
            "а не обычный пароль аккаунта."
        ),
        "yandex": (
            "Яндекс отклонил вход. Проверьте, что указан именно пароль приложения «Почта» "
            "(не его название и не пароль Яндекс ID), а в настройках Почты включены "
            "IMAP и «Пароли приложений и OAuth-токены». Если адрес — алиас, "
            "укажите основной логин Яндекс ID в поле «Логин IMAP»."
        ),
        "mailru": (
            "Mail.ru отклонил вход. Создайте пароль для внешнего приложения с "
            "полным доступом к Почте и используйте его вместо обычного пароля."
        ),
        "microsoft": (
            "Microsoft отклонил парольный вход. Outlook требует OAuth2; если для "
            "аккаунта недоступен пароль приложения, введите код MIREA вручную."
        ),
        "custom": (
            "Почтовый сервер отклонил вход. Проверьте логин, пароль приложения и "
            "разрешение доступа по IMAP у провайдера."
        ),
    }
    return instructions.get(account.provider, instructions["custom"])


def infer_email_provider(address: str) -> str:
    domain = address.strip().casefold().rsplit("@", 1)[-1]
    if domain in {"gmail.com", "googlemail.com"}:
        return "gmail"
    if domain in YANDEX_PERSONAL_DOMAINS:
        return "yandex"
    if domain in {"mail.ru", "inbox.ru", "bk.ru", "list.ru", "internet.ru"}:
        return "mailru"
    if domain in {"outlook.com", "hotmail.com", "live.com", "msn.com"}:
        return "microsoft"
    return "custom"


@dataclass(frozen=True, slots=True)
class EmailAccount:
    address: str
    password: str
    provider: str = "auto"
    imap_host: str = ""
    imap_port: int = 993
    imap_username: str = ""

    def normalized(self) -> EmailAccount:
        address = self.address.strip()
        if "@" not in address:
            raise ValueError("Укажите полный адрес электронной почты")
        provider = self.provider if self.provider in EMAIL_PROVIDERS else "auto"
        if provider == "auto":
            provider = infer_email_provider(address)
        host = self.imap_host.strip()
        if provider != "custom":
            host = EMAIL_PROVIDERS[provider][1]
        if not host or "://" in host or "/" in host:
            raise ValueError("Укажите IMAP-сервер без https://, например imap.example.ru")
        port = int(self.imap_port)
        if not 1 <= port <= 65535:
            raise ValueError("Порт IMAP должен быть от 1 до 65535")
        password = self.password.strip()
        if provider == "gmail":
            password = password.replace(" ", "")
        if not password:
            raise ValueError("Укажите пароль приложения для почты")
        username = self.imap_username.strip()
        if "\r" in username or "\n" in username:
            raise ValueError("Логин IMAP не должен содержать переносы строк")
        return EmailAccount(address, password, provider, host, port, username)

    @property
    def provider_name(self) -> str:
        return EMAIL_PROVIDERS.get(self.provider, EMAIL_PROVIDERS["custom"])[0]

    def imap_usernames(self) -> tuple[str, ...]:
        """Login names accepted by the provider, in preferred order."""
        if self.imap_username:
            return (self.imap_username,)
        local, domain = self.address.rsplit("@", 1)
        if self.provider == "yandex" and domain.casefold() in YANDEX_PERSONAL_DOMAINS:
            return local, self.address
        return (self.address,)


@contextmanager
def _authenticated_mailbox(account: EmailAccount):
    """Open IMAP and try every provider-specific username representation."""
    last_auth_error: imaplib.IMAP4.error | None = None
    for attempt, username in enumerate(account.imap_usernames(), start=1):
        mailbox = imaplib.IMAP4_SSL(account.imap_host, account.imap_port, timeout=15)
        try:
            mailbox.login(username, account.password)
        except imaplib.IMAP4.error as exc:
            try:
                mailbox.shutdown()
            except (AttributeError, OSError, imaplib.IMAP4.error):
                log.debug("email_imap_failed_connection_cleanup_skipped")
            if not _is_authentication_error(exc):
                raise
            last_auth_error = exc
            log.info(
                "email_imap_login_variant_rejected provider=%s attempt=%s",
                account.provider,
                attempt,
            )
            continue
        try:
            yield mailbox
        finally:
            try:
                mailbox.logout()
            except (AttributeError, OSError, imaplib.IMAP4.error):
                log.debug("email_imap_logout_skipped")
        return
    if last_auth_error is not None:
        raise last_auth_error
    raise RuntimeError("Не удалось открыть соединение с почтовым сервером")


def _decode_header(value: str | None) -> str:
    parts = []
    for fragment, charset in decode_header(value or ""):
        if isinstance(fragment, bytes):
            parts.append(fragment.decode(charset or "utf-8", errors="replace"))
        else:
            parts.append(fragment)
    return "".join(parts)


def _message_text(message: Message) -> str:
    chunks: list[str] = []
    parts = message.walk() if message.is_multipart() else (message,)
    for part in parts:
        if part.get_content_maintype() == "multipart":
            continue
        if part.get_content_type() not in ("text/plain", "text/html"):
            continue
        payload = part.get_payload(decode=True)
        if not isinstance(payload, bytes):
            continue
        text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        chunks.append(HTML_TAG.sub(" ", text))
    return "\n".join(chunks)


def extract_fresh_otp(raw_message: bytes, not_before: datetime) -> str | None:
    message = email.message_from_bytes(raw_message)
    try:
        sent_at = parsedate_to_datetime(message.get("Date", ""))
    except (TypeError, ValueError, OverflowError):
        return None
    if sent_at is None:
        return None
    if sent_at.tzinfo is None:
        sent_at = sent_at.replace(tzinfo=UTC)
    if sent_at.astimezone(UTC) < not_before.astimezone(UTC) - timedelta(seconds=5):
        return None

    subject = _decode_header(message.get("Subject"))
    sender = _decode_header(message.get("From"))
    body = _message_text(message)
    combined = f"{subject}\n{body}"
    contextual = CONTEXT_CODE.search(combined)
    if contextual:
        return contextual.group(1)
    if "mirea" in f"{subject} {sender}".lower():
        generic = SIX_DIGITS.search(combined)
        if generic:
            return generic.group(1)
    return None


class ImapOtpReader:
    def latest_uid(self, account: EmailAccount) -> int:
        """Return the newest message UID before requesting a new MIREA code."""
        account = account.normalized()
        try:
            with _authenticated_mailbox(account) as mailbox:
                status, _ = mailbox.select("INBOX", readonly=True)
                if status != "OK":
                    raise RuntimeError(
                        f"Не удалось открыть входящие письма {account.provider_name}"
                    )
                status, data = mailbox.uid("search", None, "ALL")
                if status != "OK" or not data or not data[0].split():
                    return 0
                return int(data[0].split()[-1])
        except imaplib.IMAP4.error as exc:
            if _is_authentication_error(exc):
                raise RuntimeError(_authentication_help(account)) from exc
            raise RuntimeError(
                f"{account.provider_name} вернул ошибку IMAP; повторите проверку позже"
            ) from exc

    def wait_for_code(
        self,
        account: EmailAccount,
        not_before: datetime,
        timeout: int = 120,
        *,
        after_uid: int | None = None,
    ) -> str:
        account = account.normalized()
        deadline = time.monotonic() + timeout
        poll_number = 0
        log.info("email_otp_wait_started provider=%s timeout_seconds=%s", account.provider, timeout)
        while time.monotonic() < deadline:
            poll_number += 1
            try:
                with _authenticated_mailbox(account) as mailbox:
                    status, _ = mailbox.select("INBOX", readonly=True)
                    if status != "OK":
                        raise RuntimeError(
                            f"Не удалось открыть входящие письма {account.provider_name}"
                        )
                    status, data = mailbox.uid("search", None, "ALL")
                    if status == "OK" and data:
                        message_ids = data[0].split()
                        if after_uid is not None:
                            message_ids = [uid for uid in message_ids if int(uid) > after_uid]
                        for message_id in reversed(message_ids[-30:]):
                            status, rows = mailbox.uid("fetch", message_id, "(BODY.PEEK[])")
                            if status != "OK":
                                continue
                            for row in rows:
                                if isinstance(row, tuple) and isinstance(row[1], bytes):
                                    code = extract_fresh_otp(row[1], not_before)
                                    if code:
                                        log.info(
                                            "email_otp_found provider=%s poll=%s",
                                            account.provider,
                                            poll_number,
                                        )
                                        return code
            except imaplib.IMAP4.error as exc:
                if _is_authentication_error(exc):
                    raise RuntimeError(_authentication_help(account)) from exc
                log.warning(
                    "email_otp_poll_failed provider=%s poll=%s error=%s",
                    account.provider,
                    poll_number,
                    type(exc).__name__,
                )
            except (OSError, RuntimeError) as exc:
                log.warning(
                    "email_otp_poll_failed provider=%s poll=%s error=%s",
                    account.provider,
                    poll_number,
                    type(exc).__name__,
                )
            time.sleep(2)
        log.warning("email_otp_timeout provider=%s polls=%s", account.provider, poll_number)
        raise TimeoutError("Код подтверждения не пришёл в течение двух минут")
