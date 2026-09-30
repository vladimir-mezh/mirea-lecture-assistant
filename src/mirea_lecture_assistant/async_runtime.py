from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Coroutine
from typing import Any, TypeVar

log = logging.getLogger(__name__)

T = TypeVar("T")


class AsyncRuntime:
    """A single long-lived event loop shared by every async call in the app.

    ``asyncio.run`` per call cannot be used here: pymirea keeps one httpx client
    alive between the login and the 2FA step, and Playwright objects stay bound
    to the loop that created them. As soon as the first loop closes, the second
    call fails with "Event loop is closed".
    """

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def loop(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._loop is None or self._loop.is_closed():
                loop = asyncio.new_event_loop()
                thread = threading.Thread(
                    target=self._serve,
                    args=(loop,),
                    name="mirea-async-runtime",
                    daemon=True,
                )
                thread.start()
                self._loop = loop
                self._thread = thread
                log.info("async_runtime_started")
            return self._loop

    @staticmethod
    def _serve(loop: asyncio.AbstractEventLoop) -> None:
        asyncio.set_event_loop(loop)
        loop.run_forever()

    def run(self, coroutine: Coroutine[Any, Any, T], timeout: float | None = None) -> T:
        """Run ``coroutine`` on the shared loop and wait for its result."""
        future = asyncio.run_coroutine_threadsafe(coroutine, self.loop())
        try:
            return future.result(timeout)
        except TimeoutError:
            future.cancel()
            raise

    def shutdown(self) -> None:
        with self._lock:
            loop, thread = self._loop, self._thread
            self._loop = self._thread = None
        if loop is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(loop.stop)
        if thread is not None:
            thread.join(timeout=5)
        loop.close()
        log.info("async_runtime_stopped")


_runtime = AsyncRuntime()


def run_async(coroutine: Coroutine[Any, Any, T], timeout: float | None = None) -> T:
    """Run a coroutine on the app-wide event loop from any thread."""
    return _runtime.run(coroutine, timeout)


def shutdown_async_runtime() -> None:
    _runtime.shutdown()
