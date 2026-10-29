# 0.2.35 — reliability and daily evidence

Branch: `codex/reliability-and-daily-summary`, based on release 0.2.34.

## Task Scheduler recovery

The Plan B signature is persisted only when task creation/deletion returns success. Failed operations retry after 30 seconds, without overlapping workers. Settings changed during an operation are reconciled after its completion. The old successful signature is preserved on failure.

## Guarded updates

Updates initiated by **0.2.35 or newer** retain `.exe.old` and a pending manifest until the new UI has initialized its database/window and processed its 10-second startup callback. Internet and a logged-in session are deliberately not prerequisites for readiness. Older versions cannot retroactively gain this protection when initiating their first update.

A separate, temporary Windows PowerShell guard runs outside both PyInstaller archives. It checks both executable hashes and starts the replacement with a fresh unpacking environment. This permits recovery even when the new executable cannot unpack Python. On startup failure or failure to confirm within 240 seconds, only the guard's own child process tree is stopped, the old executable is restored and started. The failed build is retained as `.exe.failed-<transaction>`. An intentional exit with status zero does not trigger a restart; a later successful ordinary launch confirms the pending version.

The guard writes event names only to `<exe>.update.log`. Normal startup records `update_startup_verified` in app.log. Old-file cleanup remains blocked until confirmation, then the application's existing cleanup retry removes `.exe.old`. Changed hashes or unrecoverable filesystem errors require manual recovery; the backup is not silently discarded. The helper needs Windows PowerShell; if process creation fails, the backup and pending manifest remain for recovery.

The regular watchdog remains responsible for crashes after successful startup; this is not a rollback mechanism for every future application error or server outage. Database backups remain separate from executable rollback.

## Capture evidence

`last_capture_at` now means a received, processed frame only. Scanner start and soft recovery update a separate bounded grace timer, never the actual frame timestamp. Health responses return null when no frame was received. Results captured for a previous lesson/room are discarded instead of being credited to the next lesson. Existing recovery throttling and overrun handling remain unchanged.

## Daily summary

History has a calendar and a separate summary table: scheduled lesson, page opened, room confirmed live by the browser health check, actual video frame, attendance confirmed. These are independent facts. A page screenshot alone does not prove video. No data means unknown, not absent. A submitted attendance event or persisted marked-lesson flag counts as an attendance confirmation.

Activity snapshots survive application restart and schedule changes in database schema v4 and are included in damaged-database salvage. They contain lesson metadata and timestamps only: no QR contents, passwords, tokens or screenshots. First video and periodic video timestamps are persisted, not every scan frame. No old history is fabricated or retroactively inferred from logs. MCP's existing attendance-history response includes today's `daily_summary`, without requiring an adapter update.

## Validation scope

Full-suite testing exposed intermittent Windows `PermissionError` during MCP staging-directory activation. Activation now retries that specific error for at most two seconds, leaving the selected manifest untouched until success. A deterministic transient-lock regression test covers this path.

Automated checks use isolated databases and synthetic lessons. Windows guard integration uses compiled tiny fixture executables, testing a ready replacement and a startup failure with actual rollback. No live lecture, attendance submission, chat send, account logout or real profile replacement is required by these checks. Building a candidate does not install it or publish a GitHub release.

Results on Windows 11: **566 pytest tests passed**, Ruff passed, packaged 0.2.35 built successfully and opened its main window in an isolated profile, then exited with status 0. The history layout was rendered and visually checked with synthetic lessons.

The independent-bundle/HTTPS and legacy-launch probes passed. The in-place A→B packaged-executable probe also passed: A's watchdog returned status 0, B ran, and A's `.old` file was removed after confirmation. The probe intentionally attempts an unsafe late import from the replaced A archive and records the expected decompression error; this demonstrates why real shutdown must not lazily import from that archive, not a production startup failure.

That packaged probe exposed a Windows-specific bootstrap issue: detached PowerShell could terminate before reading the guard script when inherited console handles were invalid. The guard now uses `CREATE_NO_WINDOW` and explicit null standard handles, covered by a regression test; regular executable launch retains its previous process flags.
