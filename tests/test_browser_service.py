from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import ClassVar

import pytest

from mirea_lecture_assistant import browser_service
from mirea_lecture_assistant.async_runtime import run_async
from mirea_lecture_assistant.browser_service import BrowserService

LECTURE = "https://mts-link.ru/event/12345"


async def _loaded():
    return None


def test_static_slides_are_not_stalled_when_media_clock_advances(service):
    assert not service._media_stalled(["stream", 1, 10], 0)
    assert not service._media_stalled(["stream", 61, 10], 60)


def test_an_unchanging_media_clock_and_frame_count_is_stalled(service):
    assert not service._media_stalled(["stream", 1, 10], 0)
    assert not service._media_stalled(["stream", 1, 10], 44)
    assert service._media_stalled(["stream", 1, 10], 45)
    assert not service._media_stalled(["new-stream", 1, 10], 46)


def test_pages_without_a_video_stream_do_not_trigger_media_restarts(service):
    assert not service._media_stalled(None, 0)
    assert not service._media_stalled(None, 300)


def test_a_track_without_incoming_data_cannot_hide_behind_its_clock(service):
    assert not service._media_stalled(["stream", 1, 10, True], 0)
    assert service._media_stalled(["stream", 61, 10, True], 60)
    assert not service._media_stalled(["stream", 62, 11, False], 61)


def test_slow_hd_capture_uses_smaller_viewport_then_recovers(service, monkeypatch):
    import asyncio

    class Page:
        def is_closed(self):
            return False

        async def send(self, method, params):
            return None

    page = Page()
    monkeypatch.setattr(browser_service.time, "monotonic", lambda: 20)
    service._capture_slow_until = 60
    asyncio.run(service._apply_capture_size(page))
    assert service._capture_override == (page, (1280, 720))
    monkeypatch.setattr(browser_service.time, "monotonic", lambda: 61)
    asyncio.run(service._apply_capture_size(page))
    assert service._capture_override == (page, (1920, 1080))


def test_capture_timeout_never_returns_a_stale_frame(service, monkeypatch):
    import asyncio

    class Page:
        async def screenshot(self, timeout):
            assert timeout <= 3000
            raise browser_service.CdpTimeout("slow frame")

    async def active_page():
        return Page()

    async def apply_size(_page):
        return None

    monkeypatch.setattr(service, "_active_page", active_page)
    monkeypatch.setattr(service, "_apply_capture_size", apply_size)
    monkeypatch.setattr(browser_service.time, "monotonic", lambda: 10)
    with pytest.raises(browser_service.CdpTimeout):
        asyncio.run(service.capture_page_state())
    assert service._capture_slow_until == 70


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
        target_id = "lecture-tab"

        def __init__(self):
            self.navigations = 0
            self.reloads = 0

        def is_closed(self):
            return False

        async def goto(self, _url):
            self.navigations += 1

        async def reload(self, **_kwargs):
            self.reloads += 1

    page = Page()

    async def active_page():
        return page

    async def connected_browser():
        return type("Browser", (), {"pages": [page]})()

    monkeypatch.setattr(service, "_active_page", active_page)
    monkeypatch.setattr(service, "_connected_browser", connected_browser)

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


def test_without_a_known_lecture_no_tab_is_taken(service):
    """The newest tab used to be taken: a helper tab or one the student opened."""
    pages = [FakePage("https://example.test/a"), FakePage("https://example.test/b")]

    assert service._pick_lecture_page(pages) is None


@pytest.mark.parametrize(
    "url", ["about:blank", "about:blank#redirect", "", "chrome-error://chromewebdata/"]
)
def test_pinned_blank_or_error_tab_is_never_a_capture_target(service, url):
    page = FakePage(url)
    page.target_id = "pinned"
    service._lecture_target = "pinned"
    service.lecture_url = LECTURE
    assert service._pick_lecture_page([page]) is None


def test_popup_wins_when_the_pinned_tab_stays_blank(service):
    blank, real = FakePage("about:blank"), FakePage(LECTURE)
    blank.target_id, real.target_id = "old", "popup"
    service._lecture_target = "old"
    service.lecture_url = LECTURE
    assert service._pick_lecture_page([blank, real]) is real
    assert service._lecture_target == "popup"


