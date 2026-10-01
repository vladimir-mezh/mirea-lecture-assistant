from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Coroutine
from typing import Any, TypeVar

log = logging.getLogger(__name__)

T = TypeVar("T")


class AsyncRuntime:
    """A single long-lived event loop shared by every async call in the app.

    ``asyncio.run`` per call cannot be used here: pymirea keeps one httpx client
    alive between the login and the 2FA step, and the browser connection stays bound
    to the loop that created it. As soon as the first loop closes, the second
    call fails with "Event loop is closed".
    """

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._health_at = time.monotonic()

    def healthy(self) -> bool:
        loop = self._loop
        if loop is None:
            return True
        if loop.is_closed() or self._thread is None or not self._thread.is_alive():
            return False
        try:
            loop.call_soon_threadsafe(self._ack_health)
        except RuntimeError:
            return False
        return time.monotonic() - self._health_at < 60

    def _ack_health(self) -> None:
        self._health_at = time.monotonic()

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
                self._health_at = time.monotonic()
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
        # Worker threads waiting on a coroutine would wait forever once the loop
        # stops, and Qt waits for those threads: after «Выход» the process stayed
        # alive, invisible, and kept the exe locked against an update.
        try:
            asyncio.run_coroutine_threadsafe(_cancel_pending(), loop).result(3)
        except Exception:
            log.warning("async_runtime_cancel_incomplete", exc_info=True)
        loop.call_soon_threadsafe(loop.stop)
        if thread is not None:
            thread.join(timeout=5)
        if thread is not None and thread.is_alive():
            # Still inside blocking work: closing a running loop raises at exit.
            log.warning("async_runtime_still_busy")
            return
        loop.close()
        log.info("async_runtime_stopped")


async def _cancel_pending() -> None:
    current = asyncio.current_task()
    tasks = [task for task in asyncio.all_tasks() if task is not current]
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


_runtime = AsyncRuntime()


def run_async(coroutine: Coroutine[Any, Any, T], timeout: float | None = None) -> T:
    """Run a coroutine on the app-wide event loop from any thread."""
    return _runtime.run(coroutine, timeout)


def shutdown_async_runtime() -> None:
    _runtime.shutdown()


def async_runtime_healthy() -> bool:
    return _runtime.healthy()
