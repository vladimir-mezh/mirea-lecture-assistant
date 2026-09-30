from __future__ import annotations

from dataclasses import dataclass

import pytest

from mirea_lecture_assistant.browser_service import BrowserService

LECTURE = "https://mts-link.ru/event/12345"


@dataclass
class FakePage:
    url: str


@pytest.fixture
def service(tmp_path):
    return BrowserService(tmp_path / "profile")


def test_the_lecture_tab_is_chosen_over_a_helper_tab(service):
    service.lecture_url = LECTURE
    pages = [
        FakePage(LECTURE),
        FakePage("https://online-edu.mirea.ru/mod/webinars/view.php?id=964187"),
    ]

    assert service._pick_lecture_page(pages).url == LECTURE


def test_navigation_inside_the_platform_keeps_the_tab(service):
    service.lecture_url = LECTURE
    pages = [FakePage("https://mts-link.ru/event/12345/session/9")]

    assert service._pick_lecture_page(pages) is pages[0]


def test_redirect_between_mts_link_subdomains_is_still_the_same_lecture(service):
    service.lecture_url = "https://my.mts-link.ru/j/123"

    assert service._matches_lecture_url("https://mts-link.ru/event/123/session/9")
    page = FakePage("https://mts-link.ru/event/123/session/9")
    helper = FakePage("https://online-edu.mirea.ru/my/")
    assert service._pick_lecture_page([page, helper]) is page


def test_mts_invitation_and_event_urls_identify_one_room(service):
    invitation = "https://my.mts-link.ru/j/10000002/20000000003/token"
    meeting = "https://my.mts-link.ru/event/20000000003/30000000002"

    assert service._room_key(invitation) == service._room_key(meeting)
    service.lecture_url = invitation
    assert service._matches_lecture_url(meeting)
    assert not service._matches_lecture_url("https://my.mts-link.ru/event/99999999/1")


def test_reopening_the_same_mts_room_reuses_the_existing_tab(service, monkeypatch):
    import asyncio

    invitation = "https://my.mts-link.ru/j/10000002/20000000003/token"
    meeting = "https://my.mts-link.ru/event/20000000003/30000000002"

    class Page:
        url = meeting

        def __init__(self):
            self.context = self
            self.pages = [self]
            self.navigations = 0
            self.reloads = 0

        def is_closed(self):
            return False

        async def goto(self, _url):
            self.navigations += 1

        async def reload(self, **_kwargs):
            self.reloads += 1

    class Playwright:
        async def stop(self):
            return None

    page = Page()

    async def active_page():
        return page, Playwright()

    monkeypatch.setattr(service, "_active_page", active_page)

    asyncio.run(service._navigate_async(invitation))
    asyncio.run(service._navigate_async(invitation, force_navigation=True))

    assert page.navigations == 0
    assert page.reloads == 1


def test_an_unrelated_page_is_not_a_live_lecture(service):
    service.lecture_url = LECTURE

    assert not service._matches_lecture_url("https://online-edu.mirea.ru/my/")


def test_the_newest_tab_of_the_platform_wins(service):
    service.lecture_url = LECTURE
    older, newer = FakePage(LECTURE), FakePage("https://mts-link.ru/event/12345?reconnect=1")

    assert service._pick_lecture_page([older, newer]) is newer


def test_without_a_known_lecture_the_last_tab_is_used(service):
    pages = [FakePage("https://example.test/a"), FakePage("https://example.test/b")]

    assert service._pick_lecture_page(pages) is pages[1]


def test_no_tabs_means_no_choice(service):
    assert service._pick_lecture_page([]) is None


def test_reading_a_page_requires_a_real_address(service):
    with pytest.raises(ValueError, match="https://"):
        service.read_html("online-edu.mirea.ru")


LOGIN_HTML = '<div class="loginform">Вход в систему</div>'
WEBINARS_HTML = '<div id="wb2-table"><table class="data"><tbody></tbody></table></div>'


def _answer(monkeypatch, service, html: str):
    from mirea_lecture_assistant import browser_service as module

    def fake_run_async(coroutine, *_args, **_kwargs):
        coroutine.close()  # the fake never reaches Playwright
        return html

    monkeypatch.setattr(module, "run_async", fake_run_async)
    monkeypatch.setattr(type(service), "is_running", property(lambda _self: True))
    service.port = 9222


def test_a_sign_in_page_is_reported_instead_of_looking_empty(service, monkeypatch):
    """Returning the login page would read as «вебинаров пока нет» further up."""
    from mirea_lecture_assistant.browser_service import NotSignedInError

    _answer(monkeypatch, service, LOGIN_HTML)

    with pytest.raises(NotSignedInError, match="СДО"):
        service.read_html("https://online-edu.mirea.ru/mod/webinars/view.php?id=964187")


def test_a_real_page_is_returned_as_is(service, monkeypatch):
    _answer(monkeypatch, service, WEBINARS_HTML)

    html = service.read_html("https://online-edu.mirea.ru/mod/webinars/view.php?id=964187")

    assert html == WEBINARS_HTML


