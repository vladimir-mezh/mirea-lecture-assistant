from __future__ import annotations

import re

PERMANENT_LOGIN_ERROR = re.compile(
    r"неверн(?:ый|ая).*?(?:логин|парол)|invalid\s+(?:credentials|password)|"
    r"уч[её]тная запись.*(?:заблок|отключ)|authentication\s+failed|"
    r"(?:gmail|яндекс|mail\.ru|microsoft|outlook|почт).*?(?:отклонил|парол|способ входа)",
    re.IGNORECASE,
)
# Not a verdict on this QR: the network, the session, or MIREA itself. pymirea
# reports a failed connection as the generic "Ошибка отметки посещаемости", and
# "Отметка пока недоступна" means the teacher has not opened attendance yet.
TRANSIENT_ATTENDANCE_ERROR = re.compile(
    r"временно\s+недоступ|ошибка\s+(?:сети|соединения)|тайм.?аут|timeout|network|"
    r"соединени|сессия\s+истек|перелог|needs_auth|авторизац|\b(?:429|5\d\d)\b|"
    r"^ошибка\s+отметки\s+посещаемости\.?$|сервер\s+вернул\s+spa|"
    r"неизвестный\s+ответ\s+сервера|пока\s+недоступн",
    re.IGNORECASE,
)


def login_retry_delay(attempt: int) -> int:
    """Bounded exponential retry delay in seconds."""
    return min(300, 15 * (2 ** max(0, attempt - 1)))


def should_retry_login(message: str | None) -> bool:
    """Only explicit credential/account failures require human intervention."""
    return not bool(PERMANENT_LOGIN_ERROR.search(message or ""))


def transient_login_failure(message: str | None) -> bool:
    """A rejected OTP is not permission to loop; explicit outages are retryable."""
    return bool(
        re.search(
            r"timeout|timed out|network|connect|temporar|unavailable|тайм.?аут|"
            r"не отвечает|недоступ|соединени|ошибка сети|\b(?:429|5\d\d)\b",
            message or "",
            re.IGNORECASE,
        )
    )


def attendance_failure_counts_for_chat(message: str | None) -> bool:
    """Only a real attendance rejection counts toward the public chat fallback."""
    return not bool(TRANSIENT_ATTENDANCE_ERROR.search(message or ""))
