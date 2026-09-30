"""Signing in to the МИРЭА СДО through the same SSO the Pulse login uses.

The СДО refuses the session Pulse hands out, so it needs its own sign-in. The
forms belong to Keycloak and use the field names seen in the SSO responses:
`username`, `password` and `emailCode`.
"""

from __future__ import annotations

import asyncio
import logging
from urllib.parse import parse_qs, urlparse

SSO_ENTRY = "https://online-edu.mirea.ru/auth/oidc/"
SIGNED_IN_URL = "https://online-edu.mirea.ru/my/"
USERNAME_FIELD = "input[name='username'], input#username"
PASSWORD_FIELD = "input[name='password'], input#password"
CODE_FIELD = "input[name='emailCode'], input#emailCode, input[name='otp']"
SUBMIT = "button[type='submit'], input[type='submit'], #kc-login"
MAX_SKIP = (
    "form[action*='required-action']:has(input[name='skip'][value='true']) "
    "input[type='submit'][value='Пропустить']"
)

log = logging.getLogger(__name__)


class SignInFailed(RuntimeError):
    """The SSO flow did not end on a signed-in СДО page."""


async def _visible(page, selector: str, timeout: int) -> bool:
    try:
        await page.wait_for_selector(selector, timeout=timeout)
    except TimeoutError:
        return False
    return True


def _on_sdo(url: str) -> bool:
    return (urlparse(url).hostname or "").lower().endswith("online-edu.mirea.ru")


def _on_optional_max(url: str) -> bool:
    parsed = urlparse(url)
    return (
        parsed.hostname == "sso.mirea.ru"
        and parsed.path.endswith("/login-actions/required-action")
        and parse_qs(parsed.query).get("execution") == ["max-account-config"]
    )


async def _skip_optional_max(page, timeout_ms: int) -> None:
    """Skip only MIREA's optional MAX enrollment, never another SSO challenge."""
    if not _on_optional_max(page.url):
        return
    if not await _visible(page, MAX_SKIP, min(timeout_ms, 5_000)):
        raise SignInFailed("На странице МАКС не найдена кнопка «Пропустить»")
    log.info("sdo_optional_max_skipped")
    # Chrome can put a native compromised-password warning above this page.
    # A DOM click submits the *same* optional form without disabling that warning
    # or depending on the browser chrome being unobstructed.
    await page.evaluate("selector => document.querySelector(selector).click()", MAX_SKIP)


async def sign_in(
    page,
    *,
    username: str,
    password: str,
    request_code,
    timeout_ms: int = 20_000,
    reserve_attempt=None,
):
    """Walk the SSO forms in `page`. `request_code` is called only if a code is asked for.

    Returns the stage the flow ended on, so the caller can log what happened
    without ever holding the password or the code.
    """
    await page.goto(SSO_ENTRY, wait_until="domcontentloaded")
    await _skip_optional_max(page, timeout_ms)
    # A live session sends /auth/oidc/ straight back to the СДО with no form.
    if not await _visible(page, USERNAME_FIELD, timeout_ms):
        if _on_sdo(page.url):
            log.info("sdo_sign_in_not_needed")
            return "already"
        raise SignInFailed("Страница входа МИРЭА не открылась")
    # The shared SSO budget is spent only when credentials are actually sent;
    # a live session that needs no form costs nothing.
    if reserve_attempt is not None and not reserve_attempt():
        raise SignInFailed("Лимит попыток входа исчерпан; повторим позже")
    await page.fill(USERNAME_FIELD, username)
    await page.fill(PASSWORD_FIELD, password)
    log.info("sdo_sign_in_credentials_submitted")
    await page.click(SUBMIT)

    if await _visible(page, CODE_FIELD, timeout_ms):
        log.info("sdo_sign_in_code_requested")
        # IMAP polling is synchronous. Keep it off the app-wide asyncio loop so
        # QR capture and browser health checks continue while the email arrives.
        code = await asyncio.to_thread(request_code)
        await page.fill(CODE_FIELD, code)
        await page.click(SUBMIT)

    try:
        await page.wait_for_url(
            lambda url: _on_sdo(str(url)) or _on_optional_max(str(url)),
            timeout=timeout_ms,
        )
        await _skip_optional_max(page, timeout_ms)
        await page.wait_for_url(lambda url: _on_sdo(str(url)), timeout=timeout_ms)
    except Exception as exc:
        raise SignInFailed("Вход не завершился возвратом в СДО") from exc
    await page.goto(SIGNED_IN_URL, wait_until="domcontentloaded")
    if await _visible(page, USERNAME_FIELD, 2_000):
        raise SignInFailed("СДО снова показала форму входа")
    log.info("sdo_sign_in_succeeded")
    return "signed-in"
