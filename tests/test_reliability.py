import asyncio

import httpx
import pytest

from mirea_lecture_assistant.domain import SessionState
from mirea_lecture_assistant.mirea_service import MireaService, classify_session_response
from mirea_lecture_assistant.reliability import (
    attendance_failure_counts_for_chat,
    login_retry_delay,
    should_retry_login,
)


def test_session_response_distinguishes_expiry_from_server_outage():
    assert classify_session_response(200, "https://attendance.mirea.ru/") is SessionState.VALID
    assert (
        classify_session_response(200, "https://login.mirea.ru/realms/mirea/login")
        is SessionState.EXPIRED
    )
    assert classify_session_response(503, "https://attendance.mirea.ru/") is SessionState.UNKNOWN


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


def test_network_timeout_keeps_session_state_unknown(monkeypatch):
    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, url):
            raise httpx.ConnectTimeout("offline", request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "AsyncHTTPTransport", lambda **_kwargs: object())
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: Client())
    service = MireaService({"session-cookie": "preserved"})

    assert asyncio.run(service.verify_state()) is SessionState.UNKNOWN
    assert service.session == {"session-cookie": "preserved"}


def test_login_redirect_is_confirmed_as_expired(monkeypatch):
    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, _url):
            request = httpx.Request("GET", "https://login.mirea.ru/realms/mirea/login")
            return httpx.Response(200, request=request)

    monkeypatch.setattr(httpx, "AsyncHTTPTransport", lambda **_kwargs: object())
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: Client())
    service = MireaService({"session-cookie": "expired"})

    assert asyncio.run(service.verify_state()) is SessionState.EXPIRED


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
