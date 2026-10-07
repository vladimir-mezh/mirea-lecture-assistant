# Optional MCP integration — Lecture Assistant 0.2.28

MCP is an independent project: https://github.com/vladimir-mezh/mirea-lecture-assistant-mcp

Initial Windows release: https://github.com/vladimir-mezh/mirea-lecture-assistant-mcp/releases/tag/v0.1.0

The application has no built-in model, AI chat, API key field or MCP SDK dependency. The MCP page follows Settings in the sidebar. It downloads the independent stable release on demand and displays installed version, access permissions and connected client heartbeats.

## Connecting

1. Open MCP and download the adapter (or check its independent updates).
2. Enable local MCP access. Optionally enable changing settings and subject rules.
3. Copy the configuration into an AI client supporting local stdio MCP servers. The client launches the adapter; Lecture Assistant must remain running.

Both access and write permissions default to off. A live adapter heartbeat is not proof that an AI model has issued a tool request: the page distinguishes waiting for the first request from connected. Clients expire after 35 seconds without heartbeats.

The config points to a stable `McpLauncher.exe`, which selects `mcp/current.json`. Updating the adapter does not require editing client configuration. Already running clients keep their previous executable; restart the client to select an updated version.

## Connecting AI clients automatically

The MCP tab lists AI clients found on the PC by their own settings folders and connects one with a click: it installs the adapter if needed, enables access and adds a single `mirea-lecture-assistant` entry (`McpLauncher.exe --profile <profile> --client-name <client>`) to the client's settings.

| Client | Settings file |
| --- | --- |
| Claude Desktop | `%APPDATA%\Claude\claude_desktop_config.json` (Store version: `%LOCALAPPDATA%\Packages\Claude_*\LocalCache\Roaming\Claude\…`) |
| Claude Code | `claude mcp add --scope user …` (falls back to `~/.claude.json`) |
| Codex | `~/.codex/config.toml`, `[mcp_servers.mirea-lecture-assistant]` |
| Cursor | `~/.cursor/mcp.json` |
| Windsurf | `~/.codeium/windsurf/mcp_config.json` |
| VS Code (Copilot) | `%APPDATA%\Code\User\mcp.json`, `servers` with `type: stdio` |
| Cline | `…\globalStorage\saoudrizwan.claude-dev\settings\cline_mcp_settings.json` |
| Gemini CLI | `~/.gemini/settings.json` |
| LM Studio | `~/.lmstudio/mcp.json` |

Only that entry is added, replaced or removed. A file that does not parse as plain JSON/TOML (comments, damage) is left untouched and the person is offered the manual configuration instead. The previous file is kept as `<file>.mirea-backup`, and the new one replaces it atomically.

## Independent updates

- Updating Lecture Assistant replaces only its own `.exe`. MCP lives in the profile (`%LOCALAPPDATA%\MireaLectureAssistant\mcp`), so the adapter, the `mcp_enabled`/`mcp_allow_changes` settings and the client configuration survive it. The new copy publishes a new pairing token, which the adapter reads on its next request.
- Updating MCP never touches the application: a new `versions/<version>` folder is added and `current.json` is switched atomically.
- Older adapter versions are removed like the application's own `.old` file: right after an update and then every few seconds, but only while no AI client runs them (Windows keeps a running program locked). Interrupted `.staging-*` folders go too, never during an install.
- A newer `McpLauncher.exe` replaces the old one by renaming the running launcher to `McpLauncher.exe.<random>.old`, removed once released. The release archive contains the adapter, launcher, manifest, README and MIT license.

## API contract v1

The application owns a private loopback HTTP endpoint and rotates a random pairing token when it starts. Only the adapter reads `mcp-connection.json` from the selected profile. Do not publish that file. Client configuration contains no pairing token. The adapter bypasses HTTP proxy environment settings for loopback requests.

Requests are POST `/rpc`, JSON, bearer authenticated, exact loopback Host and no browser Origin. Maximum request size is 16 KiB. The UI processes requests on its own thread; timed-out queued requests are cancelled before execution. The adapter rereads the connection information for every request, allowing application restarts without a new client config.

Six model-facing tools map to application methods:

| Tool | Application method | Effect |
| --- | --- | --- |
| `get_status` | `status` | Application version, presence of saved session, scanner status and last frame age |
| `get_settings` | `get_settings` | Allowed settings and validation schema |
| `get_schedule` | `get_schedule` | Cached lesson metadata, up to 200 lessons |
| `get_subject_rules` | `get_subject_rules` | Existing subject modes |
| `update_settings` | `update_settings` | Validated settings, with explicit write permission |
| `set_subject_rule` | `set_subject_rule` | AUTO/ASK/IGNORE for an existing subject, with write permission |

`session_present` is not a claim that the remote session has been verified at this moment. Schedule retrieval does not contact MIREA. Changing settings is rejected while the UI has unsaved edits. Supported settings exclude credentials, chat settings and raw QR data; numeric ranges and exact JSON types are checked. Diagnostic status excludes session cookies, URLs with tokens and codes. There are no attendance, chat-send, login-code, arbitrary file, shell or password tools.

Adapter releases may extend their own implementation independently while retaining API v1 compatibility. New application capabilities require an explicit compatible API change; a standalone adapter update cannot add powers the application does not expose. Incompatible release manifests are rejected before activation.

## Installation validation and checks

- The installer accepts assets only from the independent repository, checks archive SHA-256, executable SHA-256 and Windows headers, a fixed archive file list and protocol compatibility; path traversal and oversized archives are rejected.
- New versions are staged separately and activated through an atomic current manifest replacement. Invalid downloads do not replace the selected version.
- Application Python suite: 497 tests passed. Separate adapter suite: 7 tests passed, including actual SDK stdio handshakes with packaged adapter and stable launcher, permissions and a synthetic application endpoint.
- Download of the published v0.1.0 assets through the application's installer succeeded.
- Packaged application 0.2.27 smoke startup exited normally without a decompression error.
- No real lecture messages, attendance actions, QR deliveries or credential exports were used in these tests. An actual AI client still needs to be configured by the user; tests do not claim that a model has connected to the real profile.

Released in Lecture Assistant 0.2.28 together with the browser reliability fixes documented in `BROWSER_RELIABILITY_0_2_26.md`.
