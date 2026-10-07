import asyncio
from urllib.parse import parse_qs

import httpx
import pytest

from mirea_lecture_assistant.pulse_auth import PulseAuth

ACTION = "https://sso.mirea.ru/realms/mirea/login-actions/authenticate"
CALLBACK = "https://pulse.mirea.ru/api/mireaauth?code=synthetic&state=matching"


@pytest.fixture(autouse=True)
def configured():
    from mirea_lecture_assistant.mirea_service import MireaService

    MireaService.configure("x" * 44)


def run(coro):
    return asyncio.run(coro)


@pytest.mark.parametrize("max_page", [False, True])
@pytest.mark.parametrize("cookie_name", [".AspNetCore.Cookies", "Pulse.Auth.Cookie"])
def test_native_flow_preserves_correlation_and_redeems_only_at_pulse(max_page, cookie_name):
    calls = []

    def dispatch(req):
        calls.append((req.method, req.url.path))
        if req.url.path == "/api/auth/login":
            assert req.url.params["redirectUri"] == "/"
            return httpx.Response(302, headers={
                "location": ACTION,
                "set-cookie": "correlation=synthetic; Secure; Path=/; HttpOnly",
            })
        if req.url.host == "sso.mirea.ru":
            assert "correlation=" not in req.headers.get("cookie", "")
            if req.method == "GET" and "execution" not in req.url.params:
                return httpx.Response(200, text=f'<form action="{ACTION}"><input name="password"></form>')
            fields = parse_qs(req.content.decode())
            if req.method == "POST" and fields.get("password"):
                assert fields == {"username": ["synthetic-user"], "password": ["synthetic-password"]}
                return httpx.Response(200, text=f'"email-code-form" "loginAction": "{ACTION}"')
            if fields.get("emailCode"):
                assert fields["emailCode"] == ["123456"]
                target = ACTION + "?execution=max-account-config" if max_page else CALLBACK
                return httpx.Response(302, headers={"location": target})
            if req.method == "GET":
                return httpx.Response(200, text=f'<form action="{ACTION}"><input type="hidden" name="skip" value="true"></form>')
            assert fields == {"skip": ["true"]}
            return httpx.Response(302, headers={"location": CALLBACK})
        if req.url.path == "/api/mireaauth":
            assert "correlation=synthetic" in req.headers["cookie"]
            return httpx.Response(302, headers={
                "location": "/", "set-cookie": f"{cookie_name}=synthetic-session; Secure; Path=/; HttpOnly",
            })
        assert req.url.path == "/"
        return httpx.Response(200, text="Pulse")

    async def scenario():
        auth = PulseAuth()
        await auth.client.aclose()
        auth.client = httpx.AsyncClient(transport=httpx.MockTransport(dispatch))
        try:
            result = await auth.login("synthetic-user", "synthetic-password")
            assert not result.success and result.challenge.kind == "email_code"
            result = await auth.complete_2fa(result.challenge, "123456")
            assert result.success
            assert result.tokens == {cookie_name: "synthetic-session"}
            assert not any(path.endswith("/token") for _, path in calls)
        finally:
            await auth.client.aclose()
            await auth.close()

    run(scenario())


@pytest.mark.parametrize("target", ["https://evil.example/", "http://sso.mirea.ru/", "https://sso.mirea.ru@evil.example/", "https://pulse.mirea.ru:444/"])
def test_redirects_cannot_send_credentials_to_untrusted_hosts(target):
    calls = []

    def dispatch(req):
        calls.append(req)
        return httpx.Response(302, headers={"location": target})

    async def scenario():
        auth = PulseAuth()
        await auth.client.aclose()
        auth.client = httpx.AsyncClient(transport=httpx.MockTransport(dispatch))
        try:
            assert not (await auth.login("synthetic", "synthetic")).success
            assert len(calls) == 1
        finally:
            await auth.client.aclose()
            await auth.close()

    run(scenario())


@pytest.mark.parametrize("status", [200, 404, 503])
def test_sso_cookie_or_html_is_not_a_successful_pulse_login(status):
    async def scenario():
        auth = PulseAuth()
        await auth.client.aclose()
        auth.client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda req: httpx.Response(status, text="not signed in", headers={"set-cookie": "KEYCLOAK_SESSION=synthetic; Path=/"})
        ))
        try:
            assert not (await auth.login("synthetic", "synthetic")).success
            assert not (await auth.complete_2fa(None, "123456")).success
        finally:
            await auth.client.aclose()
            await auth.close()

    run(scenario())


def test_network_failure_remains_retryable_but_bad_destination_does_not():
    from mirea_lecture_assistant.reliability import transient_login_failure

    assert transient_login_failure(PulseAuth._failure(httpx.ConnectTimeout("synthetic")).message)
    assert not transient_login_failure(PulseAuth._failure(ValueError("synthetic")).message)
