"""A small Chrome DevTools Protocol client: exactly what the app asks of Chrome.

Playwright drove Chrome through a bundled Node.js driver that alone was about
40 MB of the executable. The app only attaches to a Chrome it started itself,
navigates, reads and clicks a handful of elements and takes screenshots; the
DevTools protocol offers all of that over one WebSocket.
"""

from __future__ import annotations

import asyncio
import base64
import itertools
import json
import logging
import urllib.request
from collections import deque
from collections.abc import Callable
from urllib.parse import urlparse

log = logging.getLogger(__name__)

# The debugging endpoint is local; a system proxy must never be asked for it.
_LOCAL = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_POLL_SECONDS = 0.1
# Setting up a tab's session is quick when the tab is healthy.
_ATTACH_SECONDS = 10.0

# Element helpers run inside the page. "Visible" follows what a person sees:
# rendered with a size, not hidden by style.
_JS_HELPERS = r"""
const visible = element => {
  if (!element || !element.isConnected) return false;
  const style = getComputedStyle(element);
  if (style.visibility === 'hidden' || style.display === 'none') return false;
  const rect = element.getBoundingClientRect();
  return rect.width > 0 && rect.height > 0;
};
const inChat = element => Boolean(element.closest(
  "[class*='chat' i], [class*='comment' i], [role='log']"));
const leavesSite = element => {
  if (element.tagName !== 'A' || !element.href) return false;
  if (element.target === '_blank') return true;
  try { return new URL(element.href, location.href).origin !== location.origin; }
  catch (error) { return true; }
};
const label = element => ((element.innerText || element.value || '').trim()).slice(0, 60);
const describe = (element, index) => ({
  index,
  visible: visible(element),
  chat: inChat(element),
  external: leavesSite(element),
  text: (element.innerText || '').trim(),
  value: 'value' in element ? String(element.value || '') : '',
  placeholder: element.getAttribute('placeholder')
    || element.getAttribute('data-placeholder')
    || element.getAttribute('aria-placeholder') || '',
  label: element.getAttribute('aria-label') || element.getAttribute('title') || '',
});
const pick = (selector, index) => {
  const all = [...document.querySelectorAll(selector)];
  if (index === null || index === undefined) return all.find(visible) || null;
  return all[index] || null;
};
"""


class CdpError(RuntimeError):
    """The browser refused a command, or the page or connection went away."""


class CdpTimeout(CdpError, TimeoutError):
    """What was waited for did not happen in time."""


def _version(port: int, timeout: float) -> dict:
    with _LOCAL.open(f"http://127.0.0.1:{port}/json/version", timeout=timeout) as response:
        info = json.loads(response.read())
    if not isinstance(info, dict):
        raise CdpError("Порт отладки отвечает не как браузер")
    return info


def debugger_url(port: int, timeout: float = 3.0) -> str:
    """The browser's WebSocket address, checked to stay on this very port.

    Any local program answering on a stale port could otherwise hand us an
    address of its own, and we would type the MIREA password into it.
    """
    url = str(_version(port, timeout).get("webSocketDebuggerUrl", ""))
    parsed = urlparse(url)
    if (
        parsed.scheme != "ws"
        or parsed.hostname not in ("127.0.0.1", "localhost")
        or parsed.port != port
        or not parsed.path.startswith("/devtools/browser/")
    ):
        raise CdpError("Порт отладки отвечает не как браузер приложения")
    return url


def browser_id(url: str) -> str:
    """The id Chrome gives this browser run, the last part of its WebSocket path."""
    return urlparse(url).path.rsplit("/", 1)[-1]


def endpoint_alive(port: int, timeout: float = 0.3, *, expected_id: str | None = None) -> bool:
    """Whether our browser answers on ``port``; with ``expected_id``, that very run."""
    try:
        url = debugger_url(port, timeout)
    except (OSError, ValueError, KeyError, CdpError):
        return False
    return expected_id is None or browser_id(url) == expected_id


