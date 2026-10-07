from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from mirea_lecture_assistant import ai_duty, diagnostics, plan_b
from mirea_lecture_assistant.domain import Lesson, RuleMode
from mirea_lecture_assistant.mcp_access import ApiJob

pytest_plugins = ["test_ui_reliability"]


# --- diagnostics ------------------------------------------------------------


def test_reports_lose_everything_personal():
    text = (
        "Иванов Иван ИКБО-01-23 ivan@mail.ru вошёл, код 482915, "
        "ссылка https://my.mts-link.ru/j/123/456?token=abc, "
        "C:\\Users\\Ivan\\AppData\\Local\\x, ключ " + "a" * 40
    )
    clean = diagnostics.scrub(text, private=["Иванов Иван", "ИКБО-01-23", "Ivan"])
    for secret in ("Иванов", "ИКБО", "ivan@mail.ru", "482915", "token=abc", "aaaa", "\\Ivan\\"):
        assert secret not in clean
    assert "https://my.mts-link.ru/…" in clean


def test_recent_problems_are_event_names_without_values(tmp_path):
    log = tmp_path / "app.log"
    log.write_text(
        "2026-10-07 10:00:00.000 INFO MainThread app ok_event key=1\n"
        "2026-10-07 10:01:00.000 WARNING MainThread mirea_lecture_assistant.ui "
        "schedule_refresh_failed message=secret@mail.ru\n"
        "2026-10-07 10:02:00.000 ERROR Dummy-1 qt lecture_tab_lost lesson_id=7\n",
        encoding="utf-8",
    )
    found = diagnostics.recent_problems(log)
    assert [p["event"] for p in found] == ["lecture_tab_lost", "schedule_refresh_failed"]
    assert "secret" not in str(found)


@pytest.mark.parametrize(
    ("mirea_up", "internet", "vpn", "verdict"),
    [
        (True, True, ["AmneziaWG Tunnel | amn0"], "ok"),
        (False, True, ["AmneziaWG Tunnel | amn0"], "vpn_suspected"),
        (False, True, [], "mirea_unreachable"),
        (False, False, [], "offline"),
    ],
)
def test_network_verdicts(monkeypatch, mirea_up, internet, vpn, verdict):
    monkeypatch.setattr(
        diagnostics,
        "_reachable",
        lambda host, _t: mirea_up if host in diagnostics.MIREA_SITES else internet,
    )
    monkeypatch.setattr(diagnostics, "vpn_adapters", lambda: vpn)
    assert diagnostics.network_report()["verdict"] == verdict


def test_bypass_files_hold_mirea_and_mts_link(tmp_path):
    import json

    folder = diagnostics.write_bypass_files(tmp_path)
    entries = json.loads((folder / "mirea-bypass-amnezia.json").read_text(encoding="utf-8"))
    names = {entry["hostname"] for entry in entries}
    assert {"sso.mirea.ru", "my.mts-link.ru", "91.215.40.0/22", "37.130.193.0/24"} <= names
    assert "sso.mirea.ru" in (folder / "mirea-bypass.txt").read_text(encoding="utf-8")


# --- plan B -------------------------------------------------------------------


def test_plan_b_task_starts_the_program_before_each_pair():
    times = [
        datetime(2026, 10, 8, 8, 53, tzinfo=UTC).replace(tzinfo=None),
        datetime(2026, 10, 8, 10, 33, tzinfo=UTC).replace(tzinfo=None),
    ]
    xml = plan_b.task_xml(Path("C:/Program Files/Mirea & Co/MireaLectureAssistant.exe"), times)
    root = ET.fromstring(xml.split("\n", 1)[1])
    ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    starts = [e.text for e in root.findall(".//t:TimeTrigger/t:StartBoundary", ns)]
    assert starts == ["2026-10-08T08:53:00", "2026-10-08T10:33:00"]
    assert root.find(".//t:Exec/t:Arguments", ns).text == "--plan-b"
    assert "Mirea & Co" in root.find(".//t:Exec/t:Command", ns).text
    assert root.find(".//t:ExecutionTimeLimit", ns).text == "PT0S"  # never stopped mid-pair
    assert root.find(".//t:RunLevel", ns).text == "LeastPrivilege"


