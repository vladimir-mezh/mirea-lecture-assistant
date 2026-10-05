from __future__ import annotations

import email
import html
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

# Whole words only: "промокод AUTUMN2026" and "раскодируйте … 15000" are not codes.
CONTEXT_CODE = re.compile(
    r"(?<![а-яёa-z])(?:код(?:а|ом)?(?:\s+(?:подтверждения|авторизации|входа))?|"
    r"verification\s+code|one[- ]time\s+(?:code|password)|otp)(?![а-яёa-z])"
    r"[^0-9]{0,80}(?<!\d)([0-9]{4,8})(?!\d)",
    re.IGNORECASE,
)
SIX_DIGITS = re.compile(r"(?<!\d)(\d{6})(?!\d)")
# A letter about signing in («…ваш код для входа в учётную запись РТУ МИРЭА»).
# Other MIREA letters — СДО deadlines, news — carry six-digit numbers too: the
# id of an assignment in a link was once taken for a code.
SIGN_IN_LETTER = re.compile(
    r"(?:для|при)\s+входа|подтвержд\w*\s+вход|вход\w*\s+в\s+уч[её]тн|"
    r"sign[- ]?in|log[- ]?in|verification",
    re.IGNORECASE,
)
HTML_TAG = re.compile(r"<[^>]+>")
HTML_NOISE = re.compile(r"<(style|script|head)\b.*?</\1\s*>", re.IGNORECASE | re.DOTALL)
MIREA_MARKERS = ("mirea", "мирэа")
# How often the mailbox is re-read while a code is awaited.
POLL_SECONDS = 3
# A code-shaped letter from another sender is used only if nothing from MIREA
# shows up meanwhile: an unrelated service's code would burn a login attempt.
FOREIGN_CODE_GRACE_SECONDS = 20
# RFC 3501 dates need English month names whatever the process locale is.
IMAP_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
LIST_LINE = re.compile(rb'^\((?P<flags>[^)]*)\)\s+(?:"(?:[^"\\]|\\.)*"|NIL)\s+(?P<name>.+)$')
# Spam folders of providers that do not flag theirs with the \Junk attribute.
JUNK_NAMES = {"spam", "junk", "junk e-mail", "bulk mail", "&bceepwqwbdw-"}
log = logging.getLogger(__name__)

