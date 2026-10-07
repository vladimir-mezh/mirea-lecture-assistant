"""The lecture flow against a real Chrome, through the DevTools client.

A local page stands in for an MTS Link room (Chrome resolves mts-link.ru to the
test server): a lobby with the entry and microphone buttons, a QR on screen and
a chat that is closed until its button is pressed. Skipped without Chrome.
"""

from __future__ import annotations

import glob
import http.server
import io
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import ClassVar

import pytest
import qrcode

from mirea_lecture_assistant.async_runtime import run_async
from mirea_lecture_assistant.browser_service import BrowserService
from mirea_lecture_assistant.cdp import endpoint_alive
from mirea_lecture_assistant.qr import ScreenScanner

QR_PAYLOAD = "https://pulse.mirea.ru/selfapprove?token=123e4567-e89b-12d3-a456-426614174000"

ROOM = """<!doctype html><html><head><meta charset="utf-8"><title>Лекция</title>
<style>
  body { font-family: sans-serif; margin: 0; }
  #room, #chatEditor { display: none; }
  #qr { width: 360px; height: 360px; image-rendering: pixelated; }
</style></head><body>
<div id="lobby">
  <input type="text" id="name" placeholder="Ваше имя">
  <button onclick="window.micPressed = true">Подключить микрофон</button>
  <button id="join" onclick="enter()">Подключиться</button>
</div>
<div id="room">
  <h1 id="who"></h1>
  <img id="qr" src="/qr.png">
  <button aria-label="Чат" onclick="toggleChat()">💬</button>
  <div class="chat-panel">
    <div id="messages">
      <div>Петров: переподключитесь, у кого звук пропал</div>
      <div>Сидорова: опрос тут <a href="https://forms.example/join" target="_blank">join</a></div>
    </div>
    <textarea id="chatEditor" placeholder="Введите сообщение"
      onkeydown="send(event, this)"></textarea>
  </div>
</div>
<div id="ended" style="display:none">Вебинар завершён</div>
<script>
  function enter() {
    // Like the real platform: leaving a joined room asks for confirmation.
    window.onbeforeunload = event => { event.preventDefault(); event.returnValue = ''; };
    document.getElementById('who').textContent = document.getElementById('name').value;
    document.getElementById('lobby').style.display = 'none';
    document.getElementById('room').style.display = 'block';
  }
  function toggleChat() {
    const editor = document.getElementById('chatEditor');
    editor.style.display = editor.style.display === 'block' ? 'none' : 'block';
  }
  function send(event, editor) {
    if (event.key !== 'Enter') return;
    event.preventDefault();
    const line = document.createElement('div');
    line.textContent = editor.value;
    document.getElementById('messages').appendChild(line);
    editor.value = '';
  }
  if (location.search.includes('ended')) {
    document.getElementById('lobby').style.display = 'none';
    document.getElementById('ended').style.display = 'block';
  }
</script></body></html>"""

WEBINARS = """<!doctype html><html><head><meta charset="utf-8"></head><body>
<div id="wb2-table"><table class="data"><tbody></tbody></table></div>
<script>
  setTimeout(() => {
    document.querySelector('tbody').innerHTML = '<tr><td>Физика</td></tr>';
  }, 300);
</script></body></html>"""


# MIREA's offer to confirm sign-ins through МАКС: a code form and a skip form.
MAX_PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>Вход в МИРЭА</title></head><body>
<h1>Подтверждение через МАКС</h1>
<form id="kc-max-otp-form" method="post"
      action="/realms/mirea/login-actions/required-action?execution=max-account-config">
  <input name="code" autofocus><button type="submit">Подтвердить</button>
</form>
<form id="kc-max-otp-skip-form" method="post"
      action="/realms/mirea/login-actions/required-action?execution=max-account-config">
  <input type="hidden" name="skip" value="true">
  <input type="submit" value="Пропустить">