def test_a_browser_started_earlier_is_adopted(service, monkeypatch):
    """A second service must attach, not try to launch Chrome on a busy profile."""
    (service.profile_dir / "DevToolsActivePort").write_text("51234\n/devtools/browser/abc")
    monkeypatch.setattr(type(service), "_cdp_available", staticmethod(lambda port: port == 51234))

    assert service.is_running
    assert service.port == 51234


def test_a_stale_port_file_is_ignored(service, monkeypatch):
    (service.profile_dir / "DevToolsActivePort").write_text("51234\n/devtools/browser/abc")
    monkeypatch.setattr(type(service), "_cdp_available", staticmethod(lambda _port: False))

    assert not service.is_running
    assert service.port is None


def test_a_profile_without_a_port_file_is_not_running(service, monkeypatch):
    monkeypatch.setattr(type(service), "_cdp_available", staticmethod(lambda _port: True))

    assert not service.is_running


def test_a_damaged_port_file_is_ignored(service, monkeypatch):
    (service.profile_dir / "DevToolsActivePort").write_text("")
    monkeypatch.setattr(type(service), "_cdp_available", staticmethod(lambda _port: True))

    assert not service.is_running


def test_content_is_retried_while_the_page_redirects(service):
    """An SSO bounce makes content() fail outright instead of returning the old DOM."""
    import asyncio

    from playwright.async_api import Error as PlaywrightError

    class RedirectingPage:
        def __init__(self):
            self.attempts = 0

        async def content(self):
            self.attempts += 1
            if self.attempts < 3:
                raise PlaywrightError("Unable to retrieve content because the page is navigating")
            return "<html>готово</html>"

        async def wait_for_timeout(self, _ms):
            return None

    page = RedirectingPage()
    assert asyncio.run(service._settled_content(page)) == "<html>готово</html>"
    assert page.attempts == 3


def test_a_page_that_never_settles_reports_the_failure(service):
    import asyncio

    from playwright.async_api import Error as PlaywrightError

    class NeverSettles:
        async def content(self):
            raise PlaywrightError("page is navigating")

        async def wait_for_timeout(self, _ms):
            return None

    with pytest.raises(PlaywrightError):
        asyncio.run(service._settled_content(NeverSettles()))


def test_capture_defaults_to_full_resolution(service):
    """A 520x360 window screenshots at ~504x265, too small for a QR in a stream."""
    assert service.capture_size == (1920, 1080)


def test_capture_override_is_skipped_when_disabled(service):
    import asyncio

    class Page:
        class context:
            @staticmethod
            async def new_cdp_session(_page):
                raise AssertionError("no CDP session should be opened")

    service.capture_size = None
    asyncio.run(service._apply_capture_size(Page()))


def test_capture_override_sends_the_configured_size(service):
    import asyncio

    sent = {}

    class Session:
        async def send(self, method, params):
            sent[method] = params

        async def detach(self):
            sent["detached"] = True

    class Page:
        class context:
            @staticmethod
            async def new_cdp_session(_page):
                return Session()

    service.capture_size = (1600, 900)
    asyncio.run(service._apply_capture_size(Page()))

    assert sent["Emulation.setDeviceMetricsOverride"]["width"] == 1600
    assert sent["Emulation.setDeviceMetricsOverride"]["height"] == 900
    assert sent["detached"]


def test_browser_restart_terminates_a_stuck_owned_process(service, monkeypatch):
    class Process:
        terminated = False

        def poll(self):
            return None

        def terminate(self):
            self.terminated = True

        def wait(self, timeout):
            assert timeout == 3

    process = Process()
    service.process = process
    monkeypatch.setattr(type(service), "is_running", property(lambda _self: False))
    monkeypatch.setattr(service, "open", lambda url, **_kwargs: f"reopened:{url}")

    result = service.restart("https://mts-link.ru/event/lesson")

    assert result == "reopened:https://mts-link.ru/event/lesson"
    assert process.terminated
    assert service.process is None


class ClosablePage:
    def __init__(self, url):
        self.url = url
        self.closed = False

    async def close(self):
        self.closed = True


class FakeContext:
    def __init__(self, pages):
        self.pages = pages
        self.opened = 0

    async def new_page(self):
        self.opened += 1
        blank = ClosablePage("about:blank")
        self.pages.append(blank)
        return blank


def test_the_finished_lecture_tab_is_closed(service):
    """A pair that is over must not keep playing in a background tab."""
    import asyncio

    service.lecture_url = LECTURE
    lecture, other = ClosablePage(LECTURE), ClosablePage("https://online-edu.mirea.ru/my/")
    context = FakeContext([lecture, other])

    assert asyncio.run(service._close_lecture_page(context)) is True
    assert lecture.closed and not other.closed
    assert context.opened == 0
    assert service.lecture_url is None


def test_closing_the_only_tab_keeps_the_browser_alive(service):
    """Closing the last tab would close the browser and its СДО session with it."""
    import asyncio

    service.lecture_url = LECTURE
    lecture = ClosablePage(LECTURE)
    context = FakeContext([lecture])

    assert asyncio.run(service._close_lecture_page(context)) is True
    assert context.opened == 1
    assert lecture.closed


def test_nothing_to_close_is_reported(service):
    import asyncio

    service.lecture_url = LECTURE
    assert asyncio.run(service._close_lecture_page(FakeContext([]))) is False
