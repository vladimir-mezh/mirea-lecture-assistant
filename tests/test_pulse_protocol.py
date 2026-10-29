from __future__ import annotations

import asyncio
import base64
import json
import sys
from urllib.parse import parse_qs, urlencode

import httpx
import pytest

from mirea_lecture_assistant.mirea_service import MireaService
from mirea_lecture_assistant.pulse_auth import PulseAuth, PulseProtocolError

ACTION = "https://sso.mirea.ru/realms/mirea/login-actions/authenticate"
STATE = "synthetic-state-do-not-log"
CODE = "synthetic-authorization-do-not-log"
PASSWORD = "synthetic-password-do-not-log"

pytest_plugins = ["test_ui_reliability"]


@pytest.fixture(autouse=True)
def configure():
    MireaService.configure("x" * 44)


def client_data(callback, **changes):
    fields = {"ru": callback, "rt": "code", "rm": "form_post", "st": STATE, **changes}
    return base64.urlsafe_b64encode(json.dumps(fields).encode()).decode().rstrip("=")


@pytest.mark.parametrize("pushed", [False, True])
@pytest.mark.parametrize("mode", ["query", "form_post"])
@pytest.mark.parametrize(
    "callback",
    [
        "https://pulse.mirea.ru/signin-oidc",
        "https://pulse.mirea.ru/future/version4/finish?platform=desktop",
    ],
)
def test_native_flow_survives_changed_callback_and_response_mode(pushed, mode, callback, caplog):
    calls = []
    response_query = urlencode({"code": CODE, "state": STATE})

    def dispatch(request):
        calls.append((request.method, request.url.host, request.url.path))
        if request.url.path == "/api/auth/login":
            query = {"client_id": "pulse"}
            query.update(
                {"request_uri": "urn:synthetic"}
                if pushed
                else {"redirect_uri": callback, "response_type": "code", "state": STATE}
            )
            return httpx.Response(
                302,
                headers={
                    "location": "https://sso.mirea.ru/authorize?" + urlencode(query),
                    "set-cookie": "correlation=synthetic; Secure; HttpOnly; Path=/",
                },
            )
        if request.url.path == "/authorize":
            target = ACTION
            if pushed:
                target += "?" + urlencode(
                    {"client_id": "pulse", "client_data": client_data(callback)}
                )
            return httpx.Response(302, headers={"location": target})
        if request.url.host == "sso.mirea.ru":
            if request.method == "GET":
                return httpx.Response(
                    200, text=f'<form action="{ACTION}"><input name="password"></form>'
                )
            fields = parse_qs(request.content.decode())
            if "password" in fields:
                assert fields["password"] == [PASSWORD]
                return httpx.Response(200, text=f'"email-code-form" "loginAction": "{ACTION}"')
            assert fields["emailCode"] == ["123456"]
            if mode == "query":
                return httpx.Response(
                    302,
                    headers={
                        "location": callback + ("&" if "?" in callback else "?") + response_query
                    },
                )
            return httpx.Response(
                200,
                text=(
                    f'<form action="{callback}" method="post">'
                    f'<input type="hidden" name="code" value="{CODE}">'
                    f'<input type="hidden" name="state" value="{STATE}"></form>'
                ),
            )
        if request.url.path != "/":
            assert "correlation=synthetic" in request.headers["cookie"]
            fields = (
                parse_qs(request.content.decode())
                if request.method == "POST"
                else dict(request.url.params)
            )
            assert fields["state"] in (STATE, [STATE])
            assert fields["code"] in (CODE, [CODE])
            return httpx.Response(
                303,
                headers={
                    "location": "/",
                    "set-cookie": "Pulse.Auth.Cookie=synthetic-session; Secure; HttpOnly; Path=/",
                },
            )
        return httpx.Response(200, text="Pulse")

    async def scenario():
        auth = PulseAuth()
        await auth.client.aclose()
        auth.client = httpx.AsyncClient(transport=httpx.MockTransport(dispatch))
        try:
            challenge = await auth.login("synthetic-user", PASSWORD)
            assert not challenge.success and challenge.challenge.kind == "email_code"
            assert auth._callback_uri == callback and auth._oidc_state == STATE
            assert not auth._bootstrapping
            result = await auth.complete_2fa(challenge.challenge, "123456")
            assert result.success and result.tokens == {"Pulse.Auth.Cookie": "synthetic-session"}
            assert not any(path.endswith("/token") for _, _, path in calls)
        finally:
            await auth.close()

    asyncio.run(scenario())
    assert all(value not in caplog.text for value in (STATE, CODE, PASSWORD))


@pytest.mark.parametrize(
    "callback",
    [
        "https://evil.example/finish",
        "http://pulse.mirea.ru/finish",
        "https://pulse.mirea.ru:444/finish",
        "https://pulse.mirea.ru/finish#fragment",
    ],
)
def test_declared_callback_is_never_trusted_outside_https_pulse(callback):
    auth = PulseAuth()
    auth._bootstrapping = True
    query = urlencode(
        {"client_id": "pulse", "redirect_uri": callback, "state": STATE, "response_type": "code"}
    )
    try:
        with pytest.raises(ValueError):
            auth._remember_authorization(
                httpx.URL("https://pulse.mirea.ru/api/auth/login"),
                "https://sso.mirea.ru/authorize?" + query,
            )
    finally:
        asyncio.run(auth.close())