EMAIL_PROVIDERS = {
    "auto": ("Определить автоматически", "", 993),
    "gmail": ("Gmail", "imap.gmail.com", 993),
    "yandex": ("Яндекс", "imap.yandex.ru", 993),
    "mailru": ("Mail.ru", "imap.mail.ru", 993),
    "microsoft": ("Microsoft / Outlook", "outlook.office365.com", 993),
    "rambler": ("Рамблер", "imap.rambler.ru", 993),
    "custom": ("Другой сервер IMAP", "", 993),
}
YANDEX_PERSONAL_DOMAINS = {
    "yandex.ru",
    "yandex.com",
    "ya.ru",
    "yandex.kz",
    "yandex.by",
    "yandex.ua",
    "narod.ru",
}
RAMBLER_DOMAINS = {
    "rambler.ru",
    "lenta.ru",
    "autorambler.ru",
    "myrambler.ru",
    "ro.ru",
    "rambler.ua",
}
# App passwords of these providers never contain spaces, but are often pasted grouped.
SPACELESS_PASSWORD_PROVIDERS = {"gmail", "yandex", "mailru"}


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
        "rambler": (
            "Рамблер отклонил вход. Включите в настройках Почты доступ для почтовых "
            "программ (IMAP) и проверьте пароль."
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
    if domain in RAMBLER_DOMAINS:
        return "rambler"
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
        detected = provider == "auto"
        if detected:
            provider = infer_email_provider(address)
        host = self.imap_host.strip()
        if provider != "custom":
            host = EMAIL_PROVIDERS[provider][1]
        if not host and detected:
            raise ValueError(
                "Почтовый сервер не определился по адресу: выберите «Другой сервер IMAP» "
                "и укажите IMAP-сервер своей почты"
            )
        if not host or "://" in host or "/" in host:
            raise ValueError("Укажите IMAP-сервер без https://, например imap.example.ru")
        port = int(self.imap_port)
        if not 1 <= port <= 65535:
            raise ValueError("Порт IMAP должен быть от 1 до 65535")
        password = self.password.strip()
        if provider in SPACELESS_PASSWORD_PROVIDERS:
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


def _decode_bytes(data: bytes, charset: str | None) -> str:
    """Decode with the declared charset, falling back when Python does not know it.

    Letters with raw 8-bit headers (reported as ``unknown-8bit``) or a misspelt
    charset are common in Russian mailboxes. One such letter arriving while a
    code was awaited used to abort the whole wait with a LookupError.
    """
    for candidate in (charset, "utf-8"):
        if not candidate:
            continue
        try:
            return data.decode(candidate, errors="replace")
        except LookupError:
            continue
    return data.decode("utf-8", errors="replace")


def _decode_header(value) -> str:
    parts = []
    for fragment, charset in decode_header(value or ""):
        if isinstance(fragment, bytes):
            parts.append(_decode_bytes(fragment, charset))
        else:
            parts.append(fragment)
    return "".join(parts)


def _message_text(message: Message) -> str:
    chunks: list[str] = []
    parts = message.walk() if message.is_multipart() else (message,)
    for part in parts:
        if part.get_content_maintype() == "multipart":
            continue
        content_type = part.get_content_type()
        if content_type not in ("text/plain", "text/html"):
            continue
        payload = part.get_payload(decode=True)
        if not isinstance(payload, bytes):
            continue
        text = _decode_bytes(payload, part.get_content_charset())
        if content_type == "text/html":
            # Styles carry digits of their own (sizes, colours) that are not a code.
            text = html.unescape(HTML_TAG.sub(" ", HTML_NOISE.sub(" ", text)))
        chunks.append(text)
    return "\n".join(chunks)


def _otp_candidate(
    raw_message: bytes,
    not_before: datetime,
    *,
    check_date: bool = True,
    sign_in_only: bool = False,
) -> tuple[str, bool] | None:
    """Return (code, sent by MIREA) for a fresh code letter, otherwise None.

    ``check_date`` is off when the letter is known to be new by its UID: a
    sender whose clock lags a few seconds must not hide the real code.
    ``sign_in_only`` takes only letters about signing in, whatever they say
    about a "код": the always-on watcher of the student's own sign-ins uses it.
    """
    message = email.message_from_bytes(raw_message)
    if check_date:
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
    from_mirea = any(marker in f"{subject} {sender}".casefold() for marker in MIREA_MARKERS)
    body = _message_text(message)
    combined = f"{subject}\n{body}"
    about_sign_in = bool(SIGN_IN_LETTER.search(combined))
    if sign_in_only and not about_sign_in:
        return None
    contextual = CONTEXT_CODE.search(combined)
    if contextual:
        return contextual.group(1), from_mirea
    if from_mirea and about_sign_in:
        generic = SIX_DIGITS.search(combined)
        if generic:
            return generic.group(1), True
    return None


def extract_fresh_otp(raw_message: bytes, not_before: datetime) -> str | None:
    candidate = _otp_candidate(raw_message, not_before)
    return candidate[0] if candidate else None


def _quote_mailbox(name: str) -> str:
    """imaplib sends mailbox names as is; a name with a space must be quoted."""
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _junk_folders(mailbox) -> list[str]:
    """Spam folders worth reading: Яндекс and Mail.ru often file a first MIREA letter there."""
    try:
        status, rows = mailbox.list()
    except (imaplib.IMAP4.error, OSError):
        log.debug("email_imap_list_failed", exc_info=True)
        return []
    if status != "OK":
        return []
    found: list[str] = []
    for row in rows or []:
        if not isinstance(row, bytes):
            continue  # a name sent as a literal; no provider does that for spam
        match = LIST_LINE.match(row.strip())
        if not match:
            continue
        flags = match["flags"].lower()
        if b"\\noselect" in flags:
            continue
        name = match["name"].strip()
        if name.startswith(b'"') and name.endswith(b'"'):
            name = name[1:-1].replace(b'\\"', b'"').replace(b"\\\\", b"\\")
        decoded = name.decode("ascii", errors="replace")  # modified UTF-7 is ASCII
        leaf = re.split(r"[/.|]", decoded)[-1].casefold()
        is_junk = b"\\junk" in flags or leaf in JUNK_NAMES
        if is_junk and decoded.upper() != "INBOX" and decoded not in found:
            found.append(decoded)
    return found


def _imap_since(moment: datetime) -> str:
    """SINCE compares server-local dates, so a day of slack covers any time zone."""
    day = moment.astimezone(UTC).date() - timedelta(days=1)
    return f"{day.day:02d}-{IMAP_MONTHS[day.month - 1]}-{day.year}"


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
                # "UID *" names only the newest letter; ALL returned every UID
                # and overflowed imaplib's line limit on very large mailboxes.
                try:
                    status, data = mailbox.uid("search", None, "UID *")
                except imaplib.IMAP4.error as exc:
                    if _is_authentication_error(exc) or isinstance(exc, imaplib.IMAP4.abort):
                        raise
                    status, data = "NO", []
                if status != "OK" or not data or not data[0].split():
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

    @staticmethod
    def _new_uids(mailbox, folder: str, not_before: datetime, after_uid: int | None) -> list[bytes]:
        """UIDs worth reading in one folder, newest first."""
        name = folder if folder == "INBOX" else _quote_mailbox(folder)
        status, _ = mailbox.select(name, readonly=True)
        if status != "OK":
            if folder == "INBOX":
                raise RuntimeError("Не удалось открыть входящие письма")
            return []
        if after_uid is not None:
            # Only letters that arrived after the snapshot, not the whole mailbox.
            status, data = mailbox.uid("search", None, f"UID {after_uid + 1}:*")
        else:
            status, data = mailbox.uid("search", None, "SINCE", _imap_since(not_before))
        if status != "OK" or not data or not data[0]:
            return []
        uids = data[0].split()
        if after_uid is not None:
            # "n:*" always names the newest letter, even when it is older than n.
            uids = [uid for uid in uids if int(uid) > after_uid]
        return list(reversed(uids[-30:]))

    def _poll(
        self,
        mailbox,
        folders: list[str],
        not_before: datetime,
        after_uid: int | None,
        checked: set[tuple[str, bytes]],
        foreign: dict[str, float],
        accept_foreign: bool = False,
        *,
        sign_in_only: bool = False,
    ) -> str | None:
        for folder in folders:
            # A UID snapshot only exists for the inbox; UIDs differ per folder.
            folder_after = after_uid if folder == "INBOX" else None
            for uid in self._new_uids(mailbox, folder, not_before, folder_after):
                key = (folder, uid)
                if key in checked:
                    continue
                status, rows = mailbox.uid("fetch", uid, "(BODY.PEEK[])")
                if status != "OK":
                    continue
                checked.add(key)
                for row in rows:
                    if not (isinstance(row, tuple) and isinstance(row[1], bytes)):
                        continue
                    try:
                        candidate = _otp_candidate(
                            row[1],
                            not_before,
                            check_date=folder_after is None,
                            sign_in_only=sign_in_only,
                        )
                    except Exception:  # one broken letter must not stop the wait
                        log.debug("email_otp_letter_unreadable", exc_info=True)
                        continue
                    if candidate is None:
                        continue
                    code, from_mirea = candidate
                    if from_mirea:
                        return code
                    foreign.setdefault(code, time.monotonic())
        if not accept_foreign:
            return None
        now = time.monotonic()
        for code, first_seen in foreign.items():
            if now - first_seen >= FOREIGN_CODE_GRACE_SECONDS:
                log.info("email_otp_foreign_sender_used")
                return code
        return None

    def wait_for_code(
        self,
        account: EmailAccount,
        not_before: datetime,
        timeout: int = 120,
        *,
        after_uid: int | None = None,
        accept_foreign: bool = False,
    ) -> str:
        """Wait for the emailed code over one IMAP connection.

        Signing in anew every two seconds made Mail.ru and Яндекс throttle or
        reject the mailbox mid-wait, so the connection is kept and the folders
        are only re-selected; it is re-opened only after a failure.
        """
        account = account.normalized()
        deadline = time.monotonic() + timeout
        poll_number = 0
        checked: set[tuple[str, bytes]] = set()
        foreign: dict[str, float] = {}
        log.info("email_otp_wait_started provider=%s timeout_seconds=%s", account.provider, timeout)
        while time.monotonic() < deadline:
            try:
                with _authenticated_mailbox(account) as mailbox:
                    folders = ["INBOX", *_junk_folders(mailbox)]
                    log.info(
                        "email_otp_mailbox_open provider=%s spam_folders=%s",
                        account.provider,
                        len(folders) - 1,
                    )
                    while time.monotonic() < deadline:
                        poll_number += 1
                        code = self._poll(
                            mailbox,
                            folders,
                            not_before,
                            after_uid,
                            checked,
                            foreign,
                            accept_foreign,
                        )
                        if code:
                            log.info(
                                "email_otp_found provider=%s poll=%s",
                                account.provider,
                                poll_number,
                            )
                            return code
                        time.sleep(POLL_SECONDS)
                    break
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
