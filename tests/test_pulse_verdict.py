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

    assert asyncio.run(service.pulse_verdict()) is SessionState.EXPIRED


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
