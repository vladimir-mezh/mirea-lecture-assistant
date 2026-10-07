import asyncio
from types import SimpleNamespace

import httpx
import pytest

from mirea_lecture_assistant.domain import SessionState
from mirea_lecture_assistant.mirea_service import MireaService
from mirea_lecture_assistant.reliability import (
    attendance_failure_counts_for_chat,
    login_retry_delay,
    should_retry_login,
)


def test_login_backoff_is_bounded():
    assert [login_retry_delay(n) for n in range(1, 7)] == [15, 30, 60, 120, 240, 300]


def test_only_explicit_credential_errors_stop_automatic_retries():
    assert not should_retry_login("Неверный логин или пароль")
    assert not should_retry_login("Gmail отклонил пароль приложения")
    assert not should_retry_login("Яндекс отклонил пароль или способ входа")
    assert should_retry_login("Сервис временно недоступен")
    assert should_retry_login("Внутренняя ошибка")


def test_network_and_auth_failures_do_not_trigger_public_chat_fallback():
    assert not attendance_failure_counts_for_chat("МИРЭА временно недоступна")
    assert not attendance_failure_counts_for_chat("Сессия истекла, требуется перелогиниться")
    assert not attendance_failure_counts_for_chat("Network timeout")
    assert attendance_failure_counts_for_chat("QR больше не действует")


def test_a_network_failure_keeps_the_session_and_its_state_unknown(pulse):
    async def offline():
        raise httpx.ConnectTimeout("offline")

    pulse._ensure_aspnet_cookie = lambda self: offline()
    service = MireaService({"session-cookie": "preserved"})

    assert asyncio.run(service.verify_state()) is SessionState.UNKNOWN
    assert service.session == {"session-cookie": "preserved"}


def test_a_refusal_of_a_saved_cookie_is_checked_again_without_it(pulse):
    """With a saved cookie pymirea skips the bootstrap, and a firewall page on the
    lesson call reads as "session expired"; a fresh bootstrap decides instead."""
    calls = []

    async def lesson_call(self, _url, _payload):
        calls.append(dict(self.session_cookies))
        if ".AspNetCore.Cookies" in self.session_cookies and len(calls) == 1:
            return None, "Сессия МИРЭА истекла. Перелогиньтесь."
        return b"", None

    pulse._grpc_unary = lesson_call
    service = MireaService({".AspNetCore.Cookies": "saved", "access_token": "a"})

    assert asyncio.run(service.verify_state()) is SessionState.VALID
    assert len(calls) == 2
    assert ".AspNetCore.Cookies" not in calls[1]


class FakeGrades:
    """pymirea's MireaGrades with scripted answers for the bootstrap and one call."""

    bootstrap = (True, None)
    unary = (b"", None)
    schedule = None
    closed = 0

    def __init__(self, session_cookies):
        self.session_cookies = session_cookies

    LESSONS_URL = "https://pulse.mirea.ru/lessons"

    async def _ensure_aspnet_cookie(self):
        return self.bootstrap

    async def _grpc_unary(self, _url, _payload):
        return self.unary

    @staticmethod
    def _encode_date_request(year, month, day):
        return bytes([year % 100, month, day])

    async def get_schedule(self, days=7):
        return self.schedule

    async def close(self):
        FakeGrades.closed += 1


@pytest.fixture
def pulse(monkeypatch):
    import pymirea.grades

    import mirea_lecture_assistant.pulse_api

    class Grades(FakeGrades):
        pass

    monkeypatch.setattr(pymirea.grades, "MireaGrades", Grades)
    monkeypatch.setattr(mirea_lecture_assistant.pulse_api, "MireaGrades", Grades)
    return Grades


def test_a_session_pulse_refuses_is_expired(pulse):
    pulse.bootstrap = (False, "Сессия истекла. Перелогиньтесь в МИРЭА.")

    service = MireaService({"session-cookie": "expired"})

    assert asyncio.run(service.verify_state()) is SessionState.EXPIRED


def test_a_session_pulse_still_accepts_is_kept_even_on_a_free_day(pulse):
    """0.2.3 threw working sessions away on a page check alone: new login, new code."""
    pulse.unary = (b"", None)  # a day without lessons is an empty, successful answer

    assert asyncio.run(MireaService({"c": "fine"}).verify_state()) is SessionState.VALID


