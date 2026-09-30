from __future__ import annotations

import contextvars
import logging
import re
import sys
from datetime import datetime
from urllib.parse import urlparse

from .domain import Lesson, SessionState

log = logging.getLogger(__name__)
# How pymirea says that Pulse refused the session itself. "Не удалось получить
# cookie … Перелогиньтесь" is deliberately not here: Pulse answering without a
# cookie is what its firewall does to a VPN address, and a new login (with a new
# emailed code) cannot fix that.
SESSION_EXPIRED_MESSAGE = re.compile(
    r"сессия[^.]*истекла|unauthenticated|unauthori[sz]ed|\b401\b", re.IGNORECASE
)
NO_LESSONS_MESSAGE = "Нет пар в ближайшие дни"
UNREACHABLE_MESSAGE = re.compile(r"не\s+отвечает|временно\s+недоступна|не\s+вернул", re.IGNORECASE)

# pymirea turns every exception of a request into "МИРЭА не отвечает" and logs the
# original from inside its except block; this keeps it for the calling coroutine.
_upstream_failure: contextvars.ContextVar[BaseException | None] = contextvars.ContextVar(
    "mirea_upstream_failure", default=None
)


class _UpstreamFailureCapture(logging.Handler):
    def __init__(self):
        super().__init__(logging.WARNING)

    def emit(self, record: logging.LogRecord) -> None:
        failure = sys.exc_info()[1]
        if failure is not None:
            _upstream_failure.set(failure)


_CAPTURE = _UpstreamFailureCapture()


def capture_upstream_failures() -> None:
    upstream = logging.getLogger("pymirea")
    if _CAPTURE not in upstream.handlers:
        upstream.addHandler(_CAPTURE)


def failure_reason(failure: BaseException | None) -> str | None:
    """Why a request to MIREA failed, in words the student can act on."""
    if failure is None:
        return None
    import httpx

    chain, current = [], failure
    while current is not None and len(chain) < 6:
        chain.append(current)
        current = current.__cause__ or current.__context__
    text = " ".join(f"{type(item).__name__} {item}" for item in chain).lower()
    if "certificate" in text or "ssl" in text and "verif" in text:
        return "сертификат сайта МИРЭА не прошёл проверку — HTTPS подменяет антивирус или прокси"
    if isinstance(failure, httpx.TooManyRedirects):
        return "вход в Пульс зациклился на переадресациях"
    if isinstance(failure, httpx.TimeoutException) or "timed out" in text:
        return "сервер МИРЭА не ответил вовремя"
    if any(x in text for x in ("getaddrinfo", "name or service", "nodename", "name resolution")):
        return "адрес сервера МИРЭА не находится — проверьте интернет"
    if isinstance(failure, httpx.ProxyError):
        return "прокси не пропускает запросы к МИРЭА"
    if isinstance(failure, httpx.ConnectError):
        return "не удалось подключиться к серверу МИРЭА"
    if isinstance(failure, (httpx.RemoteProtocolError, httpx.ReadError, httpx.WriteError)):
        return "сервер МИРЭА оборвал соединение"
    return f"{type(failure).__name__}: {str(failure)[:120]}".rstrip(": ")


def _with_reason(message: str) -> str:
    """pymirea's message, plus the actual failure behind a generic "не отвечает"."""
    failure = _upstream_failure.get()
    if failure is None or not UNREACHABLE_MESSAGE.search(message):
        return message
    reason = failure_reason(failure)
    log.warning("mirea_request_failed kind=%s reason=%s", type(failure).__name__, reason)
    return f"{message} Причина: {reason}."


def classify_session_response(status_code: int, final_url: str) -> SessionState:
    """Classify an actual HTTP response without conflating it with transport failure."""
    if status_code >= 500:
        return SessionState.UNKNOWN
    lowered = final_url.lower()
    host = (urlparse(lowered).hostname or "").lower()
    # The same rule as pymirea's verify_session. Pulse is an app authorised by its
    # bearer token: a cookie-only request may well end on sso.mirea.ru with a
    # perfectly good session, so that alone must not count as expired (0.2.3
    # did, and threw the working session away at every start).
    if status_code in {401, 403} or "/login" in lowered or host == "login.mirea.ru":
        return SessionState.EXPIRED
    return SessionState.VALID


