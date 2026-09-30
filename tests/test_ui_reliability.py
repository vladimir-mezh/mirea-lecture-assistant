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
    from mirea_lecture_assistant import ui

    window.active_lecture_id = "lesson"
    sent = []
    monkeypatch.setattr(
        window, "_send_chat_fallback", lambda reason, lesson: sent.append((reason, lesson))
    )
    clock = [1000.0]
    monkeypatch.setattr(ui.time, "monotonic", lambda: clock[0])

    for number in range(5):
        clock[0] += 40  # rotating codes rejected over more than two minutes
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

    challenge = SimpleNamespace(kind="email_code", field_name="emailCode")
    window._login_finished(SimpleNamespace(challenge=challenge, success=False, message=""))
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


def test_a_room_that_ends_after_the_pair_just_finishes(window, monkeypatch):
    now = datetime.now().astimezone()
    lesson = Lesson(
        "over", "Надёжность", "ЛК", now - timedelta(minutes=96), now - timedelta(minutes=6)
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


def test_the_pair_is_not_left_before_five_minutes_past_its_end(window, monkeypatch):
    """Even with attendance marked, a room closing at the bell is not the end."""
    now = datetime.now().astimezone()
    lesson = Lesson(
        "bell", "Надёжность", "ЛК", now - timedelta(minutes=88), now - timedelta(minutes=2)
    )
    window.db.set_setting("marked_lessons", ["bell"])
    _room, calls = _watch_room_that_says_ended(window, monkeypatch, lesson)

    assert "bell" in window.room_lost_lessons
    assert [item.external_id for item in calls["lookups"]] == ["bell"]
    assert calls["closed"] == []


def test_a_room_closed_five_minutes_in_is_followed_by_the_new_one(window, monkeypatch):
    now = datetime.now().astimezone()
    lesson = Lesson(
        "fresh", "Надёжность", "ЛК", now - timedelta(minutes=5), now + timedelta(minutes=85)
    )
    _watch_room_that_says_ended(window, monkeypatch, lesson)
    assert "fresh" in window.room_lost_lessons
    assert window._lookup_pause(lesson) == 60

    opened = []
    monkeypatch.setattr(window, "_open_lecture", lambda url, lesson_id: opened.append(url))
    window.db.set_rule(lesson.subject_name, RuleMode.AUTO)
    new_room = SimpleNamespace(
        join_url="https://my.mts-link.ru/j/1/3",
        is_joinable=True,
        title="Лекция",
        start_at=now,
        end_at=None,
        groups=(),
        webinar_id=2,
    )
    window._source_resolved(lesson, new_room, [])

    assert opened == ["https://my.mts-link.ru/j/1/3"]
    assert "fresh" not in window.room_lost_lessons


def _active(window, lesson, url):
    today = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    window.db.sync_lessons([lesson], today)
    window.active_lecture_id = lesson.external_id
    window.active_lecture_url = url
    window.joined_lessons.add(lesson.external_id)


def _webinar(url, webinar_id=9):
    return SimpleNamespace(
        join_url=url,
        is_joinable=True,
        title="Лекция",
        start_at=datetime.now().astimezone(),
        end_at=None,
        groups=("ИКБО-01-24",),
        webinar_id=webinar_id,
    )


def test_the_sdo_is_checked_again_while_the_pair_runs(window, monkeypatch):
    lesson = _running_lesson("live")
    _active(window, lesson, "https://my.mts-link.ru/j/1/2")
    lookups = []
    monkeypatch.setattr(window, "_resolve_from_sources", lookups.append)

    window._evaluate_current_lessons(window.db.list_lessons())

    assert [item.external_id for item in lookups] == ["live"]
    # First twenty minutes: every two minutes; later: every five.
    assert window._lookup_pause(lesson) == 120
    later = Lesson(
        "late", "Надёжность", "ЛК", lesson.start_at - timedelta(minutes=40), lesson.end_at
    )
    window.active_lecture_id = "late"
    assert window._lookup_pause(later) == 300


def test_a_newer_room_found_mid_pair_replaces_the_current_one(window, monkeypatch):
    lesson = _running_lesson("live")
    _active(window, lesson, "https://my.mts-link.ru/j/1/2")
    opened = []
    monkeypatch.setattr(
        window, "_open_lecture", lambda url, lesson_id, force=False: opened.append((url, force))
    )

    window._source_resolved(lesson, _webinar("https://my.mts-link.ru/j/1/2"), [])
    assert opened == []  # the same room: stay

    window._source_resolved(lesson, _webinar("https://my.mts-link.ru/j/1/5", 10), [])
    assert opened == [("https://my.mts-link.ru/j/1/5", True)]
    assert window.db.get_resolved_link("live") == "https://my.mts-link.ru/j/1/5"


def test_an_older_room_never_pulls_the_pair_back(window, monkeypatch):
    """A closed room's row came back after its rejection lapsed: stay in the live one."""
    lesson = _running_lesson("live")
    _active(window, lesson, "https://my.mts-link.ru/j/1/5")
    window.room_webinar_ids["https://my.mts-link.ru/j/1/5"] = 10
    opened = []
    monkeypatch.setattr(window, "_open_lecture", lambda *args, **kwargs: opened.append(args))

    window._source_resolved(lesson, _webinar("https://my.mts-link.ru/j/1/2", 9), [])

    assert opened == []


def _pair(external_id, start, subject="Надёжность"):
    return Lesson(external_id, subject, "ЛК", start, start + timedelta(minutes=90))


def test_the_previous_pair_never_takes_the_tab_of_the_next_one(window, monkeypatch):
    """A closed at the bell, B opened, then A's dead room came back on top of B."""
    now = datetime.now().astimezone()
    a = _pair("A", now - timedelta(minutes=96), subject="Физика")  # ended 6 min ago
    b = _pair("B", now + timedelta(minutes=4), subject="Химия")  # starts in 4 min
    window.db.sync_lessons([a, b], now.replace(hour=0, minute=0, second=0, microsecond=0))
    window.db.set_rule("Физика", RuleMode.AUTO)
    window.db.set_resolved_link("A", "https://my.mts-link.ru/j/a")
    window.active_lecture_id = "B"
    window.active_lecture_url = "https://my.mts-link.ru/j/b"
    window.joined_lessons.add("B")
    runs = []
    monkeypatch.setattr(window, "_run", lambda *args, **kwargs: runs.append(args))
    monkeypatch.setattr(window, "_resolve_from_sources", lambda lesson: None)

    window._evaluate_current_lessons(window.db.list_lessons())
    window._room_found(a, "https://my.mts-link.ru/j/a")

    assert runs == []  # nothing navigated B's tab to A's room


def test_a_large_lead_does_not_leave_the_running_pair_early(window, monkeypatch):
    now = datetime.now().astimezone()
    a = _pair("A", now - timedelta(minutes=88), subject="Физика")  # ends in 2 min
    b = _pair("B", now + timedelta(minutes=8), subject="Химия")
    window.join_before.setValue(15)
    window.db.sync_lessons([a, b], now.replace(hour=0, minute=0, second=0, microsecond=0))
    window.db.set_rule("Химия", RuleMode.AUTO)
    window.db.set_resolved_link("B", "https://my.mts-link.ru/j/b")
    window.active_lecture_id = "A"
    window.active_lecture_url = "https://my.mts-link.ru/j/a"
    opened = []
    monkeypatch.setattr(window, "_open_lecture", lambda url, lesson_id: opened.append(lesson_id))
    monkeypatch.setattr(window, "_resolve_from_sources", lambda lesson: None)

    window._evaluate_current_lessons(window.db.list_lessons())

    assert opened == []  # A keeps the tab until its end + 5 min


def test_a_double_pair_in_one_room_scans_the_second_pair_too(window, monkeypatch):
    now = datetime.now().astimezone()
    first = _pair("first", now - timedelta(minutes=100))
    second = _pair("second", now - timedelta(minutes=1))
    window.db.sync_lessons([first, second], now.replace(hour=0, minute=0, second=0, microsecond=0))
    window.db.set_rule("Надёжность", RuleMode.AUTO)
    window.db.set_setting("marked_lessons", ["first"])
    window.active_lecture_id = "first"
    window.active_lecture_url = "https://my.mts-link.ru/j/room"
    monkeypatch.setattr(window, "_resolve_from_sources", lambda lesson: None)
    monkeypatch.setattr(window, "_scan_tick", lambda: None)

    window._evaluate_current_lessons(window.db.list_lessons())

    assert window.active_lecture_id == "second"
    assert window.db.get_resolved_link("second") == "https://my.mts-link.ru/j/room"
    assert window.scan_timer.isActive()
    window.scan_timer.stop()


def test_a_lookup_started_before_the_rejection_does_not_reopen_the_room(window, monkeypatch):
    lesson = _running_lesson("lost")
    today = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    window.db.sync_lessons([lesson], today)
    window.db.set_rule(lesson.subject_name, RuleMode.AUTO)
    window._reject_room("lost", "https://my.mts-link.ru/j/dead")
    opened = []
    monkeypatch.setattr(window, "_open_lecture", lambda *args, **kwargs: opened.append(args))

    window._source_resolved(lesson, _webinar("https://my.mts-link.ru/j/dead"), [])

    assert opened == []


def test_an_automatic_open_is_refused_after_the_pair(window):
    now = datetime.now().astimezone()
    over = _pair("over", now - timedelta(minutes=100))
    window.db.sync_lessons([over], now.replace(hour=0, minute=0, second=0, microsecond=0))

    assert window._may_open("https://my.mts-link.ru/j/x", "over") is False


def test_five_quick_rejections_do_not_post_to_the_public_chat(window, monkeypatch):
    """Rotating codes fail five times in twenty seconds; the next one often works."""
    window.active_lecture_id = "lesson"
    sent = []
    monkeypatch.setattr(window, "_send_chat_fallback", lambda *args: sent.append(args))

    for number in range(5):
        event_id = window.db.add_qr_event(f"fingerprint-{number}", "detected", lesson_id="lesson")
        window.pending_qr[event_id] = PendingAttendance(
            raw_data=f"qr-{number}",
            lesson_id="lesson",
            lecture_url=None,
            detected_at=datetime.now().astimezone(),
        )
        window._record_attendance_failure(event_id)

    assert sent == []


def test_an_authenticator_code_is_asked_for_instead_of_waiting_for_email(window, monkeypatch):
    from mirea_lecture_assistant.email_otp import EmailAccount

    window.pending_email_credentials = EmailAccount("student@mail.ru", "app-password")
    asked, waited = [], []
    monkeypatch.setattr(window, "_manual_2fa", lambda challenge, reason="": asked.append(reason))
    monkeypatch.setattr(window, "_run", lambda *args, **kwargs: waited.append(args))

    challenge = SimpleNamespace(kind="otp", field_name="otp", hidden_fields={})
    window._login_finished(SimpleNamespace(challenge=challenge, success=False, message=""))

    assert waited == [] and len(asked) == 1


def test_a_pulse_login_waits_while_the_sdo_sign_in_holds_the_mailbox(window, monkeypatch):
    from mirea_lecture_assistant import ui

    timers, runs = [], []
    monkeypatch.setattr(ui.QTimer, "singleShot", lambda delay, callback: timers.append(delay))
    monkeypatch.setattr(window, "_run", lambda *args, **kwargs: runs.append(args))

    with window.sdo_sign_in_lock:
        window._run_initial_login("user", "password", "Вход…")

    assert runs == [] and timers == [30_000]
    assert not window.login_in_progress


def test_the_sdo_sign_in_waits_while_a_pulse_login_awaits_its_code(window):
    window.login_in_progress = True
    assert window._sign_in_to_sdo_locked() is False


def test_the_login_button_does_nothing_while_a_login_runs(window, monkeypatch):
    from mirea_lecture_assistant import ui

    window.login_in_progress = True
    dialogs = []
    monkeypatch.setattr(ui, "LoginDialog", lambda *args: dialogs.append(args))

    window.login()

    assert dialogs == []


def test_the_theme_can_be_switched_and_is_remembered(window):
    from mirea_lecture_assistant.ui import THEMES

    window.theme_choice.setCurrentIndex(window.theme_choice.findData("dark"))
    assert window.db.get_setting("theme") == "dark"
    assert window.colors is THEMES["dark"]
    assert THEMES["dark"]["bg"] in window.styleSheet()

    window.theme_choice.setCurrentIndex(window.theme_choice.findData("light"))
    assert window.colors is THEMES["light"]
    assert THEMES["light"]["bg"] in window.styleSheet()


def test_both_themes_define_every_colour_the_stylesheet_uses():
    from mirea_lecture_assistant.ui import STYLE, THEMES

    for name, tokens in THEMES.items():
        STYLE.substitute(tokens)  # raises KeyError on a missing token
        assert tokens.keys() == THEMES["light"].keys(), name


@pytest.mark.parametrize(
    ("challenge", "uses_email"),
    [
        (SimpleNamespace(kind="email_code", field_name="emailCode", hidden_fields={}), True),
        # pymirea's classic-form fallback labels an email form "otp" too.
        (SimpleNamespace(kind="otp", field_name="otp", hidden_fields={"session_code": "x"}), True),
        (SimpleNamespace(kind="otp", field_name="otp", hidden_fields={}), False),  # authenticator
        (SimpleNamespace(kind="otp", field_name="code", hidden_fields={"login": "true"}), False),
    ],
)
def test_the_email_wait_is_skipped_only_for_known_non_email_codes(window, challenge, uses_email):
    assert window._challenge_uses_email(challenge) is uses_email


def test_tokens_renewed_during_a_schedule_refresh_are_saved_once(window, monkeypatch):
    """Unsaved renewals made the next start use spent tokens: a new login and code."""
    saved = []
    monkeypatch.setattr(window.session_store, "save", lambda session: saved.append(dict(session)))
    window.mirea.session = {"access_token": "old"}
    window.persisted_session = window._session_fingerprint({"access_token": "old"})

    window._schedule_loaded([])
    assert saved == []  # nothing changed, nothing written

    window.mirea.session["access_token"] = "renewed"
    window._schedule_loaded([])
    window._schedule_loaded([])
    assert saved == [{"access_token": "renewed"}]


def test_an_inconclusive_session_check_still_loads_the_schedule(window, monkeypatch):
    window.mirea.session = {"cookie": "kept"}
    started = []
    monkeypatch.setattr(window, "_refresh_schedule_background", lambda: started.append(True))
    monkeypatch.setattr("mirea_lecture_assistant.ui.QTimer.singleShot", lambda *_args: None)

    window._session_verified(SessionState.UNKNOWN)

    assert started == [True]
    assert window.mirea.session == {"cookie": "kept"}


def test_a_second_launch_brings_the_hidden_window_back(window):
    window.hide()
    window.show_request.write_text("123", encoding="ascii")

    window._check_show_request()

    assert window.isVisible()
    assert not window.show_request.exists()


def test_a_second_launch_learns_whether_the_running_copy_answered(tmp_path):
    import threading

    from mirea_lecture_assistant.app import ask_running_copy_to_show
    from mirea_lecture_assistant.paths import SHOW_REQUEST_FILE

    # Nobody picks the request up: a hung copy, reported to the user.
    assert ask_running_copy_to_show(tmp_path, wait_seconds=0.3) is False
    assert not (tmp_path / SHOW_REQUEST_FILE).exists()

    def running_copy():
        request = tmp_path / SHOW_REQUEST_FILE
        for _ in range(50):
            if request.exists():
                request.unlink()
                return
            threading.Event().wait(0.02)

    responder = threading.Thread(target=running_copy)
    responder.start()
    assert ask_running_copy_to_show(tmp_path, wait_seconds=2) is True
    responder.join()