</form></body></html>"""

# Another required action: nothing on it may be pressed.
PASSWORD_PAGE = """<!doctype html><html><head><meta charset="utf-8"></head><body>
<form method="post" action="/realms/mirea/login-actions/required-action?execution=UPDATE_PASSWORD">
  <input name="password-new"><button type="submit">Пропустить</button>
</form></body></html>"""


def _chrome() -> str | None:
    candidates = [
        os.environ.get("CHROME_PATH", ""),
        *sorted(glob.glob("/opt/pw-browsers/chromium-*/chrome-linux/chrome")),
        shutil.which("google-chrome") or "",
        shutil.which("chromium") or "",
        shutil.which("chromium-browser") or "",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    ]
    return next((path for path in candidates if path and Path(path).is_file()), None)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Site(http.server.BaseHTTPRequestHandler):
    qr_png = b""
    posted: ClassVar[list[tuple[str, bytes]]] = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        Site.posted.append((self.path, self.rfile.read(length)))
        body = b"<!doctype html><title>done</title>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if "execution=max-account-config" in self.path:
            body, kind = MAX_PAGE.encode(), "text/html; charset=utf-8"
        elif "execution=UPDATE_PASSWORD" in self.path:
            body, kind = PASSWORD_PAGE.encode(), "text/html; charset=utf-8"
        elif self.path.startswith("/event/"):
            body, kind = ROOM.encode(), "text/html; charset=utf-8"
        elif self.path == "/qr.png":
            body, kind = self.qr_png, "image/png"
        elif self.path.startswith("/framed/"):
            # The whole room inside a frame of the same site.
            body = (
                b'<!doctype html><meta charset="utf-8"><body style="margin:0">'
                b'<iframe src="/event/888" style="border:0;width:100vw;height:100vh"></iframe>'
            )
            kind = "text/html; charset=utf-8"
        elif self.path.startswith("/redirect/"):
            # The page replaces itself while it is still being parsed.
            body = b"<!doctype html><script>location.replace('/event/777')</script>"
            kind = "text/html; charset=utf-8"
        elif self.path.startswith("/mod/webinars/"):
            body, kind = WEBINARS.encode(), "text/html; charset=utf-8"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        return None


@pytest.fixture(scope="module")
def room(tmp_path_factory):
    chrome = _chrome()
    if chrome is None:
        pytest.skip("Chrome is not installed")
    image = io.BytesIO()
    qrcode.make(QR_PAYLOAD).save(image, format="PNG")
    Site.qr_png = image.getvalue()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Site)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = _free_port()
    profile = tmp_path_factory.mktemp("profile")
    args = [
        chrome,
        "--headless=new",
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-proxy-server",
        (
            "--host-resolver-rules=MAP mts-link.ru 127.0.0.1, MAP online-edu.mirea.ru 127.0.0.1,"
            " MAP sso.mirea.ru 127.0.0.1"
        ),
        "about:blank",
    ]
    if sys.platform.startswith("linux"):
        args.insert(1, "--no-sandbox")
    process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(150):
        if endpoint_alive(port):
            break
        time.sleep(0.1)
    else:
        process.kill()
        pytest.skip("Chrome did not open its debugging port")
    service = BrowserService(profile / "service")
    service.port = port
    service._find_browser = lambda: ("Chromium", chrome)
    yield service, server.server_address[1]
    try:
        service.close()
    finally:
        if process.poll() is None:
            process.kill()
        server.shutdown()


def test_the_whole_lecture_flow_runs_on_a_real_browser(room):
    service, http_port = room
    lecture = f"http://mts-link.ru:{http_port}/event/12345"

    service.open(lecture)
    assert service.lecture_state() == "waiting"

    assert service.join_lecture("Иванов Иван") == "joined"
    page = run_async(service._active_page())
    assert run_async(page.inner_text("#who")) == "Иванов Иван"
    # The microphone button also says "подключ…", but only the entry was pressed.
    assert run_async(page.evaluate("() => Boolean(window.micPressed)")) is False

    # A classmate asking to reconnect in the chat is not a lost connection.
    assert service.lecture_state() == "live"

    png, text = run_async(service.capture_page_state())
    assert ScreenScanner().decode_png(png).decoded == (QR_PAYLOAD,)
    assert "переподключитесь" in text

    service.send_chat_message("Иванов Иван ИКБО-01-24")
    assert run_async(page.count_text("Иванов Иван ИКБО-01-24")) == 1

    run_async(page.goto(lecture + "?ended=1"))
    assert service.lecture_state() == "ended"


def test_a_page_filled_by_script_is_read_after_its_rows_appear(room):
    service, http_port = room
    html = service.read_html(f"http://online-edu.mirea.ru:{http_port}/mod/webinars/view.php?id=1")
    assert "<td>Физика</td>" in html


def test_a_leave_confirmation_or_an_alert_never_freezes_the_lecture_tab(room):
    """An unanswered "leave the webinar?" used to time out every capture after a reload."""
    service, http_port = room
    lecture = f"http://mts-link.ru:{http_port}/event/555"
    service.open(lecture)
    assert service.join_lecture("Иванов Иван") == "joined"  # arms onbeforeunload
    page = run_async(service._active_page())

    started = time.monotonic()
    service.open(lecture, force_navigation=True)  # the reload recovery
    run_async(page.evaluate("() => setTimeout(() => alert('Внимание'), 0)"))
    time.sleep(0.3)
    png, _text = run_async(service.capture_page_state())

    assert png.startswith(b"\x89PNG")
    assert time.monotonic() - started < 10
    assert service.lecture_state() in ("waiting", "live")


def test_a_script_redirect_while_loading_still_finishes_the_navigation(room):
    service, http_port = room

    async def visit(page):
        await page.goto(f"http://mts-link.ru:{http_port}/redirect/1", timeout=8_000)
        return page.url

    assert service.run_on_new_page(visit, timeout=15).endswith("/event/777")


def test_opening_a_lecture_never_takes_a_helper_tab(room):
    service, http_port = room
    browser = run_async(service._connected_browser())
    helper = run_async(service._helper_page(browser))
    run_async(helper.goto(f"http://online-edu.mirea.ru:{http_port}/mod/webinars/view.php?id=2"))
    helper_url = helper.url
    service.lecture_url = None
    service._lecture_target = None

    service.open(f"http://mts-link.ru:{http_port}/event/999")

    assert helper.url == helper_url
    service._helper_targets.discard(helper.target_id)
    run_async(helper.close())


def test_a_room_rendered_inside_a_frame_is_joined_and_chatted_in(room):
    service, http_port = room
    service.lecture_url = None
    service._lecture_target = None
    service.open(f"http://mts-link.ru:{http_port}/framed/1")
    time.sleep(0.5)  # let the frame load

    assert service.lecture_state() == "waiting"
    assert service.join_lecture("Петров Пётр") == "joined"
    assert service.lecture_state() == "live"
    service.send_chat_message("Петров Пётр ИКБО-01-24")
    page = run_async(service._active_page())
    assert run_async(page.count_text("Петров Пётр ИКБО-01-24")) == 1


def test_pinned_blank_tab_is_replaced_by_the_real_lecture_tab(room):
    service, _http_port = room
    real = run_async(service._active_page())
    browser = run_async(service._connected_browser())
    blank = run_async(browser.new_page(background=True))
    try:
        service._lecture_target = blank.target_id
        assert run_async(service._active_page()).target_id == real.target_id
        assert service._lecture_target == real.target_id
    finally:
        run_async(blank.close())


def test_empty_http_document_is_not_a_live_or_capturable_lecture(room):
    service, _http_port = room
    page = run_async(service._active_page())
    run_async(page.evaluate("() => document.body.replaceChildren()"))
    assert service.lecture_state() == "unstable"
    with pytest.raises(RuntimeError, match="ещё не загрузилась"):
        run_async(service.capture_page_state())


EXTENSION_SCRIPT = Path(__file__).resolve().parents[1] / "browser_extension" / "skip-max.js"


def _run_extension_on(service, url: str) -> None:
    browser = run_async(service._connected_browser())
    page = run_async(browser.new_page(background=True))
    try:
        run_async(page.goto(url, wait_until="domcontentloaded"))
        script = EXTENSION_SCRIPT.read_text(encoding="utf-8")
        run_async(page.evaluate("() => {" + script + "}"))
        time.sleep(1.5)
    finally:
        run_async(page.close())


def test_the_extension_presses_skip_on_the_max_offer(room):
    service, http_port = room
    Site.posted.clear()
    url = (
        f"http://sso.mirea.ru:{http_port}/realms/mirea/login-actions/required-action"
        "?execution=max-account-config"
    )

    _run_extension_on(service, url)

    # Exactly the skip form went out, not the code form.
    assert [body for _path, body in Site.posted] == [b"skip=true"]


def test_the_extension_presses_nothing_on_another_required_action(room):
    service, http_port = room
    Site.posted.clear()
    url = (
        f"http://sso.mirea.ru:{http_port}/realms/mirea/login-actions/required-action"
        "?execution=UPDATE_PASSWORD"
    )

    _run_extension_on(service, url)

    assert Site.posted == []


def test_a_running_browser_holds_its_profile(room, tmp_path):
    service, _http_port = room
    chrome_profile = service.profile_dir.parent  # the fixture's --user-data-dir
    assert BrowserService(chrome_profile).profile_in_use()
    assert not BrowserService(tmp_path / "unused").profile_in_use()


def test_an_unresponsive_browser_on_the_profile_gets_no_second_launch(room, monkeypatch):
    """The real case behind a window full of about:blank tabs."""
    service, _http_port = room
    other = BrowserService(service.profile_dir.parent)
    other._find_browser = service._find_browser
    launched = []
    monkeypatch.setattr(type(other), "_cdp_available", staticmethod(lambda _port: False))
    monkeypatch.setattr(type(other), "_await_running_browser", lambda _self: False)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: launched.append(a))

    with pytest.raises(RuntimeError, match="не отвечает"):
        other.ensure_running()

    assert launched == []


def test_live_video_capture_reads_a_fresh_frame_without_screenshot(room, monkeypatch):
    from mirea_lecture_assistant.cdp import Page

    service, http_port = room
    service.open(f"http://mts-link.ru:{http_port}/event/12345")
    page = run_async(service._active_page())
    run_async(
        page.evaluate("""async () => {
      const img = document.querySelector('#qr'); await img.decode();
      const c = document.createElement('canvas'); c.width = c.height = 360;
      c.getContext('2d').drawImage(img, 0, 0, 360, 360);
      const v = document.createElement('video'); v.muted = true;
      v.style = 'width:360px;height:360px'; v.srcObject = c.captureStream(10);
      document.body.append(v); await v.play(); window.fixtureCanvas = c;
    }""")
    )
    time.sleep(0.3)

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("Video capture must bypass the compositor")

    monkeypatch.setattr(Page, "screenshot", forbidden)
    png, _ = run_async(service.capture_page_state())
    assert service._capture_backend == "video"
    assert ScreenScanner().decode_png(png).decoded == (QR_PAYLOAD,)
    run_async(
        page.evaluate("""() => {
      const c = window.fixtureCanvas, ctx = c.getContext('2d');
      ctx.fillStyle = 'white'; ctx.fillRect(0, 0, c.width, c.height);
    }""")
    )
    time.sleep(0.3)
    png, _ = run_async(service.capture_page_state())
    assert ScreenScanner().decode_png(png).decoded == ()


def test_the_finished_lecture_tab_closes_the_empty_browser(room):
    # Last test: this fixture shares one isolated browser for this module.
    service, http_port = room
    service.open(f"http://mts-link.ru:{http_port}/event/12345")
    browser = run_async(service._connected_browser())
    lecture = run_async(service._active_page())
    for page in list(browser.pages):
        if page is not lecture:
            run_async(page.close())
    assert service.close_lecture_tab() is True
    assert not service.is_running
