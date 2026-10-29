from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from mirea_lecture_assistant import mcp_install, plan_b, updater
from mirea_lecture_assistant.database import Database
from mirea_lecture_assistant.domain import Lesson

pytest_plugins = ["test_ui_reliability"]


def pair(identifier="today"):
    now = datetime.now().astimezone()
    return Lesson(
        identifier,
        "Тестовый предмет",
        "ЛК",
        now - timedelta(minutes=10),
        now + timedelta(minutes=80),
    )


def test_plan_b_failure_retries_without_persisting_success(window, monkeypatch):
    from mirea_lecture_assistant import ui

    monkeypatch.delenv("MIREA_ASSISTANT_SMOKE_TEST", raising=False)
    monkeypatch.setattr(plan_b, "available", lambda: True)
    monkeypatch.setattr(window, "_plan_b_times", lambda: [datetime(2030, 1, 1, tzinfo=UTC)])
    monkeypatch.setattr(updater, "current_executable", lambda: Path("C:/test/app.exe"))
    callbacks, retries = [], []
    monkeypatch.setattr(window, "_run", lambda work, done, *_a, **_k: callbacks.append(done))
    monkeypatch.setattr(
        ui.QTimer, "singleShot", lambda delay, callback: retries.append((delay, callback))
    )
    window._sync_plan_b()
    callbacks.pop()(False)
    assert window.db.get_setting("plan_b_signature") is None
    assert retries[0][0] == 30_000
    retries.pop()[1]()
    callbacks.pop()(True)
    assert window.db.get_setting("plan_b_signature") is not None
    assert not window.plan_b_running


def test_scanner_start_and_recovery_never_invent_a_received_frame(window, monkeypatch):
    monkeypatch.setattr(window, "_scan_tick", lambda: None)
    window.last_capture_at = 0.0
    window.toggle_scanner()
    assert window.last_capture_at == 0.0
    assert window.capture_grace_at > 0
    assert window._soft_recovery_due()
    assert window.last_capture_at == 0.0


def test_a_frame_from_another_lesson_does_not_refresh_capture(window, monkeypatch):
    monkeypatch.setattr(window, "_continue_scan_without_gap", lambda: None)
    window.active_lecture_id = "new"
    window.last_capture_at = 0
    window._scan_results(None, ("old", None))
    assert window.last_capture_at == 0


def test_daily_evidence_survives_restart_without_inventing_attendance(tmp_path):
    db = Database(tmp_path / "test.sqlite3")
    lesson = pair()
    db.sync_lessons([lesson], keep_from=lesson.start_at - timedelta(days=1))
    db.record_lesson_activity(lesson, "opened")
    row = db.daily_summary(lesson.start_at.date())[0]
    assert row["opened_at"] and not row["joined_at"] and not row["video_at"] and not row["marked"]
    db.record_lesson_activity(lesson, "joined")
    db.record_lesson_activity(lesson, "video")
    db.close()
    db = Database(tmp_path / "test.sqlite3")
    row = db.daily_summary(lesson.start_at.date())[0]
    assert row["joined_at"] and row["video_at"] and not row["marked"]
    db.set_setting("marked_lessons", [lesson.external_id])
    assert db.daily_summary(lesson.start_at.date())[0]["marked"]
    assert not db.daily_summary(lesson.start_at.date() - timedelta(days=1))


def test_summary_page_has_separate_evidence_columns(window):
    lesson = pair()
    window.db.sync_lessons([lesson], keep_from=lesson.start_at - timedelta(days=1))
    window.db.record_lesson_activity(lesson, "opened")
    window._fill_daily_summary()
    assert window.daily_summary.rowCount() == 1
    assert window.daily_summary.item(0, 2).text() != "Нет данных"
    assert window.daily_summary.item(0, 3).text() == "Нет данных"
    assert window.daily_summary.item(0, 5).text() == "Не подтверждено"


def test_update_readiness_needs_matching_build_and_nonce(tmp_path, monkeypatch):
    current, new = tmp_path / "app.exe", tmp_path / "new.exe"
    current.write_bytes(b"old")
    new.write_bytes(b"new")
    updater.install(new, current)
    assert not updater.clean_leftovers(current)
    monkeypatch.setattr(updater, "can_self_update", lambda: True)
    monkeypatch.setattr(updater, "current_executable", lambda: current)
    monkeypatch.setenv("MIREA_UPDATE_NONCE", "wrong")
    updater.confirm_ready()
    assert not (tmp_path / "app.exe.update-ready").exists()
    metadata = json.loads(updater.pending_path(current).read_text())
    monkeypatch.setenv("MIREA_UPDATE_NONCE", metadata["nonce"])
    updater.confirm_ready()
    assert (tmp_path / "app.exe.update-ready").read_text() == metadata["nonce"]
    assert (tmp_path / "app.exe.old").read_bytes() == b"old"