@pytest.mark.parametrize(
    "bootstrap",
    [
        (False, "МИРЭА не отвечает. Попробуйте позже."),
        (False, "МИРЭА временно недоступна. Попробуйте через 30 сек."),
        # What Pulse's firewall does to a VPN address; a new login cannot help.
        (False, "Не удалось получить cookie (.AspNetCore.Cookies). Перелогиньтесь."),
    ],
)
def test_an_unreachable_or_blocked_pulse_is_not_an_expired_session(pulse, bootstrap):
    pulse.bootstrap = bootstrap

    assert asyncio.run(MireaService({"c": "fine"}).verify_state()) is SessionState.UNKNOWN


@pytest.mark.parametrize(
    ("answer", "state"),
    [
        ((None, "Сессия МИРЭА истекла. Перелогиньтесь."), SessionState.EXPIRED),
        ((None, "Ошибка сервера: 401"), SessionState.EXPIRED),
        ((None, "Unauthenticated"), SessionState.EXPIRED),
        ((None, "Ошибка сервера: 403"), SessionState.UNKNOWN),
        ((None, "Сервер не отвечает"), SessionState.UNKNOWN),
    ],
)
def test_the_lesson_call_decides_after_a_bootstrap(pulse, answer, state):
    pulse.unary = answer

    assert asyncio.run(MireaService({"c": "x"}).verify_state()) is state


def test_no_lessons_from_an_accepting_pulse_is_an_empty_schedule(pulse):
    pulse.schedule = SimpleNamespace(success=False, message="Нет пар в ближайшие дни", lessons=None)
    pulse.unary = (b"", None)

    assert asyncio.run(MireaService({"c": "x"}).get_schedule(14)) == []


def test_no_lessons_because_every_call_was_refused_means_an_expired_session(pulse):
    pulse.schedule = SimpleNamespace(success=False, message="Нет пар в ближайшие дни", lessons=None)
    pulse.unary = (None, "Сессия МИРЭА истекла. Перелогиньтесь.")

    with pytest.raises(RuntimeError, match="Сессия истекла"):
        asyncio.run(MireaService({"c": "x"}).get_schedule(14))


def test_no_lessons_while_pulse_is_unreachable_is_not_reported_as_a_free_fortnight(pulse):
    pulse.schedule = SimpleNamespace(success=False, message="Нет пар в ближайшие дни", lessons=None)
    pulse.bootstrap = (False, "МИРЭА временно недоступна. Попробуйте через 30 сек.")

    with pytest.raises(RuntimeError, match="Пульс не вернул расписание"):
        asyncio.run(MireaService({"c": "x"}).get_schedule(14))


def test_a_generic_no_answer_carries_the_actual_reason(pulse):
    """pymirea hides every failure behind "МИРЭА не отвечает"; the journal and
    the status line need the real one (timeout, certificate, DNS)."""
    import logging

    from mirea_lecture_assistant.mirea_service import capture_upstream_failures

    capture_upstream_failures()

    async def failing_schedule(self, days=7):
        try:
            raise httpx.ConnectError(
                "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
                "unable to get local issuer certificate"
            )
        except Exception as exc:  # noqa: BLE001 - mirrors pymirea's own catch-all
            logging.getLogger("pymirea.grades").warning(f"BRS auth bootstrap failed: {exc}")
        return SimpleNamespace(success=False, message="МИРЭА не отвечает. Попробуйте позже.")

    pulse.get_schedule = failing_schedule

    with pytest.raises(RuntimeError) as failure:
        asyncio.run(MireaService({"c": "x"}).get_schedule(14))

    assert "МИРЭА не отвечает" in str(failure.value)
    assert "сертификат" in str(failure.value)


@pytest.mark.parametrize(
    ("failure", "words"),
    [
        (httpx.ReadTimeout("timed out"), "не ответил вовремя"),
        (httpx.ConnectError("[Errno 11001] getaddrinfo failed"), "не находится"),
        (httpx.ConnectError("[WinError 10061] refused"), "не удалось подключиться"),
        (httpx.TooManyRedirects("Exceeded maximum allowed redirects."), "переадресаци"),
        (httpx.RemoteProtocolError("Server disconnected"), "оборвал"),
    ],
)
def test_network_failures_are_named_in_plain_words(failure, words):
    from mirea_lecture_assistant.mirea_service import failure_reason

    assert words in failure_reason(failure)


@pytest.mark.parametrize(
    "message",
    [
        "Ошибка отметки посещаемости",
        "Сервер вернул SPA вместо ответа",
        "Неизвестный ответ сервера",
        "Отметка пока недоступна",
    ],
)
def test_server_side_and_network_failures_are_not_rejections(message):
    from mirea_lecture_assistant.reliability import attendance_failure_counts_for_chat

    assert not attendance_failure_counts_for_chat(message)
