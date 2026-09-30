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
from mirea_lecture_assistant.domain import Lesson, PendingAttendance, RuleMode, SessionState
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
    # Unattended: reported through the tray, not a modal dialog nobody sees.
    monkeypatch.setattr(
        window, "_background_problem", lambda _title, message: failures.append(message)
    )
    monkeypatch.setattr(window, "_operation_failed", lambda message: failures.append("modal"))

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


def test_session_recovery_does_not_start_a_second_login_while_the_code_is_awaited(
    window, monkeypatch
):
    """Slow mail (Яндекс, Mail.ru) used to let the minute timer start another SSO flow."""
    from mirea_lecture_assistant.email_otp import EmailAccount

    window.pending_email_credentials = EmailAccount("student@mail.ru", "app-password")
    window.mirea.session = {"cookie": "stale"}
    started = []
    monkeypatch.setattr(
        window, "_run", lambda function, *_args, **_kwargs: started.append(function)
    )
    monkeypatch.setattr(window, "_auto_login", lambda: started.append("second-login"))

    window._login_finished(SimpleNamespace(challenge="challenge", success=False, message=""))
    window._recover_expired_session("schedule_refresh")

    assert window.login_in_progress
    assert "second-login" not in started
    assert len(started) == 1  # only the email wait itself


def test_expired_saved_session_is_dropped_before_automatic_login(window, monkeypatch):
    window.mirea.session = {"cookie": "expired"}
    sessions_at_login = []
    monkeypatch.setattr(
        window, "_auto_login", lambda: sessions_at_login.append(dict(window.mirea.session))
    )

    window._session_verified(SessionState.EXPIRED)

    assert sessions_at_login == [{}]


def _running_lesson(external_id: str = "running") -> Lesson:
    now = datetime.now().astimezone()
    return Lesson(
        external_id=external_id,
        subject_name="Надёжность",
        lesson_type="ЛК",
        start_at=now - timedelta(minutes=10),
        end_at=now + timedelta(minutes=60),
    )


def test_lecture_keeps_running_while_its_lesson_is_missing_from_the_schedule(window, monkeypatch):
    lesson = _running_lesson()
    window.active_lesson = lesson
    window.active_lecture_id = lesson.external_id
    window.active_lecture_url = "https://mts-link.ru/event/running"
    checks = []
    monkeypatch.setattr(window, "_run", lambda function, done, *_a, **_k: checks.append(done))

    window._lecture_watch_tick()

    assert window.active_lecture_id == lesson.external_id
    assert len(checks) == 1  # the room is still being watched


def test_a_rejected_token_is_not_resent(window, monkeypatch):
    window.mirea.session = {"cookie": "ok"}
    event_id = window.db.add_qr_event("fingerprint", "detected", lesson_id="lesson")
    window.pending_qr[event_id] = PendingAttendance(
        raw_data="qr",
        lesson_id="lesson",
        lecture_url=None,
        detected_at=datetime.now().astimezone(),
    )
    window.latest_qr_event_by_lesson["lesson"] = event_id
    scheduled = []
    monkeypatch.setattr(window, "_schedule_retry", scheduled.append)

    window._attendance_finished(
        event_id, SimpleNamespace(success=False, message="Токен недействителен"), "lesson"
    )

    assert scheduled == []
    assert event_id not in window.pending_qr
    assert window.db.recent_qr_events()[0].status == "failed"
    assert window.attendance_failures_by_lesson["lesson"] == 1


def test_only_one_retry_timer_runs_per_event(window, monkeypatch):
    from mirea_lecture_assistant import ui

    timers = []
    monkeypatch.setattr(ui.QTimer, "singleShot", lambda delay, callback: timers.append(delay))
    event_id = window.db.add_qr_event("fingerprint", "retrying", lesson_id="lesson")
    window.pending_qr[event_id] = PendingAttendance(
        raw_data="qr", lesson_id="lesson", lecture_url=None, detected_at=datetime.now().astimezone()
    )

    window._schedule_retry(event_id)
    window._schedule_retry(event_id)

    assert timers == [5_000]