@pytest.mark.parametrize(
    "context", ["invalid!", "W10", client_data("https://pulse.mirea.ru/finish", st=None)]
)
def test_invalid_pushed_context_is_rejected_without_guessing(context):
    auth = PulseAuth()
    auth._bootstrapping = True
    try:
        with pytest.raises(PulseProtocolError):
            auth._remember_authorization(
                httpx.URL("https://sso.mirea.ru/authorize"),
                ACTION + "?" + urlencode({"client_id": "pulse", "client_data": context}),
            )
    finally:
        asyncio.run(auth.close())


@pytest.mark.parametrize("problem", ["state", "route", "duplicate"])
def test_callback_context_cannot_be_swapped_or_duplicated(problem):
    auth = PulseAuth()
    auth._callback_uri = "https://pulse.mirea.ru/actual/finish"
    auth._oidc_state = STATE
    target = "https://pulse.mirea.ru/signin-oidc" if problem == "route" else auth._callback_uri
    state = "different" if problem == "state" else STATE
    extra = '<input type="hidden" name="state" value="other">' if problem == "duplicate" else ""
    page = httpx.Response(
        200,
        request=httpx.Request("GET", ACTION),
        text=(
            f'<form method="post" action="{target}"><input type="hidden" name="code" value="{CODE}">'
            f'<input type="hidden" name="state" value="{state}">{extra}</form>'
        ),
    )
    try:
        if problem == "route":
            assert auth._callback_form(page) is None
        else:
            with pytest.raises(PulseProtocolError):
                auth._callback_form(page)
    finally:
        asyncio.run(auth.close())


@pytest.mark.parametrize("status", [307, 308])
def test_post_redirects_keep_the_body_within_the_same_trusted_origin(status):
    async def scenario():
        auth = PulseAuth()
        await auth.client.aclose()
        original = httpx.Request("POST", ACTION, data={"emailCode": "123456"})

        def dispatch(request):
            assert request.method == "POST" and request.content == original.content
            return httpx.Response(200, text="finished fixture")

        auth.client = httpx.AsyncClient(transport=httpx.MockTransport(dispatch))
        try:
            page = await auth._settle(
                httpx.Response(status, request=original, headers={"location": ACTION + "?next=1"})
            )
            assert page.status_code == 200
        finally:
            await auth.close()

    asyncio.run(scenario())


def test_unsupported_callback_is_not_mistaken_for_an_email_prompt(caplog):
    auth = PulseAuth()
    page = httpx.Response(
        200,
        request=httpx.Request("GET", ACTION),
        text=(
            f'<form method="post" action="https://pulse.mirea.ru/new-unannounced-route">'
            f'<input name="code" value="{CODE}"><input name="state" value="{STATE}"></form>'
        ),
    )
    try:
        with pytest.raises(PulseProtocolError) as error:
            asyncio.run(auth._settle(page))
        result = auth._failure(error.value)
        assert not result.success and not result.challenge
        assert "совместимости" in result.message
    finally:
        asyncio.run(auth.close())
    assert CODE not in caplog.text and STATE not in caplog.text


def test_repeated_form_post_is_not_redeemed_again():
    posts = []
    html = (
        f'<form method="post" action="https://pulse.mirea.ru/signin-oidc">'
        f'<input name="code" value="{CODE}"><input name="state" value="{STATE}"></form>'
    )

    async def scenario():
        auth = PulseAuth()
        await auth.client.aclose()

        def dispatch(request):
            if request.method == "POST":
                posts.append(True)
                return httpx.Response(302, headers={"location": ACTION})
            return httpx.Response(200, text=html)

        auth.client = httpx.AsyncClient(transport=httpx.MockTransport(dispatch))
        try:
            with pytest.raises(PulseProtocolError):
                await auth._settle(
                    httpx.Response(200, request=httpx.Request("GET", ACTION), text=html)
                )
        finally:
            await auth.close()

    asyncio.run(scenario())
    assert posts == [True]


def test_preserved_post_does_not_carry_a_password_to_another_sso_origin():
    async def scenario():
        auth = PulseAuth()
        await auth.client.aclose()
        auth.client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _request: pytest.fail("Body must not be forwarded")
            )
        )
        request = httpx.Request("POST", ACTION, data={"password": PASSWORD})
        try:
            with pytest.raises(PulseProtocolError):
                await auth._settle(
                    httpx.Response(
                        307,
                        request=request,
                        headers={
                            "location": "https://login.mirea.ru/realms/mirea/login-actions/authenticate"
                        },
                    )
                )
        finally:
            await auth.close()

    asyncio.run(scenario())


def test_auth_check_mode_never_opens_or_submits_lecture_activity(window, monkeypatch):
    monkeypatch.setattr(sys, "argv", [sys.argv[0], "--check-login"])
    monkeypatch.setattr(
        window, "_run", lambda *_a, **_k: pytest.fail("No lecture work in auth check")
    )
    monkeypatch.setattr(window, "_auto_login", lambda: pytest.fail("User starts the login"))
    window._startup_auth()
    window._evaluate_cached_lessons()
    window._evaluate_current_lessons([])
    window._restore_active_lecture()
    window._open_lecture("https://my.mts-link.ru/event/synthetic")
    window.toggle_scanner()
    window._handle_qr("synthetic-not-a-real-qr")
    window._submit_attendance(123)
    window._send_chat_fallback()
    assert not window.scan_timer.isActive()
    assert window._current_online_lesson() is None
    assert not window.db.recent_qr_events()
