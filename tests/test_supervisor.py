from __future__ import annotations

from mirea_lecture_assistant import supervisor

ACCESS_VIOLATION = 0xC0000005


def _run(codes):
    calls = []
    clock = [0.0]

    def call(command, env):
        calls.append(env.get(supervisor.CHILD_ENV))
        # Always unpacked into a folder of its own, never one borrowed.
        assert env[supervisor.RESET_ENV] == "1"
        return codes.pop(0)

    def sleep(seconds):
        clock[0] += seconds

    code = supervisor.run(
        ["app.exe", "--autostart"], call=call, sleep=sleep, clock=lambda: clock[0]
    )
    return code, calls


def test_a_crashed_app_is_started_again():
    code, calls = _run([ACCESS_VIOLATION, supervisor.CRASHED, 0])

    assert code == 0
    assert calls == ["1", "1", "1"]  # every run is a child of the watchdog


def test_a_deliberate_end_is_final():
    # «Выход» / handover (0), a failed start (3), the Task Manager (1),
    # being closed by a newer copy (15): none is started again.
    for code in (0, supervisor.STARTUP_FAILED, 1, 15):
        assert _run([code]) == (code, ["1"])


def test_it_gives_up_after_a_few_crashes_in_a_row():
    code, calls = _run([ACCESS_VIOLATION] * 10)

    assert code == ACCESS_VIOLATION
    assert len(calls) == supervisor.MAX_RESTARTS + 1


def test_only_the_built_program_outside_tests_is_watched(monkeypatch):
    monkeypatch.setattr(supervisor.sys, "frozen", True, raising=False)
    monkeypatch.setattr(supervisor.sys, "platform", "win32")

    assert supervisor.should_supervise({})
    assert not supervisor.should_supervise({supervisor.CHILD_ENV: "1"})
    assert not supervisor.should_supervise({"MIREA_ASSISTANT_SMOKE_TEST": "1"})


def test_an_update_starts_a_separate_copy_with_its_own_watchdog(monkeypatch):
    monkeypatch.setenv(supervisor.CHILD_ENV, "1")

    environment = supervisor.child_environment()
    assert supervisor.CHILD_ENV not in environment
    # Without this the updated copy ran on the old copy's temporary folder and
    # lost its certificates when the old copy quit: "[Errno 2] No such file".
    assert environment[supervisor.RESET_ENV] == "1"