def test_network_failures_back_off_and_stale_codes_are_dropped(window, monkeypatch):
    from mirea_lecture_assistant import ui

    timers = []
    monkeypatch.setattr(ui.QTimer, "singleShot", lambda delay, callback: timers.append(delay))
    event_id = window.db.add_qr_event("fingerprint", "retrying", lesson_id="lesson")
    window.pending_qr[event_id] = PendingAttendance(
        raw_data="qr", lesson_id="lesson", lecture_url=None, detected_at=datetime.now().astimezone()
    )
    for _ in range(9):
        window._schedule_retry(event_id)
        window.retry_scheduled.discard(event_id)
    assert timers[0] == 5_000 and timers[-1] == 120_000

    window.pending_qr[event_id].detected_at -= timedelta(hours=1)
    window._schedule_retry(event_id)
    assert event_id not in window.pending_qr


def test_link_typed_in_a_row_belongs_to_that_lesson_only(window):
    window._save_lesson_link("lesson-1", "https://mts-link.ru/event/1")

    assert window.db.get_resolved_link("lesson-1") == "https://mts-link.ru/event/1"
    assert window.db.get_link("Надёжность") == ""


def test_failed_sdo_sign_in_pauses_further_attempts(window, monkeypatch):
    calls = []

    def locked():
        calls.append(1)
        raise RuntimeError("SSO failed")

    monkeypatch.setattr(window, "_sign_in_to_sdo_locked", locked)

    with pytest.raises(RuntimeError):
        window._sign_in_to_sdo()
    assert window._sign_in_to_sdo() is False
    assert calls == [1]


def test_webinar_lookup_is_not_repeated_every_minute(window, monkeypatch):
    lesson = _running_lesson()
    runs = []
    monkeypatch.setattr(window, "_run", lambda *args, **kwargs: runs.append(args))

    window._resolve_from_sources(lesson)
    window._source_resolved(lesson, None, [])
    window._resolve_from_sources(lesson)

    assert len(runs) == 1


def test_schedule_failure_still_opens_an_already_found_room(window, monkeypatch):
    lesson = _running_lesson()
    today = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    window.db.sync_lessons([lesson], today)
    window.db.set_rule(lesson.subject_name, RuleMode.AUTO)
    window.db.set_resolved_link(lesson.external_id, "https://mts-link.ru/event/running")
    opened = []
    monkeypatch.setattr(window, "_open_lecture", lambda url, lesson_id: opened.append(url))
    monkeypatch.setattr(window, "_recover_expired_session", lambda _reason: None)

    window._schedule_refresh_failed("Пульс недоступен")

    assert opened == ["https://mts-link.ru/event/running"]


def test_auth_indicator_follows_the_session(window, monkeypatch):
    window.mirea.session = {"cookie": "expired"}
    monkeypatch.setattr(window, "_auto_login", lambda: None)
    monkeypatch.setattr(window, "refresh_schedule", lambda: None)
    monkeypatch.setattr(window.session_store, "save", lambda _session: None)

    window._session_verified(SessionState.EXPIRED)

    assert "истекла" in window.auth_status.text()
    window._login_finished(SimpleNamespace(challenge=None, success=True, message="", tokens={}))
    assert "вход выполнен" in window.auth_status.text()


def test_now_card_names_the_next_pair_and_its_mode(window):
    now = datetime.now().astimezone()
    lesson = Lesson(
        external_id="next",
        subject_name="Матанализ",
        lesson_type="ЛК",
        start_at=now + timedelta(minutes=30),
        end_at=now + timedelta(minutes=120),
    )
    window.db.sync_lessons([lesson], now.replace(hour=0, minute=0, second=0, microsecond=0))
    window.db.set_rule("Матанализ", RuleMode.AUTO)

    window._update_now_card()

    assert "Матанализ" in window.now_card.text()
    assert "Авто" in window.now_card.text()
    assert "Матанализ" in window.tray.toolTip()


def test_background_results_are_not_wiped_by_other_operations(window):
    window.statusBar().showMessage("Посещение отмечено", 8000)
    worker_done = []

    class FakeWorker:
        signals = SimpleNamespace(
            done=SimpleNamespace(connect=worker_done.append),
            failed=SimpleNamespace(connect=lambda _handler: None),
        )

        def setAutoDelete(self, _flag):
            return None

    window._start_worker(
        FakeWorker(),
        lambda _value: None,
        lambda _message: None,
        busy_text="Проверяем вкладку лекции…",
        pool=SimpleNamespace(start=lambda _worker: None),
    )
    assert "Проверяем вкладку" in window.activity_label.text()

    worker_done[0](None)

    assert window.activity_label.text() == ""
    assert window.statusBar().currentMessage() == "Посещение отмечено"


