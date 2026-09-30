"""Run the saved login flow without printing credentials or the one-time code.

Each stage is executed the way the GUI executes it — from a worker thread, on the
shared application event loop — so this script reproduces the real environment of
the login and reports where it breaks.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

from mirea_lecture_assistant.app import _session_key
from mirea_lecture_assistant.async_runtime import run_async, shutdown_async_runtime
from mirea_lecture_assistant.email_otp import ImapOtpReader
from mirea_lecture_assistant.logging_setup import configure_logging
from mirea_lecture_assistant.mirea_service import MireaService
from mirea_lecture_assistant.paths import data_dir
from mirea_lecture_assistant.security import SessionStore


def main() -> None:
    configure_logging(data_dir() / "logs")
    store = SessionStore()
    mirea_credentials = store.load_credentials()
    email_credentials = store.load_email_credentials()
    print("credentials configured:", bool(mirea_credentials), bool(email_credentials))
    if not mirea_credentials or not email_credentials:
        return

    MireaService.configure(_session_key())
    service = MireaService()
    reader = ImapOtpReader()
    # A fresh pool thread per stage, exactly like QThreadPool in the UI.
    with ThreadPoolExecutor(max_workers=3) as pool:
        latest_uid = pool.submit(reader.latest_uid, email_credentials).result()
        started = datetime.now(UTC)
        first = pool.submit(run_async, service.login(*mirea_credentials)).result()
        print("first stage:", first.success, bool(first.challenge), first.message)
        if not first.challenge:
            return

        code = pool.submit(
            reader.wait_for_code,
            email_credentials,
            started,
            120,
            after_uid=latest_uid,
        ).result()
        print("fresh OTP acquired: True")
        second = pool.submit(run_async, service.complete_2fa(first.challenge, code)).result()
        print("second stage:", second.success, second.message, bool(second.tokens))

    shutdown_async_runtime()


if __name__ == "__main__":
    main()