def test_first_launch_waits_for_a_loaded_page_not_only_a_browser_port(service, monkeypatch):
    monkeypatch.setattr(type(service), "is_running", property(lambda _self: False))
    monkeypatch.setattr(type(service), "_find_browser", lambda _self: ("Chrome", "chrome"))
    monkeypatch.setattr(type(service), "_free_port", staticmethod(lambda: 12345))
    monkeypatch.setattr(type(service), "_cdp_available", staticmethod(lambda _port: True))
    monkeypatch.setattr(browser_service.subprocess, "Popen", lambda *_a, **_kw: None)

    async def not_loaded():
        assert service.lecture_url == LECTURE
        raise RuntimeError("blank page")

    monkeypatch.setattr(service, "_wait_lecture_loaded_async", not_loaded)
    with pytest.raises(RuntimeError, match="blank page"):
        service.open(LECTURE)


def test_loaded_page_wait_rejects_blank_without_success(service, monkeypatch):
    import asyncio

    service.lecture_url = LECTURE
    blank = FakePage("about:blank")

    async def connected():
        return type("Browser", (), {"pages": [blank]})()

    monkeypatch.setattr(service, "_connected_browser", connected)
    with pytest.raises(RuntimeError, match="не загрузилась"):
        asyncio.run(service._wait_lecture_loaded_async(timeout=0.01))


def test_loading_page_is_not_available_to_the_scanner(service, monkeypatch):
    import asyncio

    service.lecture_url = LECTURE

    class Page:
        url = LECTURE

        async def evaluate(self, *_args, **_kwargs):
            return False

    async def connected():
        return type("Browser", (), {"pages": [Page()]})()

    monkeypatch.setattr(service, "_connected_browser", connected)
    with pytest.raises(RuntimeError, match="ещё не загрузилась"):
        asyncio.run(service._active_page())


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

    from mirea_lecture_assistant.cdp import CdpError

    class RedirectingPage:
        def __init__(self):
            self.attempts = 0

        async def content(self):
            self.attempts += 1
            if self.attempts < 3:
                raise CdpError("Execution context was destroyed")
            return "<html>готово</html>"

        async def wait_for_timeout(self, _ms):
            return None

    page = RedirectingPage()
    assert asyncio.run(service._settled_content(page)) == "<html>готово</html>"
    assert page.attempts == 3


def test_a_page_that_never_settles_reports_the_failure(service):
    import asyncio

    from mirea_lecture_assistant.cdp import CdpError

    class NeverSettles:
        async def content(self):
            raise CdpError("page is navigating")

        async def wait_for_timeout(self, _ms):
            return None

    with pytest.raises(CdpError):
        asyncio.run(service._settled_content(NeverSettles()))


def test_capture_defaults_to_full_resolution(service):
    """A 520x360 window screenshots at ~504x265, too small for a QR in a stream."""
    assert service.capture_size == (1920, 1080)


class OverridePage:
    """A tab that records the DevTools commands sent to it."""

    def __init__(self):
        self.sent = []

    def is_closed(self):
        return False

    async def send(self, method, params=None):
        self.sent.append((method, params))


def test_capture_override_is_skipped_when_disabled(service):
    import asyncio

    page = OverridePage()
    service.capture_size = None
    asyncio.run(service._apply_capture_size(page))

    assert page.sent == []


def test_capture_override_sends_the_configured_size(service):
    import asyncio

    page = OverridePage()
    service.capture_size = (1600, 900)
    asyncio.run(service._apply_capture_size(page))

    ((method, params),) = page.sent
    assert method == "Emulation.setDeviceMetricsOverride"
    assert (params["width"], params["height"]) == (1600, 900)


def test_capture_override_is_sent_once_per_tab_and_again_on_a_size_change(service):
    import asyncio

    page = OverridePage()

    async def frames():
        for _ in range(5):
            await service._apply_capture_size(page)
        service.capture_size = (1280, 720)
        await service._apply_capture_size(page)

    asyncio.run(frames())

    overrides = [
        params for method, params in page.sent if method == "Emulation.setDeviceMetricsOverride"
    ]
    assert [(item["width"], item["height"]) for item in overrides] == [(1920, 1080), (1280, 720)]
    assert ("Emulation.clearDeviceMetricsOverride", {}) in page.sent


