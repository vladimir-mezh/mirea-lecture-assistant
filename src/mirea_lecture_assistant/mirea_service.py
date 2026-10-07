from __future__ import annotations

import contextvars
import logging
import re
import sys
from datetime import datetime
from urllib.parse import urlparse

from .domain import Lesson, SessionState
from .pulse_api import PULSE_COOKIE_NAMES

log = logging.getLogger(__name__)
# An explicit API refusal expires the session. A missing cookie by itself is
# inconclusive: auth flow changes, maintenance and network filters can cause it.
SESSION_EXPIRED_MESSAGE = re.compile(
    r"сессия[^.]*истекла|unauthenticated|unauthori[sz]ed|\b401\b", re.IGNORECASE
)
NO_LESSONS_MESSAGE = "Нет пар в ближайшие дни"
PULSE_COOKIE = ".AspNetCore.Cookies"
TOKEN_KEYS = frozenset({"access_token", "refresh_token", "token_type", "expires_in"})
REDIRECTS = (301, 302, 303, 307, 308)
# Keep the error actionable without attributing an auth failure to a VPN.
PLAIN_MESSAGES = {
    "Не удалось получить cookie (.AspNetCore.Cookies). Перелогиньтесь.": (
        "«Пульс» ответил, но не открыл сессию. Вход не завершён; "
        "повторите вход. Если ошибка повторяется, проверьте журнал."
    ),
}
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


def _is_sso_redirect_loop(failure: BaseException | None) -> bool:
    """Only a loop inside MIREA's sign-in flow is evidence of failed auth.

    The loop runs between Pulse's sign-in route and the SSO, so it may stop on
    either; a loop on any other Pulse page (maintenance) proves nothing.
    """
    import httpx

    if not isinstance(failure, httpx.TooManyRedirects):
        return False
    try:
        url = urlparse(str(failure.request.url))
    except RuntimeError:  # An upstream exception without the request is inconclusive.
        return False
    if url.hostname == "pulse.mirea.ru":
        return url.path.startswith(("/api/auth/", "/signin-oidc"))
    return url.hostname in {"sso.mirea.ru", "login.mirea.ru"} and (
        "/login-actions/" in url.path or "/protocol/openid-connect/auth" in url.path
    )