class Browser:
    """One WebSocket to the browser; pages talk through flattened sessions on it."""

    def __init__(self, websocket):
        self._ws = websocket
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        self._pages: dict[str, Page] = {}  # discovery order: the newest tab is last
        self._sessions: dict[str, Page] = {}
        self._closed = False
        self._reader = asyncio.get_running_loop().create_task(self._read())

    @classmethod
    async def connect(cls, port: int, timeout: float = 3.5) -> Browser:
        import websockets

        url = await asyncio.to_thread(debugger_url, port, timeout)
        websocket = await asyncio.wait_for(
            websockets.connect(
                url, max_size=None, proxy=None, ping_interval=None, open_timeout=timeout
            ),
            timeout,
        )
        browser = cls(websocket)
        try:
            await browser.send("Target.setDiscoverTargets", {"discover": True})
            for info in (await browser.send("Target.getTargets"))["targetInfos"]:
                browser._target_seen(info)
        except BaseException:
            await browser.disconnect()
            raise
        return browser

    def is_connected(self) -> bool:
        return not self._closed

    @property
    def pages(self) -> list[Page]:
        return [
            page
            for page in self._pages.values()
            if not page.is_closed() and not page.url.startswith("devtools://")
        ]

    async def new_page(self, url: str = "about:blank", *, background: bool = False) -> Page:
        params = {"url": url, "background": background}
        target_id = (await self.send("Target.createTarget", params))["targetId"]
        page = self._pages.get(target_id)
        if page is None:
            page = self._pages[target_id] = Page(self, target_id, url)
        return page

    async def send(
        self,
        method: str,
        params: dict | None = None,
        *,
        session_id: str | None = None,
        timeout: float = 30.0,
    ) -> dict:
        if self._closed:
            raise CdpError("Соединение с браузером закрыто")
        message_id = next(self._ids)
        future = asyncio.get_running_loop().create_future()
        self._pending[message_id] = future
        message = {"id": message_id, "method": method, "params": params or {}}
        if session_id:
            message["sessionId"] = session_id
        try:
            await self._ws.send(json.dumps(message))
            return await asyncio.wait_for(future, timeout)
        except TimeoutError as exc:
            raise CdpTimeout(f"{method}: браузер не ответил за {timeout:.0f} с") from exc
        except CdpError:
            raise
        except Exception as exc:  # the socket itself failed
            raise CdpError(f"{method}: соединение с браузером прервано") from exc
        finally:
            self._pending.pop(message_id, None)

    async def close_browser(self) -> None:
        try:
            await self.send("Browser.close", timeout=5)
        except CdpError:
            log.debug("cdp_browser_close_failed", exc_info=True)
        await self.disconnect()

    async def disconnect(self) -> None:
        self._closed = True
        try:
            await self._ws.close()
        except Exception:  # an already broken socket needs no goodbye
            log.debug("cdp_socket_close_failed", exc_info=True)
        if not self._reader.done():
            self._reader.cancel()
        self._fail_pending()

    async def _read(self) -> None:
        try:
            async for raw in self._ws:
                message = json.loads(raw)
                if "id" in message:
                    future = self._pending.get(message["id"])
                    if future is not None and not future.done():
                        if "error" in message:
                            error = message["error"].get("message", "ошибка браузера")
                            future.set_exception(CdpError(error))
                        else:
                            future.set_result(message.get("result", {}))
                else:
                    self._dispatch(message)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.debug("cdp_reader_stopped", exc_info=True)
        finally:
            self._closed = True
            self._fail_pending()

    def _fail_pending(self) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(CdpError("Соединение с браузером потеряно"))
        for page in self._pages.values():
            page._fail_waiters(CdpError("Соединение с браузером потеряно"))

    def _dispatch(self, message: dict) -> None:
        method = message.get("method", "")
        params = message.get("params", {})
        session_id = message.get("sessionId")
        if session_id:
            page = self._sessions.get(session_id)
            if page is not None:
                page._on_event(method, params)
            return
        if method in ("Target.targetCreated", "Target.targetInfoChanged"):
            self._target_seen(params["targetInfo"])
        elif method == "Target.targetDestroyed":
            page = self._pages.pop(params.get("targetId", ""), None)
            if page is not None:
                page._closed = True
                page._fail_waiters(CdpError("Вкладка закрыта"))
        elif method == "Target.detachedFromTarget":
            page = self._sessions.pop(params.get("sessionId", ""), None)
            if page is not None:
                page._session_id = None

    def _target_seen(self, info: dict) -> None:
        if info.get("type") != "page":
            return
        page = self._pages.get(info["targetId"])
        if page is None:
            page = self._pages[info["targetId"]] = Page(self, info["targetId"])
        page._url = info.get("url", page._url)


