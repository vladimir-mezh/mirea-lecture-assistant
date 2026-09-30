from __future__ import annotations

import os
from datetime import datetime, timedelta
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["MIREA_ASSISTANT_SMOKE_TEST"] = "1"

import pytest
from PySide6.QtWidgets import QApplication

from mirea_lecture_assistant import paths
from mirea_lecture_assistant.database import Database
from mirea_lecture_assistant.domain import Lesson, PendingAttendance, SessionState
from mirea_lecture_assistant.security import SessionStore
from mirea_lecture_assistant.ui import MainWindow


@pytest.fixture
def window(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(SessionStore, "load", lambda _self: None)
    monkeypatch.setattr(SessionStore, "load_credentials", lambda _self: None)
    monkeypatch.setattr(SessionStore, "load_email_credentials", lambda _self: None)
    app = QApplication.instance() or QApplication([])
    instance = MainWindow(Database(tmp_path / "assistant.sqlite3"))
    instance.schedule_timer.stop()
    instance.lecture_watch_timer.stop()
    yield instance
    instance.force_exit = True
    instance.close()
    assert app is not None


def test_detected_qr_keeps_the_lesson_that_was_active(window):
    window.active_lecture_id = "lesson-old"
    window.active_lecture_url = "https://mts-link.ru/event/old"
    window._handle_qr(
        "https://pulse.mirea.ru/selfapprove?token=123e4567-e89b-12d3-a456-426614174000"
    )

    event = window.db.recent_qr_events()[0]
    pending = window.pending_qr[event.id]
    assert event.lesson_id == "lesson-old"
    assert pending.lesson_id == "lesson-old"


def test_late_attendance_success_does_not_mark_the_next_lesson(window):
    event_id = window.db.add_qr_event("fingerprint", "retrying", lesson_id="lesson-old")
    window.pending_qr[event_id] = PendingAttendance(
        raw_data="secret",
        lesson_id="lesson-old",
        lecture_url="https://mts-link.ru/event/old",
        detected_at=datetime.now().astimezone(),
    )
    window.active_lecture_id = "lesson-new"

    window._attendance_finished(event_id, SimpleNamespace(success=True, message="OK"))

    assert "lesson-old" in window.db.get_setting("marked_lessons", [])
    assert "lesson-new" not in window.db.get_setting("marked_lessons", [])


def test_unknown_session_state_keeps_the_current_session(window, monkeypatch):
    window.mirea.session = {"cookie": "still-useful"}
    monkeypatch.setattr(
        window,
        "_run",
        lambda _function, done, _busy, failed=None: done(SessionState.UNKNOWN),
    )

    window._recover_expired_session("schedule_refresh")

    assert window.mirea.session == {"cookie": "still-useful"}


def test_expired_session_starts_automatic_login(window, monkeypatch):
    window.mirea.session = {"cookie": "expired"}
    called = []
    monkeypatch.setattr(
        window,
        "_run",
        lambda _function, done, _busy, failed=None: done(SessionState.EXPIRED),
    )
    monkeypatch.setattr(window, "_auto_login", lambda: called.append(True))

    window._recover_expired_session("schedule_refresh")

    assert window.mirea.session == {}
    assert called == [True]


def test_startup_checks_saved_session_before_auto_login(window, monkeypatch):
    window.mirea.session = {"cookie": "saved"}
    checks = []
    auto_logins = []
    monkeypatch.delenv("MIREA_ASSISTANT_SMOKE_TEST", raising=False)
    monkeypatch.setattr(window, "_run", lambda *args, **kwargs: checks.append(True))
    monkeypatch.setattr(window, "_auto_login", lambda: auto_logins.append(True))

    window._startup_auth()

    assert checks == [True]
    assert auto_logins == []


def test_automatic_2fa_rejection_does_not_request_another_code(window, monkeypatch):
    window.automatic_login_cycle = True
    window.otp_submission_attempted = True
    window.pending_login_credentials = ("user", "password")
    retries = []
    failures = []
    monkeypatch.setattr(window, "_run_initial_login", lambda *args: retries.append(args))
    monkeypatch.setattr(window, "_schedule_login_retry", lambda *_: retries.append("retry"))
    monkeypatch.setattr(window, "_operation_failed", failures.append)

    window._login_finished(SimpleNamespace(challenge=object(), success=False, message="rejected"))

    assert retries == []
    assert failures == ["rejected"]
    assert not window.automatic_login_cycle


def test_background_auth_attempts_share_rolling_limit_across_flows_and_restarts(
    tmp_path, monkeypatch
):
    path = tmp_path / "assistant.sqlite3"
    first = Database(path)
    second = Database(path)
    now = [10_000.0]
    monkeypatch.setattr("mirea_lecture_assistant.database.time.time", lambda: now[0])

    for flow in ("mirea", "sdo", "sdo", "mirea", "sdo"):
        assert first.reserve_auth_attempt(flow)
    assert not second.reserve_auth_attempt("mirea")
    now[0] += 1799
    assert not second.reserve_auth_attempt("sdo")
    now[0] += 1
    assert second.reserve_auth_attempt("sdo")


def test_background_auth_limit_counts_previous_version_attempts(tmp_path, monkeypatch):
    db = Database(tmp_path / "assistant.sqlite3")
    monkeypatch.setattr("mirea_lecture_assistant.database.time.time", lambda: 10_000.0)
    db.set_setting("auth_attempt_last_mirea", 9999.0)
    db.set_setting("auth_attempt_last_sdo", 9998.0)

    assert db.reserve_auth_attempt("mirea")
    assert len(db.get_setting("auth_attempts")) == 3


def test_three_failed_health_checks_escalate_to_browser_restart(window, monkeypatch):
    now = datetime.now().astimezone()
    lesson = Lesson(
        external_id="lesson",
        subject_name="Надёжность",
        lesson_type="ЛК",
        start_at=now - timedelta(minutes=10),
        end_at=now + timedelta(minutes=60),
    )
    window.db.sync_lessons([lesson], now.replace(hour=0, minute=0, second=0, microsecond=0))
    window.active_lecture_id = lesson.external_id
    window.active_lecture_url = "https://mts-link.ru/event/lesson"
    reopened = []
    restarted = []
    monkeypatch.setattr(
        window,
        "_run",
        lambda _function, done, _busy, failed=None: done("lost"),
    )
    monkeypatch.setattr(window, "_open_lecture", lambda *args, **kwargs: reopened.append(args))
    monkeypatch.setattr(window, "_restart_lecture_browser", lambda: restarted.append(True))

    window._lecture_watch_tick()
    window._lecture_watch_tick()
    window._lecture_watch_tick()

    assert len(reopened) == 2
    assert restarted == [True]


def test_room_end_stops_monitoring_even_after_scheduled_end(window, monkeypatch):
    now = datetime.now().astimezone()
    lesson = Lesson(
        external_id="overtime",
        subject_name="Надёжность",
        lesson_type="ЛК",
        start_at=now - timedelta(hours=2),
        end_at=now - timedelta(minutes=20),
    )
    window.db.sync_lessons([lesson], now.replace(hour=0, minute=0, second=0, microsecond=0))
    window.active_lecture_id = lesson.external_id
    window.active_lecture_url = "https://mts-link.ru/event/overtime"
    monkeypatch.setattr(window, "_run", lambda _fn, done, _busy, failed=None: done("ended"))

    window._lecture_watch_tick()

    assert window.active_lecture_id is None
    assert window.active_lecture_url is None


def test_new_rotating_qr_replaces_an_old_pending_code(window):
    window.active_lecture_id = "lesson"
    window.active_lecture_url = "https://mts-link.ru/event/lesson"
    first = "https://pulse.mirea.ru/selfapprove?token=123e4567-e89b-12d3-a456-426614174000"
    second = "https://pulse.mirea.ru/selfapprove?token=223e4567-e89b-12d3-a456-426614174000"

    window._handle_qr(first)
    first_event = window.db.recent_qr_events()[0]
    window._handle_qr(second)

    assert first_event.id not in window.pending_qr
    assert len(window.pending_qr) == 1


def test_rejections_are_counted_across_rotating_codes(window, monkeypatch):
    window.active_lecture_id = "lesson"
    sent = []
    monkeypatch.setattr(
        window, "_send_chat_fallback", lambda reason, lesson: sent.append((reason, lesson))
    )

    for number in range(5):
        event_id = window.db.add_qr_event(f"fingerprint-{number}", "detected", lesson_id="lesson")
        window.pending_qr[event_id] = PendingAttendance(
            raw_data=f"qr-{number}",
            lesson_id="lesson",
            lecture_url="https://mts-link.ru/event/lesson",
            detected_at=datetime.now().astimezone(),
        )
        window.latest_qr_event_by_lesson["lesson"] = event_id
        window._record_attendance_failure(event_id)

    assert sent == [("five_attendance_failures", "lesson")]


def test_finishing_a_room_discards_its_in_memory_qr(window):
    event_id = window.db.add_qr_event("fingerprint", "retrying", lesson_id="lesson")
    window.pending_qr[event_id] = PendingAttendance(
        raw_data="short-lived-secret",
        lesson_id="lesson",
        lecture_url="https://mts-link.ru/event/lesson",
        detected_at=datetime.now().astimezone(),
    )
    window.latest_qr_event_by_lesson["lesson"] = event_id
    window.active_lecture_id = "lesson"

    window._finish_active_lecture("room_ended")

    assert not window.pending_qr
    assert not window.latest_qr_event_by_lesson