def test_plan_b_keeps_within_what_one_task_holds():
    start = datetime(2026, 10, 8, tzinfo=UTC).replace(tzinfo=None)
    times = [start + timedelta(hours=i) for i in range(80)]
    xml = plan_b.task_xml(Path("C:/a.exe"), times)
    assert xml.count("<TimeTrigger>") == plan_b.MAX_TRIGGERS


# --- the agent ------------------------------------------------------------------


def test_agents_get_the_task_on_stdin_and_no_shell_powers():
    codex = ai_duty.Agent("codex", "Codex", "C:/npm/codex.cmd").command()
    assert codex[-1] == "-" and "read-only" in codex
    claude = ai_duty.Agent("claude-code", "Claude Code", "C:/claude.exe").command()
    assert claude[claude.index("--allowedTools") + 1] == "mcp__mirea-lecture-assistant"
    assert "--dangerously-skip-permissions" not in claude


def test_agent_run_passes_the_task_and_keeps_its_output(tmp_path, monkeypatch):
    seen = {}

    class Done:
        returncode = 0

    def run(args, **kwargs):
        seen.update(kwargs, args=args)
        kwargs["stdout"].write("OK\n")
        return Done

    monkeypatch.setattr(ai_duty.subprocess, "run", run)
    agent = ai_duty.Agent("codex", "Codex", "codex")
    assert ai_duty.run(agent, tmp_path / "work", tmp_path / "logs", context="нет комнаты") == 0
    assert seen["input"].startswith(ai_duty.PROMPT) and "нет комнаты" in seen["input"]
    assert seen["cwd"] == tmp_path / "work"
    assert "OK" in next((tmp_path / "logs").glob("ai-duty-*.log")).read_text(encoding="utf-8")


def test_issue_is_filed_only_with_a_signed_in_github_cli(monkeypatch):
    monkeypatch.setattr(ai_duty.shutil, "which", lambda _n: None)
    assert ai_duty.post_issue("t", "b") is None
    monkeypatch.setattr(ai_duty.shutil, "which", lambda _n: "gh")
    calls = []

    class Result:
        def __init__(self, code, out=""):
            self.returncode, self.stdout = code, out

    def run(args, **kwargs):
        calls.append(args)
        if args[1:3] == ["auth", "status"]:
            return Result(0)
        return Result(0, "https://github.com/vladimir-mezh/mirea-lecture-assistant/issues/9\n")

    monkeypatch.setattr(ai_duty.subprocess, "run", run)
    assert ai_duty.post_issue("t", "b").endswith("/issues/9")
    assert calls[-1][:4] == ["gh", "issue", "create", "--repo"]
    assert "github.com/vladimir-mezh/mirea-lecture-assistant/issues/new?" in ai_duty.issue_url(
        "t", "b"
    )


# --- the window ---------------------------------------------------------------


def _pair_without_room(window, minutes_in=6):
    now = datetime.now().astimezone()
    lesson = Lesson(
        "duty", "Физика", "ЛК", now - timedelta(minutes=minutes_in), now + timedelta(minutes=60)
    )
    lesson.is_online = True
    window.db.sync_lessons([lesson], now - timedelta(days=1))
    window.db.set_rule("Физика", RuleMode.AUTO)
    window._set_auth_state("signed_in")
    return lesson


def _ask(window, method, params=None, timeout=10):
    from PySide6.QtWidgets import QApplication

    job = ApiJob(method, params or {})
    window._mcp_rpc(job)
    deadline = time.monotonic() + timeout
    while not job.done.is_set() and time.monotonic() < deadline:
        QApplication.processEvents()
        time.sleep(0.01)
    assert job.done.is_set()
    return job.result


def _enable(window):
    window.mcp_enabled.setChecked(True)
    window.mcp_access.server = object()


def test_health_names_the_problem_and_its_repair(window, monkeypatch):
    _pair_without_room(window)
    _enable(window)
    monkeypatch.setattr(diagnostics, "network_report", lambda timeout=5: {"verdict": "ok"})
    health = _ask(window, "check_health")["result"]
    assert health["verdict"] == "problem"
    assert health["problems"][0]["code"] == "not_in_room"
    assert health["problems"][0]["actions"][0] == "reopen_lecture"
    assert health["lesson"]["subject"] == "Физика"
    assert health["network"] == {"verdict": "ok"}