def test_schedule_modes_are_shown_in_russian(window):
    now = datetime.now().astimezone()
    lesson = Lesson("l1", "Физика", "ЛК", now + timedelta(hours=1), now + timedelta(hours=2))
    window.db.sync_lessons([lesson], now.replace(hour=0, minute=0, second=0, microsecond=0))
    window.db.set_rule("Физика", RuleMode.IGNORE)

    window._fill_schedule()

    assert window.schedule_table.item(0, 5).text() == "Не открывать"
    window.subject_rule_subject.setCurrentText("Физика")
    window.subject_rule_mode.setCurrentIndex(window.subject_rule_mode.findData("AUTO"))
    assert window.db.get_rule("Физика") is RuleMode.AUTO


def _watch_room_that_says_ended(window, monkeypatch, lesson):
    today = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    window.db.sync_lessons([lesson], today)
    room = "https://my.mts-link.ru/j/1/2"
    window.db.set_resolved_link(lesson.external_id, room)
    window.joined_lessons.add(lesson.external_id)
    window.active_lecture_id = lesson.external_id
    window.active_lecture_url = room
    calls = {"lookups": [], "closed": []}
    monkeypatch.setattr(window, "_resolve_from_sources", calls["lookups"].append)

    def run(function, done, *_args, **_kwargs):
        if function == window.browser.lecture_state:
            done("ended")
        elif function == window.browser.close_lecture_tab:
            calls["closed"].append(True)

    monkeypatch.setattr(window, "_run", run)
    monkeypatch.setattr(type(window.browser), "probably_running", property(lambda _self: True))
    window._lecture_watch_tick()
    return room, calls


def test_a_room_that_ends_long_before_the_pair_is_replaced(window, monkeypatch):
    """The first real lecture: a dead room left the app on about:blank for the whole pair."""
    lesson = _running_lesson("early")
    room, calls = _watch_room_that_says_ended(window, monkeypatch, lesson)

    assert window.active_lecture_id is None
    assert window.db.get_resolved_link("early") == ""
    assert room in window._rejected_rooms("early")
    assert "early" not in window.joined_lessons
    assert [item.external_id for item in calls["lookups"]] == ["early"]
    assert calls["closed"] == []  # the next room opens in the same tab


def test_a_room_that_ends_with_the_pair_just_finishes(window, monkeypatch):
    now = datetime.now().astimezone()
    lesson = Lesson(
        "over", "Надёжность", "ЛК", now - timedelta(minutes=85), now + timedelta(minutes=5)
    )
    room, calls = _watch_room_that_says_ended(window, monkeypatch, lesson)

    assert window.active_lecture_id is None
    assert window.db.get_resolved_link("over") == room
    assert calls["lookups"] == []
    assert calls["closed"] == [True]


def test_a_rejected_room_is_not_reopened_from_the_cache(window, monkeypatch):
    lesson = _running_lesson("cached")
    today = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    window.db.sync_lessons([lesson], today)
    window.db.set_rule(lesson.subject_name, RuleMode.AUTO)
    window.db.set_resolved_link("cached", "https://my.mts-link.ru/j/1/2")
    window._reject_room("cached", "https://my.mts-link.ru/j/1/2")
    opened, lookups = [], []
    monkeypatch.setattr(window, "_open_lecture", lambda url, lesson_id: opened.append(url))
    monkeypatch.setattr(window, "_resolve_from_sources", lookups.append)

    window._evaluate_current_lessons(window.db.list_lessons())

    assert opened == []
    assert [item.external_id for item in lookups] == ["cached"]


def test_a_rejected_room_becomes_eligible_again_later(window, monkeypatch):
    from mirea_lecture_assistant import ui

    window._reject_room("lesson", "https://my.mts-link.ru/j/1/2")
    later = ui.time.time() + ui.REJECTED_ROOM_SECONDS + 1
    monkeypatch.setattr(ui.time, "time", lambda: later)

    assert window._rejected_rooms("lesson") == frozenset()


def test_a_link_typed_by_the_student_overrides_a_rejection(window):
    window._reject_room("lesson", "https://my.mts-link.ru/j/1/2")

    window._save_lesson_link("lesson", "https://my.mts-link.ru/j/1/2")

    assert window._rejected_rooms("lesson") == frozenset()
