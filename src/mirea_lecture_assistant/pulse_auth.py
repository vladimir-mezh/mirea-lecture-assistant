"""Keep Pulse's server-side OAuth flow and correlation cookies intact.

Pulse, not this desktop client, redeems the authorization code. A Keycloak
cookie alone is not a completed Pulse login.
"""

from __future__ import annotations

import logging
from urllib.parse import parse_qs, urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup
from pymirea.auth import AuthResult, MireaAuth

from .email_otp import code_tag
from .pulse_api import PULSE_COOKIE_NAMES

log = logging.getLogger(__name__)
REDIRECTS = {301, 302, 303, 307, 308}


class PulseAuth:
    def __init__(self):
        # Reuse upstream timeouts, proxy configuration and HTML parsers only.
        # Never call its attendance-app token exchange or cookie bootstrap.
        self._parser = MireaAuth()
        self.client = self._parser.client
        self._challenge = None

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
                if not form.find("input", attrs={"name": "skip", "value": "true"}):
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
                response = await self.client.get(target, follow_redirects=False)
                continue
            if response.status_code != 200:
                return response
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
        return AuthResult(False, "МИРЭА не завершила вход в Пульс. Начните вход заново.")

    @staticmethod
    def _failure(exc):
        log.warning("pulse_auth_failed kind=%s", type(exc).__name__)
        if isinstance(exc, httpx.HTTPError) and not isinstance(exc, httpx.TooManyRedirects):
            # Preserve the UI's outage retry classification, including after OTP.
            return AuthResult(False, "Ошибка соединения с МИРЭА. Повторим вход позже.")
        return AuthResult(False, "Не удалось завершить вход в Пульс. Повторите вход.")

    async def login(self, username: str, password: str):
        self._challenge = None
        try:
            page = await self._settle(
                await self.client.get(
                    "https://pulse.mirea.ru/api/auth/login",
                    params={"redirectUri": "/"},
                    follow_redirects=False,
                )
            )
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
