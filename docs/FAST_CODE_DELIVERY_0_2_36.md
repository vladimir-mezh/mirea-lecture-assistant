# 0.2.36 — fast, form-bound email code delivery

The observed 0.2.34 trace showed a letter found, then offered to the extension in
1 ms, followed by clipboard fallback eight seconds later without a fill
acknowledgement. It does not prove that the mail provider delivered the letter
quickly; it identifies a failure to complete the application→extension leg.

## Changes

- Extension 1.4 starts watching during document startup. DOM mutations (including
  changed challenge text), form interactions and a lightweight heartbeat detect
  the email-code form. The existing IMAP IDLE listener remains unchanged: there
  is no extra mailbox polling or repeated account login.
- Re-injecting the content script cleans up its previous listener/observer.
  Each tab has one current worker session. New challenge nonces, tab navigation,
  form disappearance and tab closure cancel obsolete long polls.
- A short `/watch` request registers readiness before the long-held `/poll`.
  Failed local requests retry with bounded 100–1000 ms backoff and fresh pairing
  configuration. Heartbeats can restart an unloaded worker without duplicating
  waiters. Capacity refusal backs off rather than entering a busy loop.
- Server cancellation wakes and terminates a waiting poll; it cannot silently
  re-register itself. A code may go to a single explicitly matching challenge
  even when other, differently marked forms are open. Two forms matching the
  same challenge remain ambiguous and are not arbitrarily chosen. Compatibility
  fallback for a single unmarked form remains as before.
- The page rechecks its current challenge at fill time, not just at observation
  time. Delivery is restricted to the explicit MIREA email-code form, top frame,
  original tab and current nonce; no focus changes or keyboard injection occur.
- Lost acknowledgements are retried, not the actual input. Server acknowledgements
  are idempotent. Codes still expire after 90 seconds; credentials, own-app login
  codes and chat fields are not exposed or affected.
- Clipboard fallback now waits three seconds instead of eight. This fallback is
  not itself an automatic browser input and is not the primary speed fix.

## Diagnostics

Existing `manual_code_letter_found` and `manual_code_offered` events are joined by
`manual_code_watch_started waiting_pages=N` and
`manual_code_fill_confirmed elapsed_ms=N`. No code, receipt, pairing key, challenge
tag or page URL is written by these events.

## Validation boundary

Tests cover retry, reinjection, stale challenges, multiple forms, closing a tab,
duplicate heartbeats, lost acknowledgements, safe target selection and actual
local HTTP long-poll wakeup. The isolated HTTP check measured **0.6 ms** from
synthetic publication to receipt on this machine. This is not a measurement of
mail delivery, a real Chrome input, SSO server confirmation or full user login.
JavaScript worker/page tests execute the real scripts with mocked browser APIs.
Validation: **574 Python tests and 17 JavaScript tests passed**. The packaged
0.2.36 executable opened its main window and exited normally in an isolated profile.
The next real login must be initiated by the user, not by the test suite.

Local deployment on 26 October: the previous executable and database files were
preserved under the real profile's `backups/before-0.2.36-20261026`. Version 0.2.36
started on that profile, verified the saved session, loaded 37 lessons and
connected the mail watcher with IDLE. Extension files on disk are 1.4.0; this
does not prove the running browser has reloaded the extension or a live code
has been filled. No real login was initiated for testing.

The extension folder is refreshed when the app starts. Its existing safe
self-update waits until MIREA SSO tabs are closed and checks once a minute; it
does not reload an extension in the middle of someone's login. Close an old
SSO tab and open a fresh login after the extension has reloaded when validating
an upgrade from 1.3.

This branch is based on `codex/reliability-and-daily-summary` and includes the
previous 0.2.35 fixes. No application GitHub release is created by this change.
