# Browser reliability fixes — 0.2.26

Branch: `codex/fix-browser-reliability-0.2.26`. Base: `v0.2.25`.

## Changes

- Capture current decoded video pixels through canvas, avoiding the compositor
  that frequently timed out while Chrome was minimised. Capture up to four
  visible videos at native resolution, capped at 1920×1080 per video. A fresh
  screenshot remains the fallback when video capture is unavailable/tainted.
  Frames and QR values are not cached. Backend changes log `capture_backend`.
- Closing the last lecture tab closes the dedicated browser, rather than
  creating `about:blank`. Other nonblank tabs are preserved. Persistent Chrome
  cookies/profile stay on disk. After two checks, an empty event landing page
  after scheduled end + 5 minutes also finishes monitoring. Live media continues
  beyond the scheduled end; the existing safety timeout remains unchanged.
- Extension 1.1 fills an explicit SSO email-code field in its original tab,
  independent of window focus. No blind keyboard injection. A private,
  authenticated loopback bridge passes codes in memory with a 90-second expiry.
  Web origins are rejected; only paired extension origins can poll. Ambiguous
  multiple forms get clipboard fallback. App-owned codes remain excluded.
- MAX skipping watches delayed DOM/SPA updates and enabled controls, but only
  the optional `max-account-config` page's offered `skip=true` form.
- Disable password saving/leak notices only in the stopped dedicated Chrome
  profile; preserve unrelated preferences and Safe Browsing. During manual-code
  arrival, a bounded hidden Windows helper tries the precise native notice's
  Close/OK button in MIREA browser windows. No global protection changes, focus
  changes, or simulated Enter key.

## Verification (7 October 2026)

- Ruff: passed. Python suite: **485 passed**. Node extension suite: **4 passed**.
- Real isolated Chrome fixture: canvas-stream QR decoded without screenshot;
  after video pixels changed, the old QR was absent. Tests used artificial
  pages/tokens, not real lecture attendance or chats.
- Packaged executable smoke: HTTPS success, main window ready, exit code 0;
  no packaging/zlib error.
- Installed locally at the canonical Downloads executable, retaining a backup
  of 0.2.25 in the application profile's `backups` directory.
- Real-profile log: `application_start version=0.2.26`,
  `stored_session_verified state=valid`, `schedule_loaded lesson_count=37`,
  `manual_code_bridge_started`, Gmail `manual_code_watcher_connected idle=True`.
- The leftover window from the 18:00 lecture was separately verified as ended,
  with no active lesson in the database, and closed through the dedicated
  browser service. No attendance submissions or chat messages were made.

## Required user step / evidence limits

Reload the existing unpacked extension on `chrome://extensions` (or prepare its
folder using the button in Settings, then install/reload). The app refreshes
that folder and its private pairing configuration at startup. Do not publish
`bridge-config.json` from the installed profile; the repository template is empty.

Real background email-code arrival, a real MAX page, the native Chrome warning,
and a full future lecture in minimised Chrome have **not** been live-tested in
this run. Synthetic tests are not a guarantee of every provider/player variant.
Firefox is not supported by this Chromium extension bridge. Native warning
dismissal is best effort and remains optional to successful DOM code filling.

No AI-assistant tab or MCP work was performed: those messages were meant for
another conversation. No release/master changes or GitHub publication in this run.
