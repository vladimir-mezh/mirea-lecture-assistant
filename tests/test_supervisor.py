from __future__ import annotations

from mirea_lecture_assistant import supervisor

ACCESS_VIOLATION = 0xC0000005


def test_watchdog_exit_logging_never_lazily_imports_the_replaced_archive(tmp_path, monkeypatch):
    import builtins

    original = builtins.__import__

    def guarded(name, *args, **kwargs):
        if name == "paths" or name.endswith(".paths"):
            raise RuntimeError("attempted import from replaced archive")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(supervisor, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(builtins, "__import__", guarded)
    supervisor._log("app_ended code=0")
    assert "app_ended code=0" in (tmp_path / "logs" / "supervisor.log").read_text()


def test_hung_child_is_terminated_as_its_own_tree(monkeypatch):
    clock = [0.0]
    killed = []

    class Process:
        pid = 12345

        def poll(self):
            return None

        def wait(self, timeout):
            if killed:
                return 1
            clock[0] += timeout  # the watchdog's own five-second poll
            raise supervisor.subprocess.TimeoutExpired("app", timeout)

    monkeypatch.setattr(supervisor.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(supervisor.subprocess, "Popen", lambda *_a, **_kw: Process())
    monkeypatch.setattr(supervisor.subprocess, "run", lambda command, **_kw: killed.append(command))
    monkeypatch.setattr(supervisor.sys, "platform", "win32")
    monkeypatch.setattr(supervisor, "_log", lambda *_args: None)
    assert supervisor.monitored_call(["app.exe"], {}) == supervisor.CRASHED
    assert killed == [["taskkill", "/PID", "12345", "/T", "/F"]]
    # Never before the startup grace ran out.
    assert clock[0] > supervisor.STARTUP_GRACE_SECONDS


def test_a_healthy_child_can_exit_without_a_forced_restart(monkeypatch):
    class Process:
        returncode = 0

        def poll(self):
            return 0

    monkeypatch.setattr(supervisor.subprocess, "Popen", lambda *_a, **_kw: Process())
    assert supervisor.monitored_call(["app.exe"], {}) == 0


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


def test_repeated_crashes_cool_down_but_do_not_abandon_the_app():
    code, calls = _run([ACCESS_VIOLATION] * 10 + [0])

    assert code == 0
    assert len(calls) == 11


def test_only_the_built_program_outside_tests_is_watched(monkeypatch):
    monkeypatch.setattr(supervisor.sys, "frozen", True, raising=False)
    monkeypatch.setattr(supervisor.sys, "platform", "win32")

    assert supervisor.should_supervise({})
    assert not supervisor.should_supervise({supervisor.CHILD_ENV: "1"})
    assert not supervisor.should_supervise({"MIREA_ASSISTANT_SMOKE_TEST": "1"})


def test_an_update_starts_a_separate_copy_with_its_own_watchdog(monkeypatch):
    monkeypatch.setenv(supervisor.CHILD_ENV, "1")
    monkeypatch.setenv(supervisor.RESET_ENV, "0")

    environment = supervisor.child_environment()
    assert supervisor.CHILD_ENV not in environment
    # Without this the updated copy ran on the old copy's temporary folder and
    # lost its certificates when the old copy quit: "[Errno 2] No such file".
    assert environment[supervisor.RESET_ENV] == "1"
    # The running copy's own environment is left as it was.
    assert supervisor.os.environ[supervisor.CHILD_ENV] == "1"
    assert supervisor.os.environ[supervisor.RESET_ENV] == "0"


def test_a_computer_waking_from_sleep_does_not_kill_the_app(monkeypatch, tmp_path):
    """The heartbeat stops while the computer sleeps; the app beats again on waking."""
    ticks = iter([0, 5, 3_600, 3_605, 3_610])
    killed = []

    class Process:
        pid = 12345
        returncode = 0

        def __init__(self):
            self.polls = 0

        def poll(self):
            self.polls += 1
            return None if self.polls <= 4 else 0

        def wait(self, timeout):
            raise supervisor.subprocess.TimeoutExpired("app", timeout)

    monkeypatch.setattr(supervisor.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(supervisor.subprocess, "Popen", lambda *_a, **_kw: Process())
    monkeypatch.setattr(supervisor.subprocess, "run", lambda command, **_kw: killed.append(command))
    monkeypatch.setattr(supervisor, "_log", lambda *_args: None)

    assert supervisor.monitored_call(["app.exe"], {}) == 0
    assert killed == []