def test_mcp_activation_retries_a_transient_windows_lock(tmp_path, monkeypatch):
    staging, target = tmp_path / "staging", tmp_path / "version"
    staging.mkdir()
    original = Path.replace
    attempts = []

    def replace(path, destination):
        attempts.append(path)
        if len(attempts) < 3:
            raise PermissionError("temporary Windows lock")
        return original(path, destination)

    monkeypatch.setattr(Path, "replace", replace)
    monkeypatch.setattr(mcp_install.time, "sleep", lambda _delay: None)
    mcp_install._activate_directory(staging, target)
    assert target.exists() and len(attempts) == 3


@pytest.mark.skipif(sys.platform != "win32", reason="Windows process flags")
def test_guard_has_valid_handles_from_a_windowless_application(tmp_path, monkeypatch):
    current, new = tmp_path / "app.exe", tmp_path / "new.exe"
    current.write_bytes(b"old")
    new.write_bytes(b"new")
    updater.install(new, current)
    calls = []
    original = updater.tempfile.NamedTemporaryFile
    monkeypatch.setattr(
        updater.tempfile, "NamedTemporaryFile", lambda **kw: original(dir=tmp_path, **kw)
    )
    monkeypatch.setattr(
        updater.subprocess, "Popen", lambda command, **kw: calls.append((command, kw))
    )
    updater.start(current)
    command, options = calls[0]
    assert command[0].endswith("powershell.exe")
    assert options["creationflags"] & subprocess.CREATE_NO_WINDOW
    assert not options["creationflags"] & subprocess.DETACHED_PROCESS
    assert all(options[stream] == subprocess.DEVNULL for stream in ("stdin", "stdout", "stderr"))


@pytest.mark.skipif(sys.platform != "win32", reason="Windows guard integration")
@pytest.mark.parametrize("healthy", [True, False])
def test_real_windows_update_guard_commit_and_rollback(tmp_path, healthy):
    csc = (
        Path(os.environ.get("SystemRoot", "C:/Windows"))
        / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    )
    if not csc.exists():
        pytest.skip("Windows C# fixture compiler unavailable")
    old = tmp_path / "app.exe"
    new = tmp_path / "new.exe"
    old_source = tmp_path / "old.cs"
    old_source.write_text(
        'class Old { static void Main() { System.IO.File.WriteAllText(System.Reflection.Assembly.GetExecutingAssembly().Location+".old-started", "yes"); }}'
    )
    code = (
        "string p=System.Reflection.Assembly.GetExecutingAssembly().Location;"
        'System.IO.File.WriteAllText(p+".update-ready",System.Environment.GetEnvironmentVariable("MIREA_UPDATE_NONCE"));'
        "System.Threading.Thread.Sleep(3000);return 0;"
        if healthy
        else "return 3;"
    )
    new_source = tmp_path / "new.cs"
    new_source.write_text("class New { static int Main() {" + code + "} }")
    for source, exe in [(old_source, old), (new_source, new)]:
        subprocess.run(
            [str(csc), "/nologo", "/target:exe", f"/out:{exe}", str(source)],
            check=True,
            capture_output=True,
            timeout=30,
        )
    old_hash = updater._digest(old)
    updater.install(new, old)
    helper = tmp_path / "guard.ps1"
    shutil.copyfile(Path(__file__).parents[1] / "assets/update_guard.ps1", helper)
    result = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(helper),
            "-Executable",
            str(old),
            "-TimeoutSeconds",
            "10",
        ],
        capture_output=True,
        timeout=35,
        check=False,
    )
    guard_log = tmp_path / "app.exe.update.log"
    events = guard_log.read_text(encoding="utf-8-sig") if guard_log.exists() else "guard_log_missing"
    assert result.returncode == 0, events
    assert not updater.pending_path(old).exists()
    log = (tmp_path / "app.exe.update.log").read_text(encoding="utf-8-sig")
    assert ("update_committed" if healthy else "update_rolled_back") in log
    if not healthy:
        assert updater._digest(old) == old_hash
        assert list(tmp_path.glob("app.exe.failed-*"))