@pytest.mark.parametrize(
    ("text", "state"),
    [
        ("Вебинар завершён\nСпасибо", "ended"),
        ("Мероприятие окончено", "ended"),
        ("Соединение потеряно", "lost"),
        ("Идёт лекция\nСлайд 3", None),
        # Chat messages: a question, or a long typed sentence, is not a banner.
        ("Иванов Иван: вебинар завершён?", None),
        ("Петров: у меня соединение прервано, ребята, переподключитесь пожалуйста кто может", None),
        ("встреча завершена?", None),
    ],
)
def test_room_state_ignores_what_students_type(text, state):
    assert BrowserService.room_state_from_text(text) == state


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("Подключиться", True),
        ("Войти в вебинар", True),
        ("Join", True),
        ("Подключить микрофон", False),
        ("Подключить камеру", False),
        ("Подключиться к звуку через браузер", False),
        ("", False),
    ],
)
def test_only_room_entry_controls_are_pressed(label, expected):
    assert BrowserService.is_entry_label(label) is expected


class ChatPage:
    """An MTS Link room reduced to what the chat fallback looks at."""

    url = LECTURE

    def __init__(self, *, chat_open: bool):
        self.chat_open = chat_open
        self.messages: list[str] = []
        self.clicks: list[int] = []
        self.typed = ""

    async def elements(self, selector):
        from mirea_lecture_assistant import browser_service

        blank = {"text": "", "value": "", "placeholder": "", "label": ""}
        if selector == browser_service.CHAT_EDITORS:
            return [
                {**blank, "index": 0, "visible": True, "placeholder": "Поиск"},
                {
                    **blank,
                    "index": 1,
                    "visible": self.chat_open,
                    "placeholder": "Введите сообщение",
                },
            ]
        if selector == browser_service.CHAT_BUTTONS:
            return [
                {**blank, "index": 0, "visible": True, "text": "Скрыть чат-бот"},
                {**blank, "index": 1, "visible": True, "label": "Чат"},
            ]
        return []

    async def click(self, _selector, *, index=None):
        self.clicks.append(index)
        self.chat_open = not self.chat_open

    async def fill(self, _selector, value, *, index=None):
        assert index == 1
        self.typed = value

    async def press_enter(self):
        self.messages.append(self.typed)

    async def count_text(self, text):
        return self.messages.count(text)

    async def wait_for_timeout(self, _ms):
        return None


def _send_chat(service, page):
    import asyncio

    async def active_page():
        return page

    service._active_page = active_page
    asyncio.run(service._send_chat_message_async("Иванов Иван ИКБО-01-24"))


def test_an_already_open_chat_is_not_toggled_closed(service):
    page = ChatPage(chat_open=True)
    _send_chat(service, page)

    assert page.clicks == []
    assert page.messages == ["Иванов Иван ИКБО-01-24"]


def test_a_closed_chat_is_opened_with_the_button_named_exactly_chat(service):
    page = ChatPage(chat_open=False)
    _send_chat(service, page)

    assert page.clicks == [1]  # not the «чат-бот» toggle that comes first
    assert page.messages == ["Иванов Иван ИКБО-01-24"]


def test_concurrent_reconnects_open_a_single_connection(service, monkeypatch):
    import asyncio

    from mirea_lecture_assistant import browser_service

    opened = []

    class Browser:
        def is_connected(self):
            return True

        @staticmethod
        async def connect(_port):
            opened.append(1)
            await asyncio.sleep(0.01)
            return Browser()

    monkeypatch.setattr(browser_service, "Browser", Browser)
    monkeypatch.setattr(type(service), "is_running", property(lambda _self: True))
    service.port = 9222

    async def many():
        return await asyncio.gather(*(service._connected_browser() for _ in range(4)))

    browsers = asyncio.run(many())

    assert len(opened) == 1
    assert len({id(browser) for browser in browsers}) == 1


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


class FakeConnection:
    def __init__(self, connected: bool = True):
        self.connected = connected
        self.disconnected = False

    def is_connected(self):
        return self.connected

    async def disconnect(self):
        self.disconnected = True


def test_the_connection_is_reused_between_calls(service):
    """Connecting anew for every frame cost more than the capture and timed it out."""
    import asyncio

    connection = FakeConnection()
    service._browser = connection

    assert asyncio.run(service._connected_browser()) is connection
    assert not connection.disconnected


