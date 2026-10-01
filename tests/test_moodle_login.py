from __future__ import annotations

import asyncio
import threading

import pytest

from mirea_lecture_assistant.moodle_login import CODE_SUBMIT, MAX_SKIP, SignInFailed, sign_in

MAX_URL = (
    "https://sso.mirea.ru/realms/mirea/login-actions/required-action"
    "?execution=max-account-config&client_id=moodle"
)


class FakePage:
    """Minimal stand-in for a browser page driving the SSO forms."""

    def __init__(
        self,
        *,
        screens: list[str],
        final_url: str = "https://online-edu.mirea.ru/my/",
        lands_on: str | None = None,
    ):
        self.screens = screens  # what each wait_for_selector round should find
        self.lands_on = lands_on  # where /auth/oidc/ redirects, as the SSO would
        self.url = "https://online-edu.mirea.ru/auth/oidc/"
        self.filled: dict[str, str] = {}
        self.clicks = 0
        self.dom_submits = 0
        self.visited: list[str] = []
        self.final_url = final_url

    async def goto(self, url, **_kwargs):
        self.visited.append(url)
        self.url = self.lands_on or url

    async def wait_for_selector(self, selector, timeout=0):
        from mirea_lecture_assistant.cdp import CdpTimeout

        if "type='submit'" in selector and self.filled.get("code"):
            return
        for screen in self.screens:
            if screen in selector:
                self.screens.remove(screen)
                return
        raise CdpTimeout("not found")

    async def fill(self, selector, value):
        self.filled["code" if "emailCode" in selector else selector.split("[")[0]] = value
        self.filled[selector] = value

    async def click(self, _selector):
        self.clicks += 1
        self.url = self.final_url

    async def evaluate(self, script, selector):
        assert selector == MAX_SKIP
        assert ".click()" in script
        self.dom_submits += 1
        self.url = self.final_url

    async def wait_for_url(self, predicate, timeout=0):
        if not predicate(self.url):
            raise TimeoutError("still elsewhere")


def _run(page, request_code=lambda: "079234"):
    return asyncio.run(
        sign_in(page, username="user@mirea.ru", password="secret", request_code=request_code)
    )


def test_a_signed_in_profile_is_left_alone():
    page = FakePage(screens=[])

    assert _run(page) == "already"
    assert page.clicks == 0


def test_credentials_and_the_emailed_code_are_submitted():
    page = FakePage(screens=["username", "password", "emailCode"])

    assert _run(page) == "signed-in"
    assert page.filled["input[name='username'], input#username"] == "user@mirea.ru"
    assert page.filled["code"] == "079234"
    assert page.clicks == 2


def test_spa_code_auto_submits_without_a_submit_button():
    class AutoSubmitPage(FakePage):
        async def wait_for_selector(self, selector, timeout=0):
            if "type='submit'" in selector and self.filled.get("code"):
                raise TimeoutError("SPA has no submit button")
            return await super().wait_for_selector(selector, timeout=timeout)

        async def fill(self, selector, value):
            await super().fill(selector, value)
            if "emailCode" in selector:
                self.url = self.final_url

    page = AutoSubmitPage(screens=["username", "password", "emailCode"])

    assert _run(page) == "signed-in"
    assert page.clicks == 1
    assert page.filled["code"] == "079234"


def test_auto_submit_redirect_does_not_click_a_different_challenge():
    class OtherActionPage(FakePage):
        async def fill(self, selector, value):
            await super().fill(selector, value)
            if "emailCode" in selector:
                self.url = MAX_URL.replace("max-account-config", "required-password-change")

        async def wait_for_selector(self, selector, timeout=0):
            if selector == CODE_SUBMIT:
                raise TimeoutError("Not the code form")
            return await super().wait_for_selector(selector, timeout=timeout)

    page = OtherActionPage(screens=["username", "password", "emailCode"])
    with pytest.raises(SignInFailed):
        _run(page)
    assert page.clicks == 1


def test_waiting_for_email_does_not_block_the_async_browser_loop():
    page = FakePage(screens=["username", "password", "emailCode"])
    caller_thread = threading.current_thread()
    code_threads = []

    _run(page, lambda: code_threads.append(threading.current_thread()) or "079234")

    assert code_threads and code_threads[0] is not caller_thread


def test_the_code_is_requested_only_when_asked_for():
    page = FakePage(screens=["username", "password"])
    calls = []

    assert _run(page, lambda: calls.append(1) or "000000") == "signed-in"
    assert calls == []
    assert page.clicks == 1


def test_optional_max_page_after_email_code_is_skipped():
    class MaxPage(FakePage):
        async def wait_for_selector(self, selector, **kwargs):
            if selector == MAX_SKIP and self.url == MAX_URL:
                return object()
            return await super().wait_for_selector(selector, **kwargs)

        async def click(self, selector):
            if selector == MAX_SKIP:
                raise AssertionError("native Chrome warning blocks a pointer click")
            self.clicks += 1
            if self.clicks == 2:
                self.url = MAX_URL
            else:
                self.url = "https://sso.mirea.ru/realms/mirea/login-actions/authenticate"

    page = MaxPage(screens=["username", "password", "emailCode"])

    assert _run(page) == "signed-in"
    assert page.clicks == 2
    assert page.dom_submits == 1
    assert page.filled["code"] == "079234"


def test_pending_optional_max_page_from_existing_session_is_skipped():
    class PendingMaxPage(FakePage):
        async def wait_for_selector(self, selector, **kwargs):
            if selector == MAX_SKIP and self.url == MAX_URL:
                return object()
            return await super().wait_for_selector(selector, **kwargs)

    page = PendingMaxPage(screens=[], lands_on=MAX_URL)

    assert _run(page) == "already"
    assert page.clicks == 0
    assert page.dom_submits == 1


def test_other_required_action_is_not_skipped():
    page = FakePage(
        screens=["username", "password"],
        final_url=MAX_URL.replace("max-account-config", "different-action"),
    )

    with pytest.raises(SignInFailed):
        _run(page)
    assert page.clicks == 1


def test_a_login_form_that_never_appears_is_reported():
    """Redirected away from the СДО and shown no form: something changed upstream."""
    page = FakePage(screens=[], lands_on="https://sso.mirea.ru/realms/mirea/login")

    with pytest.raises(SignInFailed, match="не открылась"):
        _run(page)


def test_staying_outside_the_sdo_is_reported():
    page = FakePage(
        screens=["username", "password"], final_url="https://sso.mirea.ru/realms/mirea/login"
    )

    with pytest.raises(SignInFailed, match="не завершился"):
        _run(page)
