from __future__ import annotations

import asyncio
import base64
import json
import time
import urllib.error
import urllib.request

import pytest

from mirea_lecture_assistant.browser_service import BrowserService
from mirea_lecture_assistant.code_bridge import CodeBridge

WATCH = "a" * 64
ORIGIN = "chrome-extension://" + "b" * 32


def test_delivery_ack_is_bound_to_the_login_tab():
    delivered = []
    bridge = CodeBridge(lambda: delivered.append(True))
    bridge.publish("123456")
    response = bridge.request("/poll", WATCH, origin=ORIGIN)
    assert response["code"] == "123456"
    bridge.request("/ack", "c" * 64, response["receipt"], ORIGIN)
    assert bridge.pending is not None and not delivered
    bridge.request("/ack", WATCH, response["receipt"], ORIGIN)
    assert bridge.pending is None and delivered == [True]
    bridge.publish("654321")
    bridge.stop()
    assert bridge.pending is None


def test_two_login_tabs_never_receive_one_code(monkeypatch):
    bridge = CodeBridge()
    bridge.watchers[ORIGIN + ":" + "c" * 64] = time.monotonic()
    bridge.publish("123456")
    now = time.monotonic()
    ticks = iter([now, now, now + 21])
    monkeypatch.setattr(time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(bridge.condition, "wait", lambda *_: None)
    assert bridge.request("/poll", WATCH, origin=ORIGIN) == {}
    assert bridge.pending[3] is None


def test_expired_code_is_not_delivered(monkeypatch):
    bridge = CodeBridge()
    bridge.publish("123456")
    now = bridge.pending[2] + 1
    ticks = iter([now, now, now + 21])
    monkeypatch.setattr(time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(bridge.condition, "wait", lambda *_: None)
    assert bridge.request("/poll", WATCH, origin=ORIGIN) == {}
    assert bridge.pending is None


def test_loopback_requires_pairing_key_and_extension_origin(caplog):
    bridge = CodeBridge()
    bridge.start()
    try:
        bridge.publish("123456")
        url = f"http://127.0.0.1:{bridge.port}/poll"

        def post(origin, token):
            request = urllib.request.Request(
                url,
                json.dumps({"watch": WATCH}).encode(),
                headers={
                    "Origin": origin,
                    "Authorization": "Bearer " + token,
                    "Content-Type": "application/json",
                },
            )
            return urllib.request.urlopen(request, timeout=2)

        for origin, token in [(ORIGIN, "wrong"), ("https://sso.mirea.ru", bridge.token)]:
            with pytest.raises(urllib.error.HTTPError) as exc:
                post(origin, token)
            assert exc.value.code == 403
        with post(ORIGIN, bridge.token) as response:
            assert json.load(response)["code"] == "123456"
        assert "123456" not in caplog.text and bridge.token not in caplog.text
    finally:
        bridge.stop()


def test_current_video_frame_bypasses_compositor(tmp_path, monkeypatch):
    service = BrowserService(tmp_path)

    class Page:
        async def evaluate(self, js, *args, **kwargs):
            return base64.b64encode(b"fresh frame").decode() if "toDataURL" in js else "chat"

        async def screenshot(self, **kwargs):
            raise AssertionError("Compositor must not be used for a decoded video")

    async def active():
        return Page()

    async def size(_page):
        pass

    monkeypatch.setattr(service, "_active_page", active)
    monkeypatch.setattr(service, "_apply_capture_size", size)
    assert asyncio.run(service.capture_page_state()) == (b"fresh frame", "chat")


def test_only_dedicated_profile_password_preferences_change(tmp_path):
    service = BrowserService(tmp_path / "dedicated")
    path = service.profile_dir / "Default" / "Preferences"
    path.parent.mkdir()
    path.write_text('{"profile":{"unrelated":42},"safebrowsing":{"enabled":true}}')
    service._prepare_password_preferences()
    prefs = json.loads(path.read_text())
    assert prefs["profile"]["unrelated"] == 42
    assert prefs["safebrowsing"]["enabled"] is True
    assert prefs["profile"]["password_manager_leak_detection"] is False


def _post(bridge, path, origin=ORIGIN, user_agent="Mozilla/5.0 Chrome/141.0 YaBrowser/25.8"):
    request = urllib.request.Request(
        f"http://127.0.0.1:{bridge.port}{path}",
        data=json.dumps({"watch": "0" * 64}).encode(),
        headers={
            "Origin": origin,
            "Authorization": "Bearer " + bridge.token,
            "Content-Type": "application/json",
            "User-Agent": user_agent,
        },
        method="POST",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return opener.open(request, timeout=5)


def test_the_extension_says_hello_and_the_app_knows_its_browser():
    bridge = CodeBridge()
    bridge.start()
    try:
        assert bridge.extension_seen is None
        _post(bridge, "/hello").read()
        assert bridge.extension_seen is not None
        assert bridge.extension_browser == "Яндекс Браузер"
    finally:
        bridge.stop()


def test_a_web_page_cannot_pretend_to_be_the_extension():
    bridge = CodeBridge()
    bridge.start()
    try:
        with pytest.raises(urllib.error.HTTPError):
            _post(bridge, "/hello", origin="https://evil.example")
        assert bridge.extension_seen is None
    finally:
        bridge.stop()


@pytest.mark.parametrize(
    ("agent", "name"),
    [
        ("Mozilla/5.0 Chrome/141.0.0.0 Safari/537.36", "Google Chrome"),
        ("Mozilla/5.0 Chrome/141.0.0.0 Safari/537.36 Edg/141.0", "Microsoft Edge"),
        ("Mozilla/5.0 Chrome/141.0.0.0 Safari/537.36 OPR/123.0", "Opera"),
    ],
)
def test_browser_names(agent, name):
    from mirea_lecture_assistant.code_bridge import browser_name

    assert browser_name(agent) == name


def test_the_folder_stamp_changes_with_the_code_not_with_the_pairing_key(tmp_path):
    from mirea_lecture_assistant.code_bridge import extension_stamp
    from mirea_lecture_assistant.paths import resource_path

    source = tmp_path / "source"
    import shutil

    shutil.copytree(resource_path("browser_extension"), source)
    target = tmp_path / "target"
    bridge = CodeBridge()
    bridge.prepare_extension(source, target)
    stamped = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
    assert stamped["version_name"].endswith(extension_stamp(source))
    before = extension_stamp(source)
    (source / "bridge-config.json").write_text('{"port": 5, "token": "x"}')
    assert extension_stamp(source) == before
    (source / "email-code.js").write_text("// newer code")
    assert extension_stamp(source) != before