class Page:
    """One tab. The DevTools session is attached on first use and kept."""

    def __init__(self, browser: Browser, target_id: str, url: str = ""):
        self._browser = browser
        self.target_id = target_id
        self._url = url
        self._closed = False
        self._session_id: str | None = None
        self._attach_lock: asyncio.Lock | None = None
        self._frame_id: str | None = None
        self._loader_id: str | None = None
        # (sequence, loader, name): the sequence tells events after a command
        # from the history before it.
        self._lifecycle: deque[tuple[int, str, str]] = deque(maxlen=200)
        self._sequence = itertools.count(1)
        self._last_sequence = 0
        self._waiters: list[tuple[Callable[[int, str, str], bool], asyncio.Future]] = []

    @property
    def url(self) -> str:
        return self._url

    def is_closed(self) -> bool:
        return self._closed

    # --- session and events -------------------------------------------------

    async def _session(self) -> str:
        if self._session_id:
            return self._session_id
        if self._attach_lock is None:
            self._attach_lock = asyncio.Lock()
        async with self._attach_lock:
            if self._session_id:
                return self._session_id
            result = await self._browser.send(
                "Target.attachToTarget",
                {"targetId": self.target_id, "flatten": True},
                timeout=_ATTACH_SECONDS,
            )
            session_id = result["sessionId"]
            self._browser._sessions[session_id] = self

            async def call(method, params=None):
                return await self._browser.send(
                    method, params, session_id=session_id, timeout=_ATTACH_SECONDS
                )

            try:
                # A dialog already open would block even Page.enable; answering
                # when there is none just returns an error.
                try:
                    await call("Page.handleJavaScriptDialog", {"accept": True})
                except CdpError:
                    pass
                await call("Page.enable")
                await call("Page.setLifecycleEventsEnabled", {"enabled": True})
                # A minimised or covered window must still behave, and render, as focused.
                await call("Emulation.setFocusEmulationEnabled", {"enabled": True})
                frame = (await call("Page.getFrameTree"))["frameTree"]["frame"]
            except BaseException:
                # A half-set-up session (no lifecycle events, no frame) used to be
                # kept for good and every later navigation of the tab timed out.
                self._browser._sessions.pop(session_id, None)
                try:
                    await self._browser.send(
                        "Target.detachFromTarget", {"sessionId": session_id}, timeout=3
                    )
                except CdpError:
                    log.debug("cdp_detach_after_failed_attach_failed", exc_info=True)
                raise
            self._frame_id = frame["id"]
            self._loader_id = frame.get("loaderId")
            self._session_id = session_id
            return session_id

    async def send(self, method: str, params: dict | None = None, *, timeout: float = 30.0):
        if self._closed:
            raise CdpError("Вкладка закрыта")
        session_id = await self._session()
        return await self._browser.send(method, params, session_id=session_id, timeout=timeout)

    def _on_event(self, method: str, params: dict) -> None:
        if method == "Page.javascriptDialogOpening":
            # alert/confirm/beforeunload block the page until answered: an
            # unanswered "leave the webinar?" froze the tab — every screenshot
            # and script timed out. Answer at once.
            log.info("cdp_dialog_dismissed type=%s", params.get("type", ""))
            asyncio.get_running_loop().create_task(self._answer_dialog())
        elif method == "Page.lifecycleEvent" and params.get("frameId") == self._frame_id:
            loader, name = params.get("loaderId", ""), params.get("name", "")
            if name == "init":
                self._loader_id = loader
            sequence = next(self._sequence)
            self._last_sequence = sequence
            self._lifecycle.append((sequence, loader, name))
            for predicate, future in list(self._waiters):
                if not future.done() and predicate(sequence, loader, name):
                    future.set_result(loader)
        elif method == "Page.frameNavigated" and not params["frame"].get("parentId"):
            frame = params["frame"]
            self._frame_id = frame["id"]
            self._loader_id = frame.get("loaderId", self._loader_id)
            self._url = frame.get("url", self._url)
        elif method == "Page.navigatedWithinDocument" and params.get("frameId") == self._frame_id:
            self._url = params.get("url", self._url)

    async def _answer_dialog(self) -> None:
        try:
            await self.send("Page.handleJavaScriptDialog", {"accept": True}, timeout=5)
        except CdpError:
            log.debug("cdp_dialog_answer_failed", exc_info=True)

    def _fail_waiters(self, error: Exception) -> None:
        for _predicate, future in self._waiters:
            if not future.done():
                future.set_exception(error)

    async def _wait_lifecycle(self, predicate, timeout_ms: float, what: str) -> None:
        if any(predicate(sequence, loader, name) for sequence, loader, name in self._lifecycle):
            return
        future = asyncio.get_running_loop().create_future()
        entry = (predicate, future)
        self._waiters.append(entry)
        try:
            await asyncio.wait_for(future, timeout_ms / 1000)
        except TimeoutError as exc:
            raise CdpTimeout(
                f"Страница не дождалась «{what}» за {timeout_ms / 1000:.0f} с"
            ) from exc
        finally:
            self._waiters.remove(entry)

    # --- navigation ---------------------------------------------------------

    async def goto(self, url: str, *, wait_until: str = "load", timeout: float = 30_000) -> None:
        event = {"load": "load", "domcontentloaded": "DOMContentLoaded"}[wait_until]
        await self._session()
        start = self._last_sequence
        result = await self.send("Page.navigate", {"url": url}, timeout=timeout / 1000)
        if result.get("errorText"):
            raise CdpError(f"Страница не открылась: {result['errorText']}")
        loader = result.get("loaderId")
        if not loader:
            return  # a jump within the same document
        await self._wait_lifecycle(self._loaded_since(start, event, loader), timeout, event)

    def _loaded_since(self, start: int, event: str, loader: str | None = None):
        """Match ``event`` of the navigated document or of any document after it.

        A script redirect while the page parses replaces the navigated document
        before it fires DOMContentLoaded; its successor loads instead.
        """

        def predicate(sequence: int, seen: str, name: str) -> bool:
            if sequence <= start or name != event:
                return False
            if seen == loader:
                return True
            return any(
                later > start and started == seen and kind == "init"
                for later, started, kind in self._lifecycle
            )

        return predicate

    async def reload(self, *, timeout: float = 30_000) -> None:
        await self._session()
        start = self._last_sequence
        await self.send("Page.reload", timeout=timeout / 1000)
        # Only a document started by this reload counts; the history used to
        # satisfy the wait at once, before anything was reloaded.
        await self._wait_lifecycle(
            self._loaded_since(start, "DOMContentLoaded"), timeout, "DOMContentLoaded"
        )

    async def wait_for_network_idle(self, timeout: float = 8_000) -> None:
        await self._session()
        loader = self._loader_id
        await self._wait_lifecycle(
            lambda _sequence, seen, name: seen == loader and name == "networkIdle",
            timeout,
            "networkIdle",
        )

    async def wait_for_url(self, predicate: Callable[[str], bool], *, timeout: float) -> None:
        deadline = asyncio.get_running_loop().time() + timeout / 1000
        while not predicate(self.url):
            if asyncio.get_running_loop().time() >= deadline:
                raise CdpTimeout(f"Адрес страницы не сменился за {timeout / 1000:.0f} с")
            await asyncio.sleep(_POLL_SECONDS)

    async def close(self) -> None:
        if self._closed:
            return
        try:
            await self._browser.send("Target.closeTarget", {"targetId": self.target_id})
        finally:
            self._closed = True

    async def wait_for_timeout(self, milliseconds: float) -> None:
        await asyncio.sleep(milliseconds / 1000)

    # --- page content -------------------------------------------------------

    async def evaluate(
        self, function: str, arg=None, *, timeout: float = 30.0, user_gesture: bool = False
    ):
        """Call a JavaScript function in the page and return its JSON-able result.

        ``user_gesture`` is for clicks and typing only: a script run "by the
        user" arms the page's leave-confirmation, so reading the page must not.
        """
        call = f"({function})({json.dumps(arg, ensure_ascii=False)})"
        result = await self.send(
            "Runtime.evaluate",
            {
                "expression": call,
                "returnByValue": True,
                "awaitPromise": True,
                "userGesture": user_gesture,
            },
            timeout=timeout,
        )
        if "exceptionDetails" in result:
            details = result["exceptionDetails"]
            text = details.get("exception", {}).get("description") or details.get("text", "")
            raise CdpError(f"Ошибка скрипта на странице: {text}")
        return result.get("result", {}).get("value")

    async def content(self) -> str:
        return await self.evaluate(
            "() => document.documentElement ? document.documentElement.outerHTML : ''"
        )

    async def inner_text(self, selector: str = "body", *, timeout: float = 1_000) -> str:
        return await self.evaluate(
            "s => { const e = document.querySelector(s); return e ? e.innerText : ''; }",
            selector,
            timeout=timeout / 1000,
        )

    async def screenshot(self, *, timeout: float = 30_000) -> bytes:
        result = await self.send(
            "Page.captureScreenshot",
            {"format": "png", "fromSurface": True},
            timeout=timeout / 1000,
        )
        return base64.b64decode(result["data"])

    async def elements(self, selector: str) -> list[dict]:
        """Describe every match: index, visible, text, value, placeholder, label."""
        return await self.evaluate(
            "s => {" + _JS_HELPERS + " return [...document.querySelectorAll(s)].map(describe); }",
            selector,
        )

    async def count_text(self, text: str) -> int:
        """How many elements show exactly this text (the innermost ones only)."""
        return await self.evaluate(
            """t => [...document.body.querySelectorAll('*')].filter(e =>
                (e.innerText || '').trim() === t
                && ![...e.children].some(c => (c.innerText || '').trim() === t)).length""",
            text,
        )

    async def wait_for_selector(self, selector: str, *, timeout: float = 30_000) -> None:
        """Wait until an element matching ``selector`` is visible."""
        deadline = asyncio.get_running_loop().time() + timeout / 1000
        script = (
            "s => {" + _JS_HELPERS + " return [...document.querySelectorAll(s)].some(visible); }"
        )
        while True:
            try:
                if await self.evaluate(script, selector, timeout=5):
                    return
            except CdpError:
                if self._closed:
                    raise
                # The document is being replaced mid-navigation; look again.
            if asyncio.get_running_loop().time() >= deadline:
                raise CdpTimeout(f"На странице не появилось {selector} за {timeout / 1000:.0f} с")
            await asyncio.sleep(_POLL_SECONDS)

    # --- input --------------------------------------------------------------

    async def click(
        self, selector: str, *, index: int | None = None, expect_label: str | None = None
    ) -> None:
        """Click the element like a person would: a real mouse press at its centre.

        With ``expect_label`` the element must still carry that label: the page
        may re-render between reading it and clicking, and then the same index
        is some other control.
        """
        box = await self.evaluate(
            """([s, i, expected]) => {"""
            + _JS_HELPERS
            + """ const e = pick(s, i);
                if (!e) return null;
                if (expected !== null && label(e) !== expected) return {changed: true};
                e.scrollIntoView({block: 'center', inline: 'center'});
                const r = e.getBoundingClientRect();
                const x = r.x + r.width / 2, y = r.y + r.height / 2;
                const top = document.elementFromPoint(x, y);
                // Covered by an overlay, or not laid out: the mouse would hit
                // something else, so the element is clicked directly.
                if (r.width > 0 && r.height > 0 && top && (top === e || e.contains(top))) {
                  return {x, y};
                }
                e.click();
                return {clicked: true};
            }""",
            [selector, index, expect_label],
            user_gesture=True,
        )
        if box is None:
            raise CdpError(f"На странице нет элемента {selector}")
        if box.get("changed"):
            raise CdpError("Элемент на странице сменился, нажатие отменено")
        if box.get("clicked"):
            return
        point = {"x": box["x"], "y": box["y"], "button": "left", "clickCount": 1}
        await self.send("Input.dispatchMouseEvent", {"type": "mouseMoved", **point})
        await self.send("Input.dispatchMouseEvent", {"type": "mousePressed", **point})
        await self.send("Input.dispatchMouseEvent", {"type": "mouseReleased", **point})

    async def fill(self, selector: str, value: str, *, index: int | None = None) -> None:
        """Replace a field's content as if it were typed: frameworks see real input."""
        found = await self.evaluate(
            """([s, i]) => {"""
            + _JS_HELPERS
            + """ const e = pick(s, i);
                if (!e) return false;
                e.scrollIntoView({block: 'center'});
                e.focus();
                if (typeof e.select === 'function') e.select();
                else document.execCommand('selectAll', false, null);
                return true;
            }""",
            [selector, index],
            user_gesture=True,
        )
        if not found:
            raise CdpError(f"На странице нет поля {selector}")
        if value:
            await self.send("Input.insertText", {"text": value})
        else:
            await self.evaluate("() => document.execCommand('delete', false, null)")

    async def press_enter(self) -> None:
        key = {
            "key": "Enter",
            "code": "Enter",
            "windowsVirtualKeyCode": 13,
            "nativeVirtualKeyCode": 13,
        }
        await self.send(
            "Input.dispatchKeyEvent",
            {"type": "keyDown", "text": "\r", "unmodifiedText": "\r", **key},
        )
        await self.send("Input.dispatchKeyEvent", {"type": "keyUp", **key})
