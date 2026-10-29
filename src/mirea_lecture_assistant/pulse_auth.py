"""Keep Pulse's server-side OAuth flow and correlation cookies intact.

Pulse, not this desktop client, redeems the authorization code. A Keycloak
cookie alone is not a completed Pulse login.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
from urllib.parse import parse_qs, parse_qsl, urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup
from pymirea.auth import AuthResult, MireaAuth

from .email_otp import code_tag
from .pulse_api import PULSE_COOKIE_NAMES

log = logging.getLogger(__name__)
REDIRECTS = {301, 302, 303, 307, 308}
CALLBACK_PATHS = {
    "/signin-oidc",
    "/api/mireaauth",
    "/api/auth/callback",
    "/api/auth/callback/keycloak",
    "/api/auth/callback/mirea",
    "/api/auth/callback/oidc",
}
RESPONSE_FIELDS = {
    "code",
    "state",
    "session_state",
    "iss",
    "id_token",
    "access_token",
    "token_type",
    "expires_in",
    "scope",
    "client_id",
}


class PulseProtocolError(ValueError):
    """A changed or inconsistent sign-in contract, not a wrong email code."""


class PulseAuth:
    def __init__(self):
        # Reuse upstream timeouts, proxy configuration and HTML parsers only.
        # Never call its attendance-app token exchange or cookie bootstrap.
        self._parser = MireaAuth()
        self.client = self._parser.client
        self._challenge = None
        self._callback_uri = None
        self._oidc_state = None
        self._callback_visits = set()
        self._bootstrapping = False

    async def close(self):
        await self._parser.close()

    @staticmethod
    def _trusted(url: str, *, action: bool = False) -> str:
        parsed = urlsplit(url)
        hosts = {"sso.mirea.ru", "login.mirea.ru"}
        if not action:
            hosts.add("pulse.mirea.ru")
        if (
            parsed.scheme != "https"
            or parsed.hostname not in hosts
            or parsed.username
            or parsed.password
            or parsed.port not in (None, 443)
            or action
            and "/realms/mirea/login-actions/" not in parsed.path
        ):
            raise ValueError("Unexpected sign-in destination")
        return url

    def _form(self, page, *, skip: bool = False):
        soup = BeautifulSoup(page.text, "html.parser")
        for form in soup.find_all("form"):
            if skip:
                if not form.find(["input", "button"], attrs={"name": "skip", "value": "true"}):
                    continue
            elif not form.find("input", attrs={"name": "password"}):
                continue
            action = form.get("action") or self._parser._extract_login_action(page.text)
            if not action:
                continue
            fields = {
                item["name"]: item.get("value", "")
                for item in form.find_all("input", attrs={"type": "hidden"})
                if item.get("name")
            }
            if skip:
                # A clicked submit control is sent by a browser along with hidden
                # fields. Omitting it repeats the required-action page.
                fields["skip"] = "true"
            return self._trusted(urljoin(str(page.url), action), action=True), fields
        action = self._parser._extract_login_action(page.text)
        # React themes expose loginAction rather than a server-rendered form.
        if action and not skip:
            return self._trusted(urljoin(str(page.url), action), action=True), {}
        if action and skip and '"login-max-otp"' in page.text:
            return self._trusted(urljoin(str(page.url), action), action=True), {"skip": "true"}
        return None

    async def _post(self, action, fields, referer):
        return await self.client.post(
            self._trusted(action, action=True),
            data=fields,
            headers={"Referer": referer},
            follow_redirects=False,
        )

    def _remember_authorization(self, source, target):
        """Pin only the authorization request issued by Pulse, never an HTML hint."""
        if (
            source.host not in {"pulse.mirea.ru", "sso.mirea.ru", "login.mirea.ru"}
            or not getattr(self, "_bootstrapping", False)
            or getattr(self, "_callback_uri", None)
        ):
            return
        parsed = urlsplit(self._trusted(target))
        if parsed.hostname not in {"sso.mirea.ru", "login.mirea.ru"}:
            return
        query = parse_qs(parsed.query)
        # With pushed authorization requests (PAR), the public request_uri is
        # opaque. Keycloak then carries the resolved request in client_data on
        # its own login-actions redirect. Read it only in this trusted bootstrap.
        if "client_data" in query and "redirect_uri" not in query:
            values = query["client_data"]
            if len(values) != 1 or len(values[0]) > 16384:
                raise PulseProtocolError("invalid_authorization_context")
            try:
                raw = values[0]
                context = json.loads(
                    base64.b64decode(raw + "=" * (-len(raw) % 4), altchars=b"-_", validate=True)
                )
                if not isinstance(context, dict):
                    raise TypeError()
                for key, short in (
                    ("redirect_uri", "ru"),
                    ("state", "st"),
                    ("response_type", "rt"),
                ):
                    if not isinstance(context.get(short), str) or not context[short]:
                        raise ValueError()
                    query[key] = [context[short]]
            except (ValueError, TypeError, UnicodeError):
                raise PulseProtocolError("invalid_authorization_context") from None
        if not {"redirect_uri", "state", "client_id", "response_type"} <= query.keys():
            return  # Older flows without these hints retain the conservative route list.
        if any(
            len(query[key]) != 1 for key in ("redirect_uri", "state", "client_id", "response_type")
        ):
            raise PulseProtocolError("ambiguous_authorization_request")
        if "code" not in query["response_type"][0].split():
            raise PulseProtocolError("unsupported_authorization_response")
        callback = urlsplit(self._trusted(query["redirect_uri"][0]))
        if callback.hostname != "pulse.mirea.ru" or callback.fragment:
            raise PulseProtocolError("unexpected_callback_destination")
        self._callback_uri = query["redirect_uri"][0]
        self._oidc_state = query["state"][0]
        log.info("pulse_auth_contract_detected callback_declared=True")

    def _callback_matches(self, target, *, response_query=False):
        parsed = urlsplit(self._trusted(target))
        if parsed.hostname != "pulse.mirea.ru" or parsed.fragment:
            return False
        declared = getattr(self, "_callback_uri", None)
        if declared:
            expected = urlsplit(declared)
            if (parsed.path, parsed.port or 443) != (expected.path, expected.port or 443):
                return False
            expected_query = parse_qs(expected.query, keep_blank_values=True)
        else:
            if parsed.path.rstrip("/") not in CALLBACK_PATHS:
                return False
            expected_query = {}
        actual = parse_qs(parsed.query, keep_blank_values=True)
        if any(actual.get(key) != value for key, value in expected_query.items()):
            return False
        extras = actual.keys() - expected_query.keys()
        return extras <= RESPONSE_FIELDS if response_query else not extras

    def _validate_callback_state(self, fields):
        expected = getattr(self, "_oidc_state", None)
        if expected is not None and not secrets.compare_digest(
            str(fields.get("state", "")).encode(), expected.encode()
        ):
            raise PulseProtocolError("callback_state_mismatch")

    def _claim_callback(self, method, target, fields):
        self._validate_callback_state(fields)
        # A repeated form must not redeem the same one-time response in a loop.
        parsed = urlsplit(target)
        destination = (
            parsed.scheme,
            parsed.hostname,
            parsed.port or 443,
            parsed.path,
            sorted(parse_qsl(parsed.query, keep_blank_values=True)),
        )
        digest = hashlib.sha256(
            repr((method, destination, sorted(fields.items()))).encode()
        ).digest()
        if digest in self._callback_visits:
            raise PulseProtocolError("repeated_callback")
        self._callback_visits.add(digest)

    def _callback_form(self, page):
        # OIDC response_mode=form_post returns a page that a browser submits to
        # Pulse. Only accept authorization responses to the known callback routes.
        if page.url.host not in {"sso.mirea.ru", "login.mirea.ru"}:
            return None
        soup = BeautifulSoup(page.text, "html.parser")
        for form in soup.find_all("form"):
            if str(form.get("method", "get")).strip().lower() != "post":
                continue
            action = urljoin(str(page.url), form.get("action", ""))
            if not self._callback_matches(action):
                continue
            fields = {}
            for item in form.find_all("input"):
                name = item.get("name")
                if (
                    not name
                    or str(item.get("type", "hidden")).lower() != "hidden"
                    or item.has_attr("disabled")
                ):
                    continue
                if name in fields:
                    raise PulseProtocolError("duplicate_callback_field")
                fields[name] = item.get("value", "")
            if fields.get("code") and fields.get("state") and fields.keys() <= RESPONSE_FIELDS:
                self._validate_callback_state(fields)
                return action, fields
        return None

    async def _settle(self, response):
        for _ in range(16):
            log.info(
                "pulse_auth_step host=%s path=%s status=%s",
                response.url.host,
                response.url.path,
                response.status_code,
            )
            log.info(
                "pulse_auth_response pulse_cookie_present=%s query_keys=%s",
                any(item.name in PULSE_COOKIE_NAMES for item in self.client.cookies.jar),
                ",".join(sorted(response.url.params.keys())) or "-",
            )
            if response.status_code in REDIRECTS:
                location = response.headers.get("location")
                if not location:
                    return response
                target = self._trusted(urljoin(str(response.url), location))
                self._remember_authorization(response.url, target)
                query = parse_qs(urlsplit(target).query)
                if (
                    response.url.host in {"sso.mirea.ru", "login.mirea.ru"}
                    and urlsplit(target).hostname == "pulse.mirea.ru"
                    and "code" in query
                ):
                    if (
                        not self._callback_matches(target, response_query=True)
                        or not query.get("state")
                        or any(len(value) != 1 for value in query.values())
                    ):
                        raise PulseProtocolError("unexpected_redirect_response")
                    self._claim_callback(
                        "GET", target, {key: value[0] for key, value in query.items()}
                    )
                if response.status_code in {307, 308} and response.request.method == "POST":
                    # Browser redirect semantics: preserve a POST, but never copy its
                    # potentially sensitive body to another origin or arbitrary SSO route.
                    if response.url.host != urlsplit(target).hostname:
                        raise PulseProtocolError("cross_origin_post_redirect")
                    if response.url.host != "pulse.mirea.ru":
                        self._trusted(target, action=True)
                    response = await self.client.post(
                        target,
                        content=response.request.content,
                        headers={
                            "Content-Type": response.request.headers.get(
                                "content-type", "application/x-www-form-urlencoded"
                            )
                        },
                        follow_redirects=False,
                    )
                else:
                    response = await self.client.get(target, follow_redirects=False)
                continue
            if response.status_code != 200:
                return response
            callback = self._callback_form(response)
            if callback:
                action, fields = callback
                self._claim_callback("POST", action, fields)
                log.info("pulse_auth_callback_submitted mode=form_post")
                response = await self.client.post(
                    action,
                    data=fields,
                    headers={"Referer": str(response.url)},
                    follow_redirects=False,
                )
                continue
            soup = BeautifulSoup(response.text, "html.parser")
            if response.url.host in {"sso.mirea.ru", "login.mirea.ru"} and any(
                form.find("input", attrs={"name": "code"})
                and form.find("input", attrs={"name": "state"})
                and urlsplit(urljoin(str(response.url), form.get("action", ""))).hostname
                == "pulse.mirea.ru"
                for form in soup.find_all("form")
            ):
                # An unsupported OAuth response is not an email OTP prompt. Do not
                # spend another email code or guess a new destination.
                raise PulseProtocolError("unrecognized_callback_form")
            execution = parse_qs(response.url.query.decode()).get("execution", [])
            if execution == ["max-account-config"]:
                form = self._form(response, skip=True)
                if form:
                    response = await self._post(*form, str(response.url))
                    continue
            return response
        raise httpx.TooManyRedirects("Pulse sign-in redirect limit", request=response.request)

    def _result(self, page):
        # Do not accept SSO identity cookies, HTML or a 302 as proof of login.
        cookies = {
            c.name: c.value
            for c in self.client.cookies.jar
            if c.domain.lstrip(".") in {"pulse.mirea.ru", "mirea.ru"}
            and any(c.name == name or c.name.startswith(name + "C") for name in PULSE_COOKIE_NAMES)
        }
        if (
            page.url.host == "pulse.mirea.ru"
            and page.status_code == 200
            and any(cookies.get(name) for name in PULSE_COOKIE_NAMES)
        ):
            self._challenge = None
            log.info("pulse_auth_cookie_received")
            return AuthResult(True, "Сессия Пульса получена", cookies=cookies)
        if page.status_code >= 500:
            return AuthResult(False, "Сервер МИРЭА временно недоступен. Повторите позже.")
        if page.status_code == 429:
            return AuthResult(False, "МИРЭА ограничила запросы (429). Повторим позже.")
        if page.url.host in {"sso.mirea.ru", "login.mirea.ru"} and page.status_code == 200:
            error = self._parser._extract_keycloak_error(page.text)
            challenge = self._parser._extract_otp_challenge(page.text, base_url=str(page.url))
            if challenge:
                self._trusted(challenge.action_url, action=True)
                challenge.referer = str(page.url)
                # «Введите код (#1F)»: only the letter with the same mark is this code.
                challenge.code_tag = code_tag(page.text)
                self._challenge = challenge
                return AuthResult(False, error or "Введите код подтверждения", challenge=challenge)
            if error:
                return AuthResult(False, error)
        log.warning(
            "pulse_auth_incomplete host=%s path=%s status=%s",
            page.url.host,
            page.url.path,
            page.status_code,
        )
        soup = BeautifulSoup(page.text, "html.parser")
        # Shape only: never log HTML, field values, cookies or OAuth parameters.
        log.warning(
            "pulse_auth_page_shape forms=%s has_callback_fields=%s has_skip_control=%s",
            len(soup.find_all("form")),
            bool(
                soup.find("input", attrs={"name": "code"})
                and soup.find("input", attrs={"name": "state"})
            ),
            bool(soup.find(["input", "button"], attrs={"name": "skip", "value": "true"})),
        )
        for form in soup.find_all("form"):
            target = urlsplit(urljoin(str(page.url), form.get("action", "")))
            names = {str(i.get("name", "")) for i in form.find_all("input")}
            known = {
                "code",
                "state",
                "session_state",
                "iss",
                "id_token",
                "access_token",
                "token_type",
                "expires_in",
                "scope",
                "client_id",
                "skip",
            }
            log.warning(
                "pulse_auth_form_shape method=%s pulse_destination=%s auth_route=%s "
                "known_fields=%s unknown_field_count=%s",
                "post" if str(form.get("method", "get")).strip().lower() == "post" else "other",
                target.hostname == "pulse.mirea.ru",
                target.path.startswith(("/api/auth/", "/api/mireaauth", "/signin-oidc")),
                ",".join(sorted(names & known)),
                len(names - known - {""}),
            )
        return AuthResult(False, "МИРЭА не завершила вход в Пульс. Начните вход заново.")

    @staticmethod
    def _failure(exc):
        log.warning("pulse_auth_failed kind=%s", type(exc).__name__)
        if isinstance(exc, PulseProtocolError):
            # Reasons are fixed internal identifiers, never remote values or HTML.
            log.warning("pulse_auth_contract_changed reason=%s", str(exc))
            return AuthResult(
                False,
                "Пульс изменил или не завершил сценарий входа. Автоматические повторы остановлены; требуется проверка совместимости.",
            )
        if isinstance(exc, httpx.HTTPError) and not isinstance(exc, httpx.TooManyRedirects):
            # Preserve the UI's outage retry classification, including after OTP.
            return AuthResult(False, "Ошибка соединения с МИРЭА. Повторим вход позже.")
        return AuthResult(False, "Не удалось завершить вход в Пульс. Повторите вход.")

    async def login(self, username: str, password: str):
        self._challenge = None
        self._callback_uri = None
        self._oidc_state = None
        self._callback_visits = set()
        self._bootstrapping = True
        try:
            page = await self._settle(
                await self.client.get(
                    "https://pulse.mirea.ru/api/auth/login",
                    params={"redirectUri": "/"},
                    follow_redirects=False,
                )
            )
            self._bootstrapping = False
            if page.url.host == "pulse.mirea.ru":
                return self._result(page)
            form = self._form(page)
            if not form:
                return self._result(page)
            action, fields = form
            fields.update(username=username, password=password)
            return self._result(await self._settle(await self._post(action, fields, str(page.url))))
        except (httpx.HTTPError, ValueError) as exc:
            return self._failure(exc)

        finally:
            self._bootstrapping = False

    async def complete_2fa(self, challenge, code: str):
        if challenge is not self._challenge or challenge is None:
            return AuthResult(False, "Сценарий входа устарел. Начните вход заново.")
        try:
            fields = dict(challenge.hidden_fields)
            fields[challenge.field_name] = code
            page = await self._settle(
                await self._post(
                    challenge.action_url,
                    fields,
                    challenge.referer or "https://sso.mirea.ru/",
                )
            )
            return self._result(page)
        except (httpx.HTTPError, ValueError) as exc:
            return self._failure(exc)
