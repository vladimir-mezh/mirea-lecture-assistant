from __future__ import annotations

import asyncio
import threading

import pytest

from mirea_lecture_assistant.async_runtime import AsyncRuntime


def test_state_survives_between_calls():
    """The login/2FA regression: step two reuses a resource bound to step one's loop."""
    runtime = AsyncRuntime()
    try:

        async def make_event():
            return asyncio.Event()

        event = runtime.run(make_event())

        async def use_event():
            event.set()
            await event.wait()
            return asyncio.get_running_loop()

        loop = runtime.run(use_event())
        assert loop is runtime.loop()
        assert not loop.is_closed()
    finally:
        runtime.shutdown()


def test_an_unresponsive_shared_loop_is_not_healthy(monkeypatch):
    runtime = AsyncRuntime()

    class Loop:
        def is_closed(self):
            return False

        def call_soon_threadsafe(self, callback):
            pass  # blocked native operation: callback never executes

    class Thread:
        def is_alive(self):
            return True

    runtime._loop = Loop()
    runtime._thread = Thread()
    runtime._health_at = 0
    monkeypatch.setattr("mirea_lecture_assistant.async_runtime.time.monotonic", lambda: 61)
    assert not runtime.healthy()


def test_runs_off_the_calling_thread():
    runtime = AsyncRuntime()
    try:

        async def worker_thread():
            return threading.current_thread().name

        assert runtime.run(worker_thread()) != threading.current_thread().name
    finally:
        runtime.shutdown()


def test_exceptions_propagate_to_the_caller():
    runtime = AsyncRuntime()
    try:

        async def boom():
            raise RuntimeError("Сервер МИРЭА не отвечает")

        with pytest.raises(RuntimeError, match="Сервер МИРЭА не отвечает"):
            runtime.run(boom())
    finally:
        runtime.shutdown()


def test_timeout_cancels_the_pending_call():
    runtime = AsyncRuntime()
    try:

        async def slow():
            await asyncio.sleep(5)

        with pytest.raises(TimeoutError):
            runtime.run(slow(), timeout=0.05)
    finally:
        runtime.shutdown()


def test_restarts_after_shutdown():
    runtime = AsyncRuntime()

    async def value():
        return 42

    assert runtime.run(value()) == 42
    runtime.shutdown()
    assert runtime.run(value()) == 42
    runtime.shutdown()
