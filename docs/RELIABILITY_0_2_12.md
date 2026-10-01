# Reliability changes, local 0.2.12

Base: GitHub tag v0.2.11 (cca7ab7). Branch: codex/reliability-0.2.11.

## Invariants

Attendance and public-chat rules are unchanged. Never replay saved QR payloads,
never generate test attendance/chat traffic against real accounts. Authentication
still goes through the real MIREA challenge and stored OS-protected credentials.
The persisted authentication limiter (5 starts / 30 minutes) remains authoritative.

## Recovery

- Transient pre-code failures retry after 2, 5, then 15 minutes indefinitely.
  Email wait and code transport exceptions enter the same retry path. A returned
  rejection after OTP submission retries only for explicit network/server outages;
  actual rejected codes and permanent credential errors stop. A cooldown schedules
  another attempt rather than silently dropping recovery. Retry generations prevent
  older timers from starting a flow early. Each attempt fetches a fresh email baseline.
- The frozen Windows supervisor launches only its own monitored child with an
  isolated heartbeat file. Qt updates it once per second, unless the shared async
  loop has stopped responding for 60 seconds or a scan has been stuck for 60 seconds.
  Missing startup heartbeat: 180-second grace. Stale heartbeat: 120-second timeout.
  Recovery kills only that child process tree, releasing the singleton lock.
  After three crashes within ten minutes, wait five minutes and keep trying.
  Deliberate exits/startup errors retain their existing behavior.
- Screenshot budget: 3 seconds; total browser capture: 4.5 seconds. A screenshot
  timeout degrades 1920x1080 to 1280x720 for 60 seconds, then retries HD. This is
  a sampling budget, not a hard guarantee of successful decoding every five seconds.
  The one-minute fallback can lose QR detail; measure with real lecture streams.
  Direct-capture mode never silently changes to whole-desktop capture on disconnect;
  the browser operation itself validates the dedicated connection, without an extra
  blocking HTTP liveness probe before every frame.
- A visible srcObject video's media clock/frame counter is sampled. No progress
  for 45 seconds yields an unstable state; two health checks trigger recovery.
  Static slides are not detected by image hashes. Unsupported players/iframes
  without an observable stream rely on existing DOM and capture checks. Advancing
  counters cannot prove that the remote lecturer is publishing a fresh image.
  No successful capture for 30 seconds during scanning also triggers tab recovery.
- Save the active lesson ID, room URL and timetable snapshot (no QR/token).
  The snapshot permits recovery while Pulse temporarily omits the active pair.
  Resume after startup when still
  within scheduled start/end+2 hours, not ignored, not superseded by a newer pair.
  Restore the active ID before opening so the normal past-pair guard permits the
  overrun. Finishing monitoring clears the checkpoint. Cached QR is never restored.

## Diagnostics and testing

app.log: automatic_login_retry_scheduled, lecture_resume_requested,
lecture_capture_stale, capture_resolution_degraded, lecture_media_stalled.
supervisor.log: app_unresponsive, restarting, cooling_down.

Regression tests use temporary databases, fake credentials/processes and media
samples. Do not kill Chrome or disconnect the real network to run these tests.
Windows sleep, power-off and loss of all network connectivity still prevent capture;
restoration can resume work but cannot recover a QR already missed.

## Verification (2026-10-01)

- 393 pytest tests passed, including the isolated real-Chrome integration suite.
- Ruff and git diff --check passed.
- PyInstaller build completed; isolated populated-profile smoke test exited 0,
  main_window_ready and smoke_https_ok HTTP 200 were observed. Real authentication
  was disabled in the smoke profile.
- Installed locally at the user's Downloads folder.
  SHA256: D161D5D48A642E1EB6EF657631035FCBE498B7B96A2975C3468B5E0592FD513A.
- Live startup logs: version=0.2.12, stored_session_verified state=valid,
  schedule_loaded lesson_count=37; supervisor heartbeat updated every second.
- No live QR was submitted as a test; there was no ongoing scheduled lecture at
  installation time. Production stream performance and true ISP outages were not
  deliberately induced. Delivery is via codex/reliability-0.2.11 only; publishing
  a GitHub release or merging master is a separate action.
