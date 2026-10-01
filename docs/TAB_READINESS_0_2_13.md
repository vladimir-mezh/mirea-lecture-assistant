# Tab readiness in local 0.2.13

This extends the local reliability changes in RELIABILITY_0_2_12.md. No changes to
course/group filters, lecture ranking, room matching rules or attendance/chat rules.

- Starting Chrome and reaching its CDP endpoint no longer completes open().
  For a lecture URL, wait up to 15 seconds for a selected page's loaded document
  with visible text or media/frame elements; outer async wait is bounded at 20s.
  Internal about:blank browser startup for SDO helper work remains supported.
- Exclude blank/about/chrome-error pages, closed tabs and helper targets from
  lecture selection, including a previously pinned tab. Fall back to the existing
  matching-page search, so a valid popup/redirect replaces a pinned blank tab.
- _active_page also checks document readiness. Capture/join/chat cannot silently
  operate on an empty document. Existing health checks classify an empty HTTP
  document as unstable and a missing/blank tab as lost, triggering normal recovery.
- UI differentiates page-loaded from connected-to-room. Clicking a lobby control
  is followed by a state check; still-waiting is not reported as joined.
- Tests use fake pages plus an isolated local Chrome/server, not real lecture/chat
  traffic. Popups replacing about:blank and empty HTTP documents are covered.

Limits: a platform loading/error page with visible text is a loaded document, not
proof of a working broadcast; existing banner, entry, media-progress and fresh-
capture checks remain responsible for that distinction. No new wrong-room filter.

Verification on 2026-10-01: 403 tests passed, Ruff and diff checks passed.
Packaged isolated-profile smoke exited 0 with HTTPS 200. The first request to
GitHub's large homepage timed out; retry against robots.txt succeeded. Installed
0.2.13 locally at the user's Downloads folder; real startup
confirmed valid saved session and 37 schedule entries. No test attendance/chat
messages were sent to real lectures. Delivery branch: codex/reliability-0.2.11;
master and GitHub release publication are separate actions.

Installed SHA256: B50FB1A022BC1B5A0A050D7ED3FEF315D5A47608815EAC39179522222B3F79CF.
