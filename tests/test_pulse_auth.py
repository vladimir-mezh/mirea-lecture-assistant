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
@pytest.mark.parametrize("form_post", [False, True])
@pytest.mark.parametrize("cookie_name", [".AspNetCore.Cookies", "Pulse.Auth.Cookie"])
@pytest.mark.parametrize(
    "skip_control",
    [
        '<input type="hidden" name="skip" value="true">',
        '<input type="submit" name="skip" value="true">',
        '<button type="submit" name="skip" value="true">Skip</button>',
    ],
)
def test_native_flow_preserves_correlation_and_redeems_only_at_pulse(
    max_page, cookie_name, skip_control, form_post
):
    calls = []

    def completed_sso():
        if form_post:
            return httpx.Response(
                200,
                text=(
                    '<form method="post" action="https://pulse.mirea.ru/api/mireaauth">'
                    '<input type="hidden" name="code" value="synthetic">'
                    '<input type="hidden" name="state" value="matching"></form>'
                ),
            )
        return httpx.Response(302, headers={"location": CALLBACK})

    def dispatch(req):
        calls.append((req.method, req.url.path))
        if req.url.path == "/api/auth/login":
            assert req.url.params["redirectUri"] == "/"
            return httpx.Response(
                302,
                headers={
                    "location": ACTION,
                    "set-cookie": "correlation=synthetic; Secure; Path=/; HttpOnly",
                },
            )
        if req.url.host == "sso.mirea.ru":
            assert "correlation=" not in req.headers.get("cookie", "")
            if req.method == "GET" and "execution" not in req.url.params:
                return httpx.Response(
                    200, text=f'<form action="{ACTION}"><input name="password"></form>'
                )
            fields = parse_qs(req.content.decode())
            if req.method == "POST" and fields.get("password"):
                assert fields == {
                    "username": ["synthetic-user"],
                    "password": ["synthetic-password"],
                }
                return httpx.Response(200, text=f'"email-code-form" "loginAction": "{ACTION}"')
            if fields.get("emailCode"):
                assert fields["emailCode"] == ["123456"]
                if max_page:
                    return httpx.Response(
                        302, headers={"location": ACTION + "?execution=max-account-config"}
                    )
                return completed_sso()
            if req.method == "GET":
                return httpx.Response(
                    200,
                    text=f'<form action="{ACTION}">{skip_control}</form>',
                )
            assert fields == {"skip": ["true"]}
            return completed_sso()
        if req.url.path == "/api/mireaauth":
            assert "correlation=synthetic" in req.headers["cookie"]
            fields = (
                parse_qs(req.content.decode()) if req.method == "POST" else dict(req.url.params)
            )
            assert fields["code"] in ("synthetic", ["synthetic"])
            assert fields["state"] in ("matching", ["matching"])
            return httpx.Response(
                302,
                headers={
                    "location": "/",
                    "set-cookie": f"{cookie_name}=synthetic-session; Secure; Path=/; HttpOnly",
                },
            )
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


@pytest.mark.parametrize(
    "target",
    [
        "https://evil.example/",
        "http://sso.mirea.ru/",
        "https://sso.mirea.ru@evil.example/",
        "https://pulse.mirea.ru:444/",
    ],
)
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
        auth.client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda req: httpx.Response(
                    status,
                    text="not signed in",
                    headers={"set-cookie": "KEYCLOAK_SESSION=synthetic; Path=/"},
                )
            )
        )
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


@pytest.mark.parametrize(
    "target",
    [
        "https://evil.example/api/mireaauth",
        "http://pulse.mirea.ru/api/mireaauth",
        "https://pulse.mirea.ru:444/api/mireaauth",
        "https://pulse.mirea.ru/unrelated",
    ],
)
def test_callback_form_cannot_post_to_other_destinations(target):
    auth = object.__new__(PulseAuth)
    page = httpx.Response(
        200,
        request=httpx.Request("GET", ACTION),
        text=(
            f'<form method="post" action="{target}">'
            '<input type="hidden" name="code" value="synthetic">'
            '<input type="hidden" name="state" value="matching"></form>'
        ),
    )
    try:
        assert auth._callback_form(page) is None
    except ValueError:
        pass  # Invalid origins are rejected before any request can be sent.


def test_callback_form_requires_code_state_and_rejects_extra_fields():
    auth = object.__new__(PulseAuth)
    for fields in (
        '<input type="hidden" name="code" value="synthetic">',
        '<input type="hidden" name="state" value="matching">',
        (
            '<input type="hidden" name="code" value="synthetic">'
            '<input type="hidden" name="state" value="matching">'
            '<input type="hidden" name="password" value="synthetic">'
        ),
    ):
        page = httpx.Response(
            200,
            request=httpx.Request("GET", ACTION),
            text=(
                f'<form method="post" action="https://pulse.mirea.ru/api/mireaauth">{fields}</form>'
            ),
        )
        assert auth._callback_form(page) is None


@pytest.mark.parametrize(
    "route",
    [
        "/signin-oidc",
        "/api/mireaauth/",
        "/api/auth/callback",
        "/api/auth/callback/keycloak",
        "/api/auth/callback/mirea",
        "/api/auth/callback/oidc",
    ],
)
def test_callback_form_accepts_standard_oidc_response_fields(route):
    auth = object.__new__(PulseAuth)
    target = "https://pulse.mirea.ru" + route
    page = httpx.Response(
        200,
        request=httpx.Request("GET", ACTION),
        text=(
            f'<form method="POST " action="{target}">'
            '<input name="code" value="synthetic">'
            '<input name="state" value="matching">'
            '<input type="hidden" name="id_token" value="synthetic-id-token">'
            '<input type="hidden" name="scope" value="openid"></form>'
        ),
    )
    action, fields = auth._callback_form(page)
    assert action == target
    assert fields == {
        "code": "synthetic",
        "state": "matching",
        "id_token": "synthetic-id-token",
        "scope": "openid",
    }
