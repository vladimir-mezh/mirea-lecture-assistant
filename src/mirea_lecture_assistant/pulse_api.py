"""Compatibility with Pulse's renamed authentication cookie.

Pulse now keeps its session in ``Pulse.Auth.Cookie`` (split into ``…C1``,
``…C2`` chunks when long) instead of ``.AspNetCore.Cookies``. pymirea's
bootstrap knows only the old name: with the new cookie it walked to
/api/auth/login, got no ``.AspNetCore.Cookies`` and answered «Перелогиньтесь».
"""

from pymirea.grades import MireaGrades as UpstreamGrades

PULSE_COOKIE_NAMES = ("Pulse.Auth.Cookie", ".AspNetCore.Cookies")

_upstream_ensure = UpstreamGrades._ensure_aspnet_cookie


async def _ensure_pulse_cookie(self):
    # The gRPC answer itself stays the verdict on the session; a present cookie
    # only skips the obsolete bootstrap.
    # Read from the jar, not with cookies.get(): Pulse renewing its cookie for
    # pulse.mirea.ru next to ours for .mirea.ru made get() raise CookieConflict.
    current = [
        cookie
        for cookie in self.client.cookies.jar
        if cookie.name == "Pulse.Auth.Cookie" and cookie.value
    ]
    if current:
        newest = next(
            (cookie for cookie in current if cookie.domain.lstrip(".") == "pulse.mirea.ru"),
            current[-1],
        )
        self.session_cookies["Pulse.Auth.Cookie"] = newest.value
        return True, None
    return await _upstream_ensure(self)


# Every pymirea path, not only this app's own calls: the QR attendance call
# (pymirea.MireaAPI) builds pymirea's MireaGrades internally.
UpstreamGrades._ensure_aspnet_cookie = _ensure_pulse_cookie


class MireaGrades(UpstreamGrades):
    """pymirea's client, kept under this name for the app's imports."""