def test_no_pair_means_ok_and_no_network_check(window, monkeypatch):
    window._set_auth_state("signed_in")
    _enable(window)
    monkeypatch.setattr(diagnostics, "network_report", lambda **_: pytest.fail("not needed"))
    health = _ask(window, "check_health")["result"]
    assert health["verdict"] == "ok" and "network" not in health


def test_repair_needs_its_own_permission(window, monkeypatch):
    lesson = _pair_without_room(window)
    _enable(window)
    assert "Починка выключена" in _ask(window, "repair", {"action": "reopen_lecture"})["error"]
    window.mcp_allow_repair.setChecked(True)
    window.db.set_resolved_link(lesson.external_id, "https://my.mts-link.ru/j/1/2")
    opened = []
    monkeypatch.setattr(
        window, "_open_lecture", lambda url, lesson_id, force=False: opened.append(url)
    )
    assert _ask(window, "repair", {"action": "reopen_lecture"})["result"]["started"]
    assert opened == ["https://my.mts-link.ru/j/1/2"]
    assert "error" in _ask(window, "repair", {"action": "format_disk"})


def test_report_is_scrubbed_kept_and_filed(window, tmp_path, monkeypatch):
    from mirea_lecture_assistant import paths

    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    window.db.set_setting("student_name", "Иванов Иван")
    _enable(window)
    filed = []
    monkeypatch.setattr(
        ai_duty, "post_issue", lambda title, body: filed.append((title, body)) or "https://x"
    )
    result = _ask(
        window,
        "report_fix",
        {"summary": "Иванов Иван не был в комнате, переоткрыл её", "fixed": True},
    )
    assert result["result"]["saved"]
    deadline = time.monotonic() + 5
    while not filed and time.monotonic() < deadline:
        time.sleep(0.01)
    title, body = filed[0]
    assert "Иванов" not in title + body and title.startswith("ИИ-дежурный починил")
    assert list((tmp_path / "reports").glob("report-*.md"))


def test_duty_calls_the_agent_only_when_something_is_wrong(window, monkeypatch):
    from PySide6.QtWidgets import QApplication

    from mirea_lecture_assistant import ui

    started = []
    monkeypatch.setattr(ui.ai_duty, "run", lambda agent, *_a, **k: started.append(k) or 0)
    monkeypatch.setattr(diagnostics, "network_report", lambda timeout=5: {"verdict": "ok"})
    window.duty_agents = {"codex": ai_duty.Agent("codex", "Codex", "codex")}
    window.duty_agent.addItem("Codex", "codex")
    window.duty_enabled.blockSignals(True)
    window.duty_enabled.setChecked(True)
    window.duty_enabled.blockSignals(False)
    window._set_auth_state("signed_in")

    window._duty_tick()  # no pair: nothing at all
    assert not started

    _pair_without_room(window, minutes_in=6)
    window._duty_tick()
    deadline = time.monotonic() + 5
    while not started and time.monotonic() < deadline:
        QApplication.processEvents()
        time.sleep(0.01)
    assert started and "комнату" in started[0]["context"]
    window._duty_tick()  # the +5 mark is checked once
    assert len(started) == 1


def test_a_vpn_problem_is_explained_without_spending_on_an_agent(window, monkeypatch):
    from PySide6.QtWidgets import QApplication

    from mirea_lecture_assistant import ui

    monkeypatch.setattr(ui.ai_duty, "run", lambda *_a, **_k: pytest.fail("no agent for VPN"))
    monkeypatch.setattr(
        diagnostics, "network_report", lambda timeout=5: {"verdict": "vpn_suspected"}
    )
    shown = []
    monkeypatch.setattr(window.tray, "showMessage", lambda title, *_a: shown.append(title))
    window.duty_enabled.blockSignals(True)
    window.duty_enabled.setChecked(True)
    window.duty_enabled.blockSignals(False)
    _pair_without_room(window)
    window._duty_check(None, "+5")
    deadline = time.monotonic() + 5
    while not shown and time.monotonic() < deadline:
        QApplication.processEvents()
        time.sleep(0.01)
    assert shown == ["Похоже, мешает VPN"]
    assert window.tray_click == window._show_vpn_help


def test_mcp_page_explains_itself(window):
    assert window.page_info_buttons and window.page_info_buttons[0].text() == "ⓘ"