def test_a_broken_connection_is_dropped_before_reconnecting(service, monkeypatch):
    import asyncio

    broken = FakeConnection(connected=False)
    service._browser = broken
    monkeypatch.setattr(type(service), "is_running", property(lambda _self: False))

    with pytest.raises(RuntimeError, match="не запущен"):
        asyncio.run(service._connected_browser())

    assert broken.disconnected
    assert service._browser is None


def test_releasing_the_connection_leaves_the_browser_running(service):
    """Shutdown must free the connection without closing the lecture or the СДО session."""
    import asyncio

    connection = FakeConnection()
    service._browser = connection

    asyncio.run(service._release_connection())

    assert connection.disconnected
    assert service._browser is None


def test_releasing_twice_is_harmless(service):
    import asyncio

    asyncio.run(service._release_connection())
    asyncio.run(service._release_connection())


def test_an_adopted_browser_is_not_killed_over_an_unknown_sound_mode(service, monkeypatch):
    """Restarting it left the profile locked and every tab uncontrollable."""
    monkeypatch.setattr(type(service), "is_running", property(lambda _self: True))
    closed = []
    monkeypatch.setattr(type(service), "close", lambda _self: closed.append(True))
    monkeypatch.setattr(type(service), "_navigate", lambda _self, _url, **_kw: None)
    monkeypatch.setattr(type(service), "_find_browser", lambda _self: ("Google Chrome", "chrome"))
    monkeypatch.setattr(service, "_wait_lecture_loaded_async", _loaded)

    service.muted = None  # adopted from a previous run
    service.open("https://my.mts-link.ru/j/1/2", muted=True)

    assert closed == []


def test_a_known_different_sound_mode_still_restarts_the_browser(service, monkeypatch):
    monkeypatch.setattr(type(service), "is_running", property(lambda _self: True))
    closed = []
    monkeypatch.setattr(type(service), "close", lambda _self: closed.append(True))
    monkeypatch.setattr(type(service), "_navigate", lambda _self, _url, **_kw: None)
    monkeypatch.setattr(type(service), "_find_browser", lambda _self: ("Google Chrome", "chrome"))
    monkeypatch.setattr(service, "_wait_lecture_loaded_async", _loaded)

    service.muted = False
    service.open("https://my.mts-link.ru/j/1/2", muted=True)

    assert closed == [True]


