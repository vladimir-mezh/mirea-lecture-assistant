# Automatic MCP client registration maintenance

Already registered local `McpLauncher.exe` entries are checked at application
startup, after installing MCP, when opening its page, and every 30 seconds.
Maintenance runs in the background and is serialized with manual connect,
disconnect and MCP installation. The page no longer offers an Update button for
stale paths. It keeps explicit Connect and Disconnect actions.

Only existing registrations are changed. Missing files/entries and registrations
with `disabled=true` or `enabled=false` are left alone. Other MCP servers, custom
client names, environment variables and additional options are preserved.
Local drive and localhost administrative-share aliases compare equal without
probing arbitrary network shares. JSON and TOML are validated before editing;
TOML comments and nested settings are preserved. Ambiguous/custom registrations
are left unchanged and retried later. Each changed file has a `.mirea-backup`.
Claude Code uses its own `mcp add-json --scope user` command and restores the old
entry if adding the replacement fails. Command arguments and stderr are not
logged. Client restart may still be required to load changed settings.

Smoke tests never maintain real client settings. Tests use temporary profiles,
including disabled/missing entries, idempotence, unknown formats, CLI failure,
JSON options, TOML multiline arguments and the UI's concurrent-operation guard.

The connection status groups adapter sessions by client name. A live connected
session wins over an idle one, so multiple Codex chats produce one status line.
Leases still expire/disconnect independently; stale leases are removed before
enforcing the registration limit. Grouping does not terminate any client process.

CLI reference: [Claude Code MCP configuration](https://code.claude.com/docs/en/mcp).

## Separate Defender observation, 29 October 2026

Defender reported `Trojan:Win32/Bearfoos.A!ml` for the independent MCP **0.2.0**
adapter, not the Lecture Assistant executable. The threat query reported an active
detection; action history contained both unsuccessful and successful records.
Reading/hash verification of the installed executable was blocked by Defender.
No exclusions, protection changes,
unblocking or quarantine restoration were performed.

The public MCP release points to source commit
`3dfcc1daf34d9eb84f039bf9e8f936715ad9bac0`. Its small first-party adapter source
exposes the app's local API over stdio and has no arbitrary shell/command tool.
This source observation does **not** establish that the blocked binary or its
bundled dependencies are safe or bit-identical to that source. The detection
remains unresolved; it must not be described as a confirmed false positive.

Microsoft provides a [file analysis submission service](https://www.microsoft.com/en-us/wdsi/filesubmission)
for suspected incorrect detections. No file was submitted on the user's behalf.
The separate MCP release and Windows protection settings are unchanged.

The freshly built Lecture Assistant executable passed its own isolated startup
and HTTPS checks and a targeted Defender scan with no new detection for that
executable. This is a separate result and does not clear the MCP 0.2.0 warning.
