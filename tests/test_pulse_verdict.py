"""MireaService's session verdict against pymirea's real code and a fake Pulse.

The flows are those pymirea itself handles: the cookie bootstrap through
/api/auth/login (which lands on the SSO page when Pulse refuses the session),
the token refresh at the SSO, and a gRPC-Web lesson call.
"""

from __future__ import annotations

import asyncio
import struct
from types import SimpleNamespace

import httpx
import pytest

from mirea_lecture_assistant.domain import SessionState
from mirea_lecture_assistant.mirea_service import MireaService, capture_upstream_failures

LOGIN_PAGE = "https://sso.mirea.ru/realms/mirea/protocol/openid-connect/auth?client_id=pulse"
EMPTY_DAY = b"\x00" + struct.pack(">I", 0) + b"\x80" + struct.pack(">I", 15) + b"grpc-status: 0\r\n"


class Breaker:
    async def allow(self):
        return SimpleNamespace(allowed=True, retry_after_s=None)

    async def record_success(self):
        return None

    async def record_failure(self):
        return None


@pytest.fixture(scope="module", autouse=True)
def configured():
    MireaService.configure("x" * 44)
    capture_upstream_failures()


@pytest.fixture
def pulse(monkeypatch):
    """Route every pymirea request to ``pulse.handle`` and keep breakers closed."""
    import pymirea.auth
    import pymirea.grades

    for module in (pymirea.auth, pymirea.grades):
        monkeypatch.setattr(module, "get_breaker", lambda _name: Breaker())
    state = SimpleNamespace(handle=None, requests=[])

    def dispatch(request: httpx.Request) -> httpx.Response:
        state.requests.append(f"{request.method} {request.url.host}{request.url.path}")
        return state.handle(request)

    real_transport = httpx.AsyncHTTPTransport
    monkeypatch.setattr(
        httpx,
        "AsyncHTTPTransport",
        lambda *args, **kwargs: (
            httpx.MockTransport(dispatch)
            if "retries" in kwargs
            else real_transport(*args, **kwargs)
        ),
    )
    return state