def test_a_failed_launch_does_not_overwrite_a_working_port(service, monkeypatch):
    """A dead port in the file made the next run unable to find the live browser."""
    (service.profile_dir / service.PORT_FILE).write_text("62167", encoding="utf-8")
    monkeypatch.setattr(type(service), "is_running", property(lambda _self: False))
    monkeypatch.setattr(type(service), "_find_browser", lambda _self: ("Google Chrome", "chrome"))
    monkeypatch.setattr(type(service), "_free_port", staticmethod(lambda: 49335))
    monkeypatch.setattr(type(service), "_cdp_available", staticmethod(lambda _port: False))
    monkeypatch.setattr(type(service), "LAUNCH_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(browser_service.subprocess, "Popen", lambda *_a, **_kw: None)

    with pytest.raises(RuntimeError, match="недоступно"):
        service.open("https://my.mts-link.ru/j/1/2")

    assert (service.profile_dir / service.PORT_FILE).read_text(encoding="utf-8") == "62167"
    assert service.port is None


def test_a_successful_launch_records_its_port(service, monkeypatch):
    monkeypatch.setattr(type(service), "_find_browser", lambda _self: ("Google Chrome", "chrome"))
    monkeypatch.setattr(type(service), "_free_port", staticmethod(lambda: 49335))
    monkeypatch.setattr(type(service), "_cdp_available", staticmethod(lambda port: port == 49335))
    monkeypatch.setattr(browser_service.subprocess, "Popen", lambda *_a, **_kw: None)
    monkeypatch.setattr(service, "_wait_lecture_loaded_async", _loaded)

    service.open("https://my.mts-link.ru/j/1/2")

    assert (service.profile_dir / service.PORT_FILE).read_text(encoding="utf-8") == "49335"


def test_a_live_browser_slow_to_answer_is_not_taken_for_dead(tmp_path):
    """A busy Chrome answering in 0.6 s read as "not running" and a second browser
    was started on its profile; on CI the integration tests failed the same way."""
    import http.server
    import json
    import threading
    import time

    class Slow(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            time.sleep(0.6)
            port = self.server.server_address[1]
            body = json.dumps(
                {"webSocketDebuggerUrl": f"ws://127.0.0.1:{port}/devtools/browser/run"}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            return None

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Slow)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        service = BrowserService(tmp_path / "profile")
        service.port = server.server_address[1]

        assert service.is_running
    finally:
        server.shutdown()


def test_a_busy_browser_on_the_profile_is_never_launched_again(service, monkeypatch):
    """Each launch on a busy profile added an about:blank tab to that browser."""
    launched = []
    monkeypatch.setattr(type(service), "is_running", property(lambda _self: False))
    monkeypatch.setattr(type(service), "profile_in_use", lambda _self: True)
    monkeypatch.setattr(type(service), "_await_running_browser", lambda _self: False)
    monkeypatch.setattr(type(service), "_find_browser", lambda _self: ("Google Chrome", "chrome"))
    monkeypatch.setattr(browser_service.subprocess, "Popen", lambda *a, **kw: launched.append(a))

    for _ in range(5):
        with pytest.raises(RuntimeError, match="не отвечает"):
            service.ensure_running()

    assert launched == []


def test_a_launch_that_gave_no_control_is_not_repeated_at_once(service, monkeypatch):
    launched = []

    class HandedOver:
        def poll(self):
            return 0  # passed its tab to a browser already running, then quit

    monkeypatch.setattr(type(service), "is_running", property(lambda _self: False))
    monkeypatch.setattr(type(service), "profile_in_use", lambda _self: False)
    monkeypatch.setattr(type(service), "_find_browser", lambda _self: ("Google Chrome", "chrome"))
    monkeypatch.setattr(type(service), "_cdp_available", staticmethod(lambda _port: False))
    monkeypatch.setattr(type(service), "LAUNCH_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(
        browser_service.subprocess, "Popen", lambda *a, **kw: launched.append(a) or HandedOver()
    )

    with pytest.raises(RuntimeError, match="недоступно"):
        service.ensure_running()
    for _ in range(5):
        with pytest.raises(RuntimeError, match="повторим позже"):
            service.ensure_running()

    assert len(launched) == 1


def test_background_work_starts_the_browser_minimized_and_inactive(service, monkeypatch):
    options = []
    monkeypatch.setattr(type(service), "is_running", property(lambda _self: False))
    monkeypatch.setattr(type(service), "profile_in_use", lambda _self: False)
    monkeypatch.setattr(type(service), "_find_browser", lambda _self: ("Google Chrome", "chrome"))
    monkeypatch.setattr(type(service), "_free_port", staticmethod(lambda: 49335))
    monkeypatch.setattr(type(service), "_cdp_available", staticmethod(lambda port: port == 49335))
    monkeypatch.setattr(browser_service, "_launch_options", lambda background: {"bg": background})
    monkeypatch.setattr(browser_service.subprocess, "Popen", lambda *_a, **kw: options.append(kw))

    service.ensure_running()

    assert options[0]["bg"] is True


@pytest.mark.skipif(sys.platform != "win32", reason="the Windows show state")
def test_the_background_show_state_is_minimized_without_activation():
    info = browser_service._launch_options(True)["startupinfo"]
    assert info.wShowWindow == 7  # SW_SHOWMINNOACTIVE
    assert browser_service._launch_options(False) == {}


def test_a_new_lecture_shows_a_window_minimized_by_background_work(service):
    sent = []

    class Page:
        target_id = "lecture"
        url = LECTURE

        def is_closed(self):
            return False

    class Browser:
        pages: ClassVar[list] = [Page()]

        async def send(self, method, params):
            sent.append((method, params))
            if method == "Browser.getWindowForTarget":
                return {"windowId": 7, "bounds": {"windowState": state}}
            return {}

    async def connected():
        return Browser()

    service.lecture_url = LECTURE
    service._connected_browser = connected
    state = "minimized"
    run_async(service._show_window_async())
    assert sent[-1] == (
        "Browser.setWindowBounds",
        {"windowId": 7, "bounds": {"windowState": "normal"}},
    )

    sent.clear()
    state = "normal"  # left as the student put it
    run_async(service._show_window_async())
    assert [method for method, _ in sent] == ["Browser.getWindowForTarget"]