class MireaService:
    """Small compatibility boundary around pymirea 0.3/0.4."""

    def __init__(self, session: dict | None = None):
        self.session = session or {}
        self._auth = None
        self._expired_session = None

    @staticmethod
    def configure(session_key: str) -> None:
        from pymirea import Config, configure

        configure(Config(session_keys=session_key))
        capture_upstream_failures()

    async def login(self, username: str, password: str):
        from .pulse_auth import PulseAuth

        if self._auth is not None:
            # A retry starts a new SSO flow; release the previous client's sockets.
            try:
                await self._auth.close()
            except Exception:  # closing a stale client must not block a retry
                log.debug("previous_auth_close_failed", exc_info=True)
        self._auth = PulseAuth()
        result = await self._auth.login(username, password)
        if result.success:
            self.session = result.tokens or {}
            return await self._validate_completed_login(result)
        return result

    async def logout(self) -> None:
        """End the SSO session on the server as well (best effort).

        A fresh login after this starts from nothing on MIREA's side too: no
        half-dead SSO session for Keycloak to resume, no stale Pulse cookie.
        """
        import httpx
        from pymirea import MireaAuth

        refresh = str(self.session.get("refresh_token") or "").strip()
        if not refresh:
            return
        url = MireaAuth.TOKEN_URL.rsplit("/", 1)[0] + "/logout"
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(15.0, connect=8.0),
                transport=httpx.AsyncHTTPTransport(retries=1),
            ) as client:
                response = await client.post(
                    url, data={"client_id": MireaAuth.CLIENT_ID, "refresh_token": refresh}
                )
            log.info("sso_logout status=%s", response.status_code)
        except httpx.HTTPError as exc:
            log.info("sso_logout_failed kind=%s", type(exc).__name__)

    async def complete_2fa(self, challenge, code: str):
        if self._auth is None:
            raise RuntimeError("Сценарий входа уже завершён; начните вход заново")
        result = await self._auth.complete_2fa(challenge, code)
        if result.success:
            self.session = result.tokens or {}
            return await self._validate_completed_login(result)
        return result

    async def _validate_completed_login(self, result):
        """An accepted OTP is not yet an authenticated Pulse API session."""
        state = await self.verify_state()
        if state is SessionState.VALID:
            result.cookies = dict(self.session)
            return result
        if state is SessionState.EXPIRED:
            self.session = {}
        # During an outage retain the new SSO session and recheck it without
        # requesting another OTP. The UI completes this same login on success.
        result.session_pending = state is SessionState.UNKNOWN
        result.success = False
        result.cookies = dict(self.session) or None
        result.message = (
            "Пульс пока не подтвердил вход; повторим проверку без нового кода."
            if state is SessionState.UNKNOWN
            else "Пульс не принял сессию после входа. Повторите вход в MIREA."
        )
        log.warning("completed_login_unconfirmed state=%s", state.value)
        return result

    async def verify(self) -> bool:
        return await self.verify_state() is SessionState.VALID

    async def verify_state(self) -> SessionState:
        """Distinguish an expired session from an unreachable MIREA service.

        Only Pulse's API can say. A page check of pulse.mirea.ru lands on the SSO
        page both with a dead session and with a working one whose SSO browser
        session is over: 0.2.3 took every such start for an expiry, and before
        that a dead session passed as valid and was never renewed.
        """
        if not self.session:
            return SessionState.EXPIRED
        if self._expired_session is self.session:
            return SessionState.EXPIRED
        return await self.pulse_verdict()

    async def pulse_verdict(self) -> SessionState:
        """Whether Pulse's API itself accepts the session.

        The same two requests a schedule refresh starts with: the cookie
        bootstrap and one day of lessons. Unlike ``get_schedule`` it keeps that
        day's own answer, so a free day is not taken for a refusal.
        """
        state = await self._pulse_verdict_once()
        # A sign-in loop is first retried without the old SSO cookies: with live
        # tokens that is the whole fix, with none left the loop means expired.
        if state is not SessionState.VALID and await self._break_redirect_loop():
            state = await self._pulse_verdict_once()
        saved = {
            name: self.session.pop(name)
            for name in list(self.session)
            if state is SessionState.EXPIRED
            and any(name == base or name.startswith(base + "C") for base in PULSE_COOKIE_NAMES)
        }
        if saved:
            # pymirea sends a saved cookie as is and never replaces it, and with one
            # any HTML answer (a firewall or maintenance page) reads as a refusal.
            # Only a fresh bootstrap through the SSO can tell.
            log.info("session_verify_retry reason=saved_cookie_refused")
            state = await self._pulse_verdict_once()
            if state is not SessionState.VALID:
                # No new cookie came of it: keep the one that may well still work,
                # rather than save a session without it after a maintenance page.
                for name, value in saved.items():
                    self.session.setdefault(name, value)
        return state

    async def _pulse_verdict_once(self) -> SessionState:
        from .pulse_api import MireaGrades

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
                    self.session.update(getattr(api, "session_cookies", {}))
                    return SessionState.VALID
        except Exception as exc:  # noqa: BLE001 - trouble reaching Pulse is not a verdict
            if _is_sso_redirect_loop(exc):
                log.warning("session_verify_expired reason=sso_redirect_loop")
                return SessionState.EXPIRED
            log.info(
                "session_verify_unknown reason=api_exception kind=%s detail=%s",
                type(exc).__name__,
                failure_reason(exc),
            )
            return SessionState.UNKNOWN
        finally:
            await api.close()
        return self._verdict(message)

    async def _break_redirect_loop(self) -> bool:
        """Drop the SSO browser cookies when the Pulse sign-in went round in circles.

        Keycloak loops when an old copy of its cookies (kept in the session,
        sent for every *.mirea.ru host) meets the fresh ones it sets: pymirea's
        bootstrap then failed with TooManyRedirects at every start. The tokens
        stay, so pymirea continues with them or refreshes them. True if the last
        request looped and cookies were dropped.
        """
        if not _is_sso_redirect_loop(_upstream_failure.get()):
            return False
        await self._trace_bootstrap()
        stale = sorted(
            key for key in self.session if key not in TOKEN_KEYS and not str(key).startswith("__")
        )
        for key in stale:
            self.session.pop(key, None)
        log.warning("pulse_redirect_loop_cookies_dropped names=%s", ",".join(stale) or "-")
        _upstream_failure.set(None)
        return bool(stale)

    async def _trace_bootstrap(self) -> None:
        """Where the bootstrap goes round, for the journal: hosts, paths, cookie names."""
        import httpx
        from pymirea.tokens import get_authorization_header

        from .pulse_api import MireaGrades

        jar = httpx.Cookies()
        for name, value in self.session.items():
            if value and name not in TOKEN_KEYS and not str(name).startswith("__"):
                jar.set(str(name), str(value), domain=".mirea.ru")
        headers = {"Origin": MireaGrades.APP_URL, "Referer": f"{MireaGrades.APP_URL}/"}
        authorization = get_authorization_header(self.session)
        if authorization:
            headers["Authorization"] = authorization
        url = f"{MireaGrades.AUTH_LOGIN_URL}?redirectUri=%2Fapi%2Fbaseinfo"
        hops: list[str] = []
        try:
            async with httpx.AsyncClient(
                cookies=jar,
                follow_redirects=False,
                timeout=httpx.Timeout(15.0, connect=8.0),
                transport=httpx.AsyncHTTPTransport(retries=0),
            ) as client:
                for _ in range(12):
                    response = await client.get(url, headers=headers)
                    names = sorted(
                        {
                            item.split("=", 1)[0].strip()
                            for item in response.headers.get_list("set-cookie")
                        }
                    )
                    hops.append(
                        f"{response.status_code} {response.url.host}{response.url.path}"
                        f" set={','.join(names) or '-'}"
                    )
                    location = response.headers.get("location")
                    if response.status_code not in REDIRECTS or not location:
                        break
                    url = str(response.url.join(location))
        except Exception as exc:  # noqa: BLE001 - a diagnostic must never fail the caller
            hops.append(f"error {type(exc).__name__}")
        log.warning(
            "pulse_redirect_trace sent=%s hops=%s",
            ",".join(sorted(jar.keys())) or "-",
            " | ".join(hops),
        )

    async def _verdict_from_schedule(self, api) -> SessionState:
        """For a pymirea without the internals ``pulse_verdict`` relies on."""
        result = await api.get_schedule(days=1)
        if result.success:
            return SessionState.VALID
        return self._verdict(result.message)

    @staticmethod
    def _verdict(message: str | None) -> SessionState:
        if _is_sso_redirect_loop(_upstream_failure.get()):
            log.warning("session_verify_expired reason=sso_redirect_loop")
            return SessionState.EXPIRED
        if SESSION_EXPIRED_MESSAGE.search(message or ""):
            return SessionState.EXPIRED
        log.info("session_verify_unknown reason=api_error message=%s", _with_reason(message or ""))
        return SessionState.UNKNOWN

    async def get_schedule(self, days: int = 14, *, _retried: bool = False) -> list[Lesson]:
        from .pulse_api import MireaGrades

        _upstream_failure.set(None)
        api = MireaGrades(session_cookies=self.session)
        session = self.session
        try:
            result = await api.get_schedule(days=days)
            session.update(getattr(api, "session_cookies", {}))
        finally:
            await api.close()
        if not result.success:
            if not _retried and await self._break_redirect_loop():
                return await self.get_schedule(days, _retried=True)
            message = result.message or "Не удалось получить расписание"
            if _is_sso_redirect_loop(_upstream_failure.get()):
                # The circuit breaker may open after this failed bootstrap.
                # Recovery must use the auth evidence already obtained rather
                # than mistake the breaker's next refusal for a network outage.
                self._expired_session = session
                raise RuntimeError("Сессия Пульса истекла: вход зациклился на переадресациях.")
            if message == NO_LESSONS_MESSAGE:
                # pymirea says this both for free days and when every request
                # failed. Ask once more: an accepted session means truly no pairs.
                cookie = tuple(self.session.get(name) for name in PULSE_COOKIE_NAMES)
                state = await self.pulse_verdict()
                if state is SessionState.VALID:
                    if (
                        tuple(self.session.get(name) for name in PULSE_COOKIE_NAMES) != cookie
                        and not _retried
                    ):
                        # The saved cookie was stale and got replaced: the empty
                        # answer came from it, so ask with the new one.
                        return await self.get_schedule(days, _retried=True)
                    return []
                if state is SessionState.EXPIRED:
                    raise RuntimeError("Сессия истекла. Перелогиньтесь в МИРЭА.")
                message = "Пульс не вернул расписание."
            raise RuntimeError(_with_reason(PLAIN_MESSAGES.get(message, message)))
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