def _refusing_pulse(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/api/auth/login":
        return httpx.Response(302, headers={"Location": LOGIN_PAGE})
    if request.url.host == "sso.mirea.ru" and request.url.path.endswith("/token"):
        return httpx.Response(400, json={"error": "invalid_grant"})
    if request.url.host == "sso.mirea.ru":
        return httpx.Response(200, html="<html><form id=kc-form-login></form></html>")
    if request.url.path.startswith("/rtu_tc."):
        return httpx.Response(200, html="<html>Keycloak login</html>")
    return httpx.Response(404)


def _accepting_pulse(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/api/auth/login":
        return httpx.Response(
            302,
            headers={
                "Location": "https://pulse.mirea.ru/api/baseinfo",
                "Set-Cookie": ".AspNetCore.Cookies=fresh; path=/; secure; httponly",
            },
        )
    if request.url.path == "/api/baseinfo":
        return httpx.Response(200, json={"ok": True})
    if request.url.path.startswith("/rtu_tc."):
        return httpx.Response(
            200, content=EMPTY_DAY, headers={"Content-Type": "application/grpc-web+proto"}
        )
    return httpx.Response(404)


def test_pulse_refusing_the_session_and_its_refresh_token_means_expired(pulse):
    pulse.handle = _refusing_pulse
    service = MireaService({"access_token": "stale", "refresh_token": "spent"})

    # The recovery after a failed refresh asks exactly this; before 0.2.5 a page
    # check answered "valid" here and the dead session was never renewed.
    assert asyncio.run(service.verify_state()) is SessionState.EXPIRED


def test_a_maintenance_page_behind_a_saved_cookie_is_not_an_expired_session(pulse):
    def maintenance(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, html="<html><h1>Технические работы</h1></html>")

    pulse.handle = maintenance
    service = MireaService({".AspNetCore.Cookies": "saved", "access_token": "a"})

    assert asyncio.run(service.verify_state()) is SessionState.UNKNOWN


def test_pulse_accepting_the_session_on_a_free_day_means_valid(pulse):
    pulse.handle = _accepting_pulse
    session = {"access_token": "a", "refresh_token": "r", "KEYCLOAK_IDENTITY": "id"}

    assert asyncio.run(MireaService(session).pulse_verdict()) is SessionState.VALID
    # pymirea keeps the new cookie in the session it was given; the app saves it.
    assert session[".AspNetCore.Cookies"] == "fresh"


def test_an_unreachable_pulse_is_unknown_and_names_the_cause(pulse, caplog):
    def unreachable(request):
        raise httpx.ConnectTimeout("timed out", request=request)

    pulse.handle = unreachable

    with caplog.at_level("INFO", logger="mirea_lecture_assistant.mirea_service"):
        state = asyncio.run(MireaService({"access_token": "a"}).pulse_verdict())

    assert state is SessionState.UNKNOWN
    assert "не ответил вовремя" in caplog.text


def test_a_schedule_refresh_through_an_unreachable_pulse_says_why(pulse):
    def unreachable(request):
        raise httpx.ConnectError("[Errno 11001] getaddrinfo failed", request=request)

    pulse.handle = unreachable

    with pytest.raises(RuntimeError) as failure:
        asyncio.run(MireaService({"access_token": "a"}).get_schedule(2))

    assert "МИРЭА не отвечает" in str(failure.value)
    assert "не находится" in str(failure.value)


def test_a_week_without_pairs_is_an_empty_schedule_not_an_error(pulse):
    pulse.handle = _accepting_pulse

    assert asyncio.run(MireaService({"access_token": "a"}).get_schedule(3)) == []


def test_a_maintenance_page_does_not_cost_the_saved_cookie(pulse):
    """The retry without the cookie proved nothing, so the cookie must stay: saved
    without it, the session was judged expired once Pulse came back (new code)."""

    def maintenance(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, html="<html><h1>Технические работы</h1></html>")

    pulse.handle = maintenance
    session = {".AspNetCore.Cookies": "still-good", "access_token": "a"}

    assert asyncio.run(MireaService(session).verify_state()) is SessionState.UNKNOWN
    assert session[".AspNetCore.Cookies"] == "still-good"


def _looping_keycloak(request: httpx.Request) -> httpx.Response:
    """Keycloak going round in circles while an old copy of its cookie is sent along."""
    if request.url.path == "/api/auth/login":
        return httpx.Response(302, headers={"Location": LOGIN_PAGE})
    if request.url.host == "sso.mirea.ru" and request.url.path.endswith("/token"):
        return httpx.Response(200, json={"access_token": "new", "refresh_token": "r2"})
    if request.url.host == "sso.mirea.ru":
        if "AUTH_SESSION_ID=stale" in request.headers.get("cookie", ""):
            return httpx.Response(
                302, headers={"Location": "https://pulse.mirea.ru/api/auth/login?redirectUri=%2F"}
            )
        return httpx.Response(200, html="<html><form id=kc-form-login></form></html>")
    if request.url.path.startswith("/rtu_tc."):
        return httpx.Response(
            200, content=EMPTY_DAY, headers={"Content-Type": "application/grpc-web+proto"}
        )
    return httpx.Response(404)


def test_a_sign_in_redirect_loop_is_broken_by_dropping_old_sso_cookies(pulse, caplog):
    """ "Вход в Пульс зациклился на переадресациях" at every start: the session's old
    SSO cookies are dropped, the tokens carry on, and the journal shows the loop."""
    pulse.handle = _looping_keycloak
    session = {
        "AUTH_SESSION_ID": "stale",
        "KEYCLOAK_IDENTITY": "old",
        "access_token": "a",
        "refresh_token": "r",
    }

    with caplog.at_level("WARNING", logger="mirea_lecture_assistant.mirea_service"):
        assert asyncio.run(MireaService(session).verify_state()) is SessionState.VALID

    assert "AUTH_SESSION_ID" not in session and "KEYCLOAK_IDENTITY" not in session
    assert session["refresh_token"]
    assert "pulse_redirect_trace" in caplog.text and "sso.mirea.ru" in caplog.text
    assert "stale" not in caplog.text  # names only, never cookie values


def test_a_schedule_refresh_recovers_from_a_sign_in_redirect_loop(pulse):
    pulse.handle = _looping_keycloak
    session = {"AUTH_SESSION_ID": "stale", "access_token": "a", "refresh_token": "r"}

    assert asyncio.run(MireaService(session).get_schedule(2)) == []


def test_a_loop_at_sso_authentication_expires_the_incomplete_session(pulse):
    def looping(request):
        return httpx.Response(
            302,
            headers={
                "Location": "https://sso.mirea.ru/realms/mirea/login-actions/authenticate"
                f"?tab_id={len(pulse.requests)}",
            },
        )

    pulse.handle = looping
    service = MireaService({"KEYCLOAK_SESSION": "intermediate"})
    assert asyncio.run(service.verify_state()) is SessionState.EXPIRED


def test_a_schedule_sso_loop_recovers_even_if_the_breaker_then_blocks_requests(pulse):
    def looping(request):
        return httpx.Response(
            302,
            headers={"Location": "https://sso.mirea.ru/realms/mirea/login-actions/authenticate"},
        )

    pulse.handle = looping
    service = MireaService({"KEYCLOAK_SESSION": "stale"})
    with pytest.raises(RuntimeError, match="Сессия Пульса истекла"):
        asyncio.run(service.get_schedule(1))
    pulse.requests.clear()
    assert asyncio.run(service.verify_state()) is SessionState.EXPIRED
    assert pulse.requests == []
    # A replacement session is not affected by evidence from the old one.
    service.session = {"KEYCLOAK_SESSION": "new"}
    pulse.handle = _accepting_pulse
    assert asyncio.run(service.verify_state()) is SessionState.VALID


def test_a_loop_on_pulse_maintenance_route_is_inconclusive(pulse):
    pulse.handle = lambda request: httpx.Response(
        302, headers={"Location": "https://pulse.mirea.ru/maintenance"}
    )
    service = MireaService({"KEYCLOAK_SESSION": "preserve"})
    assert asyncio.run(service.verify_state()) is SessionState.UNKNOWN
    assert service.session == {"KEYCLOAK_SESSION": "preserve"}


def test_completed_login_bootstraps_and_confirms_pulse_before_success(pulse):
    from pymirea import AuthResult

    pulse.handle = _accepting_pulse
    service = MireaService({"KEYCLOAK_SESSION": "new"})
    result = AuthResult(success=True, message="OK", cookies=dict(service.session))
    result = asyncio.run(service._validate_completed_login(result))
    assert result.success
    assert result.tokens[".AspNetCore.Cookies"] == "fresh"
    assert any("LessonService" in request for request in pulse.requests)


def test_completed_login_waits_out_an_outage_without_losing_sso(pulse):
    from pymirea import AuthResult

    def offline(request):
        raise httpx.ConnectTimeout("offline", request=request)

    pulse.handle = offline
    service = MireaService({"KEYCLOAK_SESSION": "new"})
    result = AuthResult(success=True, message="OK", cookies=dict(service.session))
    result = asyncio.run(service._validate_completed_login(result))
    assert not result.success
    assert result.session_pending
    assert service.session == {"KEYCLOAK_SESSION": "new"}


def test_a_completed_otp_does_not_make_a_refused_session_successful(pulse):
    from pymirea import AuthResult

    pulse.handle = _refusing_pulse
    service = MireaService({"KEYCLOAK_SESSION": "refused"})
    result = AuthResult(success=True, message="OK", cookies=dict(service.session))
    result = asyncio.run(service._validate_completed_login(result))
    assert not result.success
    assert not result.session_pending
    assert service.session == {}
