from __future__ import annotations

import logging
from datetime import datetime
from urllib.parse import urlparse

from .domain import Lesson, SessionState

log = logging.getLogger(__name__)


def classify_session_response(status_code: int, final_url: str) -> SessionState:
    """Classify an actual HTTP response without conflating it with transport failure."""
    if status_code >= 500:
        return SessionState.UNKNOWN
    lowered = final_url.lower()
    host = (urlparse(lowered).hostname or "").lower()
    if status_code in {401, 403} or "/login" in lowered or host == "login.mirea.ru":
        return SessionState.EXPIRED
    # Pulse sends an expired session to the SSO sign-in page, as pymirea knows.
    if host == "sso.mirea.ru" or "openid-connect/auth" in lowered:
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
        except (httpx.NetworkError, httpx.TimeoutException, httpx.HTTPError):
            log.info("session_verify_unknown reason=network")
            return SessionState.UNKNOWN
        return classify_session_response(response.status_code, str(response.url))

    async def get_schedule(self, days: int = 14) -> list[Lesson]:
        from pymirea.grades import MireaGrades

        api = MireaGrades(session_cookies=self.session)
        try:
            result = await api.get_schedule(days=days)
        finally:
            await api.close()
        if not result.success:
            raise RuntimeError(result.message or "Не удалось получить расписание")
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
