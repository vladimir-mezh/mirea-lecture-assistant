# Pulse compatibility — 0.2.37

## Incident and provenance

The real-profile log on 28 October showed `pulse_auth_incomplete` after the
email-code/required-action step. The application could no longer complete Pulse
login, independently of an already joined browser webinar.

MushkarevArt's [PR #33](https://github.com/vladimir-mezh/mirea-lecture-assistant/pull/33)
adds OIDC `form_post`, all three offered MAX skip-control variants and structural
diagnostics. Its commit `5db6eccf212e4f575b12f03d90718dffa2a8f5d5` is incorporated
with original authorship. It also packages an optional runtime-specific CA policy
file, without disabling certificate validation. The integration branch is based
on published 0.2.36, retaining the earlier code-delivery and reliability fixes.

## Reducing coupling to site updates

1. Use Pulse's native login entry point and keep its HTTP cookie jar/correlation
   cookies. Pulse, not the desktop application, redeems its authorization code.
2. Support both query redirects and HTML form POST responses. Input shape, not
   visual layout, CSS classes, a submit caption or an inline JavaScript spelling,
   determines the transition.
3. Pin the callback URI and state from the trusted initial authorization chain.
   Plain OIDC parameters and Keycloak's resolved `client_data` context after an
   opaque PAR `request_uri` are supported. This context is read only during the
   bootstrap originating from Pulse, never from an arbitrary HTML suggestion.
   Thus a newly declared Pulse callback path does not require editing a route
   list. Old flows without these hints retain the restricted known-route list.
4. Require HTTPS on the known Pulse host, exact declared callback path and static
   query parameters, matching state, non-duplicate response fields and permitted
   response parameters. Unknown destinations are never followed with credentials.
5. Respect same-origin HTTP 307/308 POST semantics; never copy the POST body to
   another origin. Detect repeated authorization responses before redeeming them
   again. Ordinary redirects retain their existing GET behavior.
6. Unknown OAuth forms are explicitly incompatible, not mistaken for a new email
   challenge. The app explains that compatibility needs checking instead of
   endlessly spending fresh codes. Diagnostics contain fixed reason identifiers,
   form counts and field-name categories, never values, cookies, HTML or tokens.
7. Keep the existing independent authenticated API/session validation before
   declaring a successful login. SSO cookies or a rendered page alone do not count.

An anonymous, no-credentials probe against the current site confirmed the PAR
bootstrap and successful extraction of callback/state. No real login or OTP was
initiated by that probe. This verifies current structural compatibility only.

## Safe manual verification

Launch `MireaLectureAssistant.exe --check-login`. This is a temporary per-process
mode, not a saved setting: automatic startup login waits for the user, lecture
opening/resume, scanner, QR submission and chat fallback are blocked. Schedule
loading and login still work. The window and log explicitly identify the mode.
After the user confirms login, close it and relaunch normally to resume the
unchanged lecture settings. Do not describe a mocked flow or a build smoke test
as proof of a live authenticated login.

## Limits and release discipline

No client can guarantee compatibility with every future backend change. Renamed
authentication cookies, changed API payloads, a new issuer/domain, mandatory MFA
or a different authentication protocol may still require an application update.
Failures should retain their shape diagnostics and produce a reviewed PR, not
download and execute remote patches or automatically merge an untrusted autofix.
Before release: synthetic protocol matrix, full regression suite, packaged
startup/HTTPS smoke and a user-initiated login with real schedule confirmation.

Protocol references:

- [OAuth Form Post Response Mode](https://openid.net/specs/oauth-v2-form-post-response-mode-1_0.html)
- [OpenID Connect Core](https://openid.net/specs/openid-connect-core-1_0.html)
- [Keycloak ClientData definition](https://github.com/keycloak/keycloak/blob/main/server-spi-private/src/main/java/org/keycloak/protocol/ClientData.java)

## Verified on the real profile

The user initiated login in the isolated-action verification mode on 28 October.
The app recorded `pulse_auth_contract_detected`,
`pulse_auth_callback_submitted mode=form_post`, `pulse_auth_cookie_received`,
`login_success` and `schedule_loaded lesson_count=37`. MCP independently reported
0.2.37, `signed_in=true`, verdict `ok`. There were no attendance submissions or
chat-fallback sends during the check. After user exit, ordinary startup again
recorded `stored_session_verified state=valid` and loaded 37 lessons.

649 Python tests, 17 extension tests, Ruff, packaged startup and HTTPS checks in
both normal and verification modes passed. The initial packaging regression on
PyInstaller 6.22.2 rejected an intentionally legacy nested launch before Python
started. This is addressed upstream in 6.22.3 (nested onefile parent validation),
so the build dependency now requires at least that patch version. Runtime
dependencies and certificate verification are unchanged.

See [PyInstaller 6.22.3 changes](https://pyinstaller.org/en/v6.22.3/CHANGES.html)
for the upstream packaging fix; do not suppress its security checks or remove
the legacy regression test to hide this failure.