class MireaService:
    """Small compatibility boundary around pymirea 0.3/0.4."""

    def __init__(self, session: dict | None = None):
        self.session = session or {}
        self._auth = None

    @staticmethod
    def configure(session_key: str) -> None:
        from pymirea import Config, configure

        configure(Config(session_keys=session_key))
        capture_upstream_failures()

    async def login(self, username: str, password: str):
        from pymirea import MireaAuth

        if self._auth is not None:
            # A retry starts a new SSO flow; release the previous client's sockets.
            try:
                await self._auth.close()
            except Exception:  # closing a stale client must not block a retry
                log.debug("previous_auth_close_failed", exc_info=True)
        self._auth = MireaAuth()
        result = await self._auth.login(username, password)
        if result.success and result.tokens:
            self.session = result.tokens
        return result

    async def complete_2fa(self, challenge, code: str):
        if self._auth is None:
            raise RuntimeError("Сценарий входа уже завершён; начните вход заново")
        result = await self._auth.complete_2fa(challenge, code)
        if result.success and result.tokens:
            self.session = result.tokens
        return result

    async def verify(self) -> bool:
        return await self.verify_state() is SessionState.VALID

    async def verify_state(self) -> SessionState:
        """Distinguish an expired session from an unreachable MIREA service."""
        if not self.session:
            return SessionState.EXPIRED
        import httpx
        from pymirea import MireaAuth

        filtered_cookies = {
            name: value
            # A copy: pymirea may refresh the tokens from another thread meanwhile.
            for name, value in dict(self.session).items()
            if name not in {"access_token", "token_type", "refresh_token", "expires_in"}
            and not str(name).startswith("__")
        }
        try:
            async with httpx.AsyncClient(
                follow_redirects=True,
                timeout=httpx.Timeout(15.0, connect=8.0),
                transport=httpx.AsyncHTTPTransport(retries=2),
                cookies=filtered_cookies,
            ) as client:
                response = await client.get(MireaAuth.ATTENDANCE_URL)
        except httpx.HTTPError as exc:
            log.info(
                "session_verify_unknown reason=network kind=%s detail=%s",
                type(exc).__name__,
                failure_reason(exc),
            )
            return SessionState.UNKNOWN
        state = classify_session_response(response.status_code, str(response.url))
        if state is SessionState.EXPIRED:
            # A page check is only a hint. The session is thrown away (which
            # means a new login and an emailed code) only if Pulse itself refuses it.
            state = await self.pulse_verdict()
            if state is SessionState.VALID:
                log.info("session_page_check_overruled reason=api_accepts_session")
        return state

    async def pulse_verdict(self) -> SessionState:
        """Whether Pulse's API itself accepts the session.

        The same two requests a schedule refresh starts with: the cookie
        bootstrap and one day of lessons. Unlike ``get_schedule`` it keeps that
        day's own answer, so a free day is not taken for a refusal.
        """
        from pymirea.grades import MireaGrades

        _upstream_failure.set(None)
        api = MireaGrades(session_cookies=self.session)
        try:
            bootstrap = getattr(api, "_ensure_aspnet_cookie", None)
            unary = getattr(api, "_grpc_unary", None)
            encode = getattr(api, "_encode_date_request", None)
            lessons_url = getattr(api, "LESSONS_URL", None)
            if not (bootstrap and unary and encode and lessons_url):
                return await self._verdict_from_schedule(api)
            accepted, message = await bootstrap()
            if accepted:
                today = datetime.now().astimezone()
                raw, message = await unary(lessons_url, encode(today.year, today.month, today.day))
                if raw is not None:
                    return SessionState.VALID
        except Exception as exc:  # noqa: BLE001 - trouble reaching Pulse is not a verdict
            log.info("session_verify_unknown reason=api_exception kind=%s", type(exc).__name__)
            return SessionState.UNKNOWN
        finally:
            await api.close()
        return self._verdict(message)

    async def _verdict_from_schedule(self, api) -> SessionState:
        """For a pymirea without the internals ``pulse_verdict`` relies on."""
        result = await api.get_schedule(days=1)
        if result.success:
            return SessionState.VALID
        return self._verdict(result.message)

    @staticmethod
    def _verdict(message: str | None) -> SessionState:
        if SESSION_EXPIRED_MESSAGE.search(message or ""):
            return SessionState.EXPIRED
        log.info("session_verify_unknown reason=api_error message=%s", _with_reason(message or ""))
        return SessionState.UNKNOWN

    async def get_schedule(self, days: int = 14) -> list[Lesson]:
        from pymirea.grades import MireaGrades

        _upstream_failure.set(None)
        api = MireaGrades(session_cookies=self.session)
        try:
            result = await api.get_schedule(days=days)
        finally:
            await api.close()
        if not result.success:
            message = result.message or "Не удалось получить расписание"
            if message == NO_LESSONS_MESSAGE:
                # pymirea says this both for free days and when every request
                # failed. Ask once more: an accepted session means truly no pairs.
                state = await self.pulse_verdict()
                if state is SessionState.VALID:
                    return []
                if state is SessionState.EXPIRED:
                    raise RuntimeError("Сессия истекла. Перелогиньтесь в МИРЭА.")
                message = "Пульс не вернул расписание."
            raise RuntimeError(_with_reason(message))
        lessons = []
        for item in result.lessons or []:
            if item.start_epoch is None or item.end_epoch is None:
                continue
            start = datetime.fromtimestamp(item.start_epoch).astimezone()
            end = datetime.fromtimestamp(item.end_epoch).astimezone()
            location = item.room or ""
            lessons.append(
                Lesson(
                    external_id=str(getattr(item, "id", "") or f"{item.start_epoch}:{item.name}"),
                    subject_name=item.name,
                    lesson_type=item.lesson_type or "Занятие",
                    teacher=item.teacher,
                    group_name=getattr(item, "subgroup", None) or "",
                    start_at=start,
                    end_at=end,
                    room=item.room,
                    source_url=getattr(item, "url", None),
                    is_online=any(x in location.lower() for x in ("online", "онлайн", "дистан")),
                )
            )
        return lessons

    async def mark_attendance(self, raw_qr: str):
        from pymirea import MireaAPI

        api = MireaAPI(session_cookies=self.session)
        try:
            return await api.mark_attendance(raw_qr)
        finally:
            await api.close()
