# Update cleanup 0.2.16 (base: v0.2.15)

Observed 2026-10-01: 0.2.15 started, verified the saved session and loaded 37
lessons. Its installed SHA256 matched the official release. The old 0.2.13
supervisor errored in run -> _log -> lazy import paths, with PYZ zlib incorrect
header check. Two old processes (started 18:08) kept the .old file locked while
the new process tree (started 19:42) was healthy.

Fix: import data_dir eagerly in supervisor, before entering the child wait.
No paths import from the replaced executable is attempted when logging exit.
This prevents the observed old-archive-offset/new-file import failure in future
updates from this version. Already-running old binaries cannot be patched in RAM.

Cleanup starts one second after normal frozen startup and repeats every five
seconds while .old is locked. It is independent of auto-update preferences and
network access. Only the exact sibling <current-exe>.old is removed; current exe,
settings, credentials, browser profiles, .download and .new are untouched.
Cleanup stops after successful removal. No process killing is automated by cleanup.

Tests cover locked/released files, active-download preservation, cleanup timers
when update checking is disabled and no late paths import in supervisor logging.
Release publication/push are separate actions; local changes preserve v0.2.14/.15
fixes (sleep/resume, async health and late QR association protections).

Verification: 413 tests passed, Ruff/diff checks passed; frozen smoke exited 0
with HTTPS 200. Installed 0.2.16 on 2026-10-01 at 19:49 Moscow. Live logs:
stored_session_verified=valid, schedule_loaded=37, update_old_version_removed.
The old .exe.old disappeared automatically (not manually deleted). The failed
old supervisor was retired by its verified PID/start time before replacement;
the healthy new tree was not killed. The temporary 0.2.15 rollback copy was
sent to the Recycle Bin after successful startup. Delivery branch:
codex/reliability-0.2.11; master merge and release publication are separate actions.

Installed SHA256: 36853E53394EBAE8684B518F248C316529C5AF1DCE25E385516D8F52B794372D.
