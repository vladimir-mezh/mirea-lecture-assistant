"""AI clients on this PC that can run a local MCP, and connecting ours to them.

Each client keeps its MCP servers in a settings file of its own. Only our one
entry is ever added, changed or removed; everything else in the file stays as
it was. A file that cannot be read safely (comments, damage) is not touched,
the previous version is kept as ``<file>.mirea-backup``, and a new one replaces
the old in one step, so a crash never leaves half a file.
"""

from __future__ import annotations

import json
import logging
import ntpath
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

NAME = "mirea-lecture-assistant"


@dataclass
class Client:
    key: str
    name: str
    # Settings files the client reads (Claude Desktop may have two installs).
    files: list[Path]
    kind: str = "json"  # "json" (mcpServers), "vscode" (servers), "toml" (Codex)
    restart: str = "Перезапустите программу, чтобы она увидела MCP."
    cli: list[str] = field(default_factory=list)  # a command that adds it instead


class NotSafe(RuntimeError):
    """The settings file cannot be changed without risking the person's own settings."""


def _home() -> Path:
    return Path(os.environ.get("USERPROFILE") or Path.home())


def _appdata() -> Path:
    return Path(os.environ.get("APPDATA") or _home() / "AppData" / "Roaming")


def _local() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or _home() / "AppData" / "Local")


def detect() -> list[Client]:
    """Clients that are installed here, as far as their own folders show."""
    home, appdata, local = _home(), _appdata(), _local()
    found: list[Client] = []

    claude_dirs = [appdata / "Claude"]
    # The Microsoft Store version keeps its settings in a package folder.
    claude_dirs += sorted(local.glob("Packages/Claude_*/LocalCache/Roaming/Claude"))
    claude_dirs = [d for d in claude_dirs if d.is_dir()]
    if claude_dirs or (local / "AnthropicClaude").is_dir():
        files = [d / "claude_desktop_config.json" for d in claude_dirs] or [
            appdata / "Claude" / "claude_desktop_config.json"
        ]
        found.append(
            Client(
                "claude-desktop",
                "Claude Desktop",
                files,
                restart="Полностью закройте Claude (значок у часов → Выход) и откройте снова.",
            )
        )
    claude_cli = shutil.which("claude")
    if (home / ".claude.json").is_file() or claude_cli:
        found.append(
            Client(
                "claude-code",
                "Claude Code",
                [home / ".claude.json"],
                restart="Начните новый сеанс Claude Code.",
                cli=[claude_cli] if claude_cli else [],
            )
        )
    if (home / ".codex").is_dir():
        found.append(
            Client(
                "codex",
                "Codex",
                [home / ".codex" / "config.toml"],
                kind="toml",
                restart="Начните новый сеанс Codex.",
            )
        )
    if (home / ".cursor").is_dir():
        found.append(Client("cursor", "Cursor", [home / ".cursor" / "mcp.json"]))
    if (home / ".codeium" / "windsurf").is_dir():
        found.append(
            Client("windsurf", "Windsurf", [home / ".codeium" / "windsurf" / "mcp_config.json"])
        )
    vscode = appdata / "Code" / "User"
    if vscode.is_dir():
        found.append(
            Client(
                "vscode",
                "VS Code (GitHub Copilot)",
                [vscode / "mcp.json"],
                kind="vscode",
                restart="Перезапустите VS Code.",
            )
        )
        cline = vscode / "globalStorage" / "saoudrizwan.claude-dev"
        if cline.is_dir():
            found.append(
                Client(
                    "cline",
                    "Cline (VS Code)",
                    [cline / "settings" / "cline_mcp_settings.json"],
                    restart="Перезапустите VS Code.",
                )
            )
    if (home / ".gemini").is_dir():
        found.append(
            Client(
                "gemini",
                "Gemini CLI",
                [home / ".gemini" / "settings.json"],
                restart="Начните новый сеанс Gemini CLI.",
            )
        )
    if (home / ".lmstudio").is_dir():
        found.append(Client("lmstudio", "LM Studio", [home / ".lmstudio" / "mcp.json"]))
    return found


# --- what goes into the files ---------------------------------------------


def _entry(client: Client, launcher: Path, profile: Path) -> dict:
    args = ["--profile", str(profile), "--client-name", client.name]
    if client.kind == "vscode":
        return {"type": "stdio", "command": str(launcher), "args": args}
    return {"command": str(launcher), "args": args}


def _section(client: Client) -> str:
    return "servers" if client.kind == "vscode" else "mcpServers"


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(
            path.read_text(encoding="utf-8-sig") or "{}", object_pairs_hook=_unique_pairs
        )
    except (OSError, ValueError) as exc:
        # Comments, a typo: rewriting it would lose what the person wrote.
        raise NotSafe(path.name) from exc
    if not isinstance(data, dict):
        raise NotSafe(path.name)
    return data


def _unique_pairs(pairs):
    data = {}
    for key, value in pairs:
        if key in data:
            raise ValueError("duplicate_json_key")
        data[key] = value
    return data


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copy2(path, path.with_name(path.name + ".mirea-backup"))
    temporary = path.with_name(path.name + ".mirea-tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


TOML_TABLE = re.compile(r"^\s*\[\[?\s*([^\]]+?)\s*\]\]?\s*(?:#.*)?$")


def _toml_without_ours(text: str) -> str:
    """The file minus our [mcp_servers.<NAME>] table (and its sub-tables)."""
    ours = (f"mcp_servers.{NAME}", f'mcp_servers."{NAME}"')
    kept, skipping = [], False
    for line in text.splitlines(keepends=True):
        table = TOML_TABLE.match(line)
        if table:
            name = table.group(1).replace(" ", "")
            skipping = any(name == own or name.startswith(own + ".") for own in ours)
        if not skipping:
            kept.append(line)
    return "".join(kept)


def _toml_value(value) -> str:
    # A JSON string is a valid TOML basic string (escapes included).
    if isinstance(value, list):
        return "[" + ", ".join(json.dumps(item) for item in value) + "]"
    return json.dumps(value)


def _toml_check(text: str, path: Path) -> None:
    import tomllib

    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise NotSafe(path.name) from exc


# --- status, connect, disconnect -------------------------------------------


def _configured(client: Client, path: Path) -> dict | None:
    if client.kind == "toml":
        if not path.exists():
            return None
        import tomllib

        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, tomllib.TOMLDecodeError):
            return None
        servers = data.get("mcp_servers", {})
        return servers.get(NAME) if isinstance(servers, dict) else None
    try:
        entry = _read_json(path).get(_section(client), {})
    except NotSafe:
        return None
    return entry.get(NAME) if isinstance(entry, dict) else None


def status(client: Client, launcher: Path, profile: Path) -> str:
    """Describe existing registrations, ignoring a second, unconfigured install."""
    entries = [e for path in client.files if (e := _configured(client, path)) is not None]
    if not entries:
        return "absent"
    if all(isinstance(e, dict) and _disabled(e) for e in entries):
        return "disabled"
    if all(
        isinstance(e, dict) and (_disabled(e) or _current(e, launcher, profile)) for e in entries
    ):
        return "connected"
    return "outdated"


def _disabled(entry: dict) -> bool:
    return entry.get("disabled") is True or entry.get("enabled") is False


def _path_key(value) -> str | None:
    """Compare local Windows aliases without accessing arbitrary UNC servers."""
    if not isinstance(value, str) or not value:
        return None
    value = value.replace("/", "\\")
    local_share = re.match(
        r"^\\\\(?:localhost|127\.0\.0\.1)\\([a-z])\$(?=\\|$)", value, re.IGNORECASE
    )
    if local_share:
        value = local_share[1] + ":" + value[local_share.end() :]
    return ntpath.normcase(ntpath.normpath(value))


def _profile_arg(entry: dict) -> tuple[list[str], int] | None:
    args = entry.get("args", [])
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        return None
    positions = [i for i, a in enumerate(args) if a == "--profile" or a.startswith("--profile=")]
    if len(positions) != 1:
        return None
    index = positions[0]
    if args[index] == "--profile" and (index + 1 == len(args) or args[index + 1].startswith("--")):
        return None
    return args, index


def _current(entry: dict, launcher: Path, profile: Path) -> bool:
    if entry.get("type", "stdio") != "stdio" or "url" in entry:
        return False
    arg = _profile_arg(entry)
    if arg is None or _path_key(entry.get("command")) != _path_key(str(launcher)):
        return False
    args, index = arg
    value = args[index].partition("=")[2] if "=" in args[index] else args[index + 1]
    return _path_key(value) == _path_key(str(profile))


def _repaired_entry(entry, launcher: Path, profile: Path) -> dict:
    if not isinstance(entry, dict):
        raise NotSafe("unsupported_registration")
    if _disabled(entry) or _current(entry, launcher, profile):
        return entry
    if entry.get("type", "stdio") != "stdio" or "url" in entry:
        raise NotSafe("unsupported_registration")
    arg = _profile_arg(entry)
    # Only a registration generated for our local launcher is maintained. Do
    # not replace a user's custom wrapper or an unrelated server with our name.
    command = _path_key(entry.get("command"))
    if arg is None or not command or ntpath.basename(command) != "mcplauncher.exe":
        raise NotSafe("custom_registration")
    args, index = arg
    args = args.copy()
    if "=" in args[index]:
        args[index] = "--profile=" + str(profile)
    else:
        args[index + 1] = str(profile)
    return {**entry, "command": str(launcher), "args": args}


def _repair_toml(text: str, old: dict, new: dict) -> str:
    """Replace only command/args, preserving other keys and nested tables."""
    import tomllib

    data = tomllib.loads(text)
    lines = text.splitlines(keepends=True)
    own_names = {f"mcp_servers.{NAME}", f'mcp_servers."{NAME}"', f"mcp_servers.'{NAME}'"}
    in_ours, changed, i = False, set(), 0
    output = []
    while i < len(lines):
        line = lines[i]
        table = TOML_TABLE.match(line)
        if table:
            in_ours = table[1].replace(" ", "") in own_names
        key = re.match(r"^\s*(command|args|\"command\"|\"args\"|'command'|'args')\s*=", line)
        if in_ours and key:
            name = key[1].strip("\"'")
            end = i + 1
            while end <= len(lines):
                try:
                    parsed = tomllib.loads("".join(lines[i:end]))
                except tomllib.TOMLDecodeError:
                    end += 1
                    continue
                if parsed != {name: old[name]}:
                    raise NotSafe("complex_toml_registration")
                break
            else:
                raise NotSafe("complex_toml_registration")
            output.append(f"{name} = {_toml_value(new[name])}\n")
            changed.add(name)
            i = end
        else:
            output.append(line)
            i += 1
    candidate = "".join(output)
    data["mcp_servers"][NAME] = new
    if changed != {"command", "args"} or tomllib.loads(candidate) != data:
        raise NotSafe("complex_toml_registration")
    return candidate


def repair_links(client: Client, launcher: Path, profile: Path) -> bool:
    """Maintain existing enabled registrations; never create a connection."""
    import tomllib

    plans = []
    for path in client.files:
        if not path.is_file():
            continue
        before = path.read_text(encoding="utf-8-sig")
        try:
            data = tomllib.loads(before) if client.kind == "toml" else _read_json(path)
        except (ValueError, NotSafe) as exc:
            raise NotSafe("invalid_configuration") from exc
        servers = data.get("mcp_servers" if client.kind == "toml" else _section(client), {})
        if not isinstance(servers, dict):
            raise NotSafe("invalid_configuration")
        if NAME not in servers:
            continue
        entry = servers[NAME]
        repaired = _repaired_entry(entry, launcher, profile)
        if repaired == entry:
            continue
        if client.kind == "toml":
            after = _repair_toml(before, entry, repaired)
        else:
            servers[NAME] = repaired
            after = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        plans.append((path, before, after, entry, repaired))
    for path, before, after, entry, repaired in plans:
        # A running client or the person may have changed/disconnected it since
        # the scan. Retry from a new snapshot, never restore the stale snapshot.
        if path.read_text(encoding="utf-8-sig") != before:
            raise NotSafe("configuration_changed")
        if client.cli:
            shutil.copy2(path, path.with_name(path.name + ".mirea-backup"))
            if not _run_cli(client, ["mcp", "remove", "--scope", "user", NAME]):
                raise NotSafe("cli_remove_failed")
            if not _run_cli(
                client, ["mcp", "add-json", "--scope", "user", NAME, json.dumps(repaired)]
            ):
                # Preserve the original registration even if the CLI's new
                # schema rejects it. Never print the configuration or stderr.
                if not _run_cli(
                    client, ["mcp", "add-json", "--scope", "user", NAME, json.dumps(entry)]
                ):
                    current = _read_json(path)
                    section = current.setdefault(_section(client), {})
                    if not isinstance(section, dict) or NAME in section:
                        raise NotSafe("cli_restore_conflict")
                    section[NAME] = entry
                    _write(path, json.dumps(current, ensure_ascii=False, indent=2) + "\n")
                raise NotSafe("cli_update_failed")
            if _configured(client, path) != repaired:
                raise NotSafe("cli_update_unconfirmed")
        else:
            _write(path, after)
    if plans:
        log.info("ai_client_links_updated client=%s", client.key)
    return bool(plans)


def sync_links(launcher: Path, profile: Path) -> dict:
    updated, errors = [], []
    for client in detect():
        try:
            if repair_links(client, launcher, profile):
                updated.append(client.key)
        except (NotSafe, OSError, ValueError):
            errors.append(client.key)
            log.warning("ai_client_links_pending client=%s", client.key)
    return {"updated": updated, "errors": errors}


def _run_cli(client: Client, args: list[str]) -> bool:
    try:
        result = subprocess.run(
            [*client.cli, *args],
            capture_output=True,
            timeout=60,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def connect(client: Client, launcher: Path, profile: Path) -> None:
    """Add (or correct) our entry. Raises NotSafe or OSError, changing nothing."""
    entry = _entry(client, launcher, profile)
    if client.cli:
        # Claude Code keeps its settings in memory and writes them back: its own
        # command is the one way an edit is not undone by a running session.
        _run_cli(client, ["mcp", "remove", "--scope", "user", NAME])
        if _run_cli(
            client,
            ["mcp", "add", "--scope", "user", NAME, "--", entry["command"], *entry["args"]],
        ):
            log.info("ai_client_connected client=%s via=cli", client.key)
            return
    for path in client.files:
        if client.kind == "toml":
            text = path.read_text(encoding="utf-8") if path.exists() else ""
            _toml_check(text, path)
            body = _toml_without_ours(text).rstrip("\n")
            table = [f"[mcp_servers.{NAME}]"] + [
                f"{key} = {_toml_value(value)}" for key, value in entry.items()
            ]
            new = (body + "\n\n" if body else "") + "\n".join(table) + "\n"
            _toml_check(new, path)
            _write(path, new)
        else:
            data = _read_json(path)
            servers = data.get(_section(client))
            if servers is None:
                servers = data[_section(client)] = {}
            if not isinstance(servers, dict):
                raise NotSafe(path.name)
            servers[NAME] = entry
            _write(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    log.info("ai_client_connected client=%s", client.key)


def disconnect(client: Client) -> None:
    """Remove our entry only."""
    if client.cli and _run_cli(client, ["mcp", "remove", "--scope", "user", NAME]):
        log.info("ai_client_disconnected client=%s via=cli", client.key)
        return
    for path in client.files:
        if not path.exists():
            continue
        if client.kind == "toml":
            text = path.read_text(encoding="utf-8")
            _toml_check(text, path)
            new = _toml_without_ours(text)
            if new != text:
                _write(path, new.rstrip("\n") + "\n")
            continue
        data = _read_json(path)
        servers = data.get(_section(client))
        if isinstance(servers, dict) and NAME in servers:
            del servers[NAME]
            _write(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    log.info("ai_client_disconnected client=%s", client.key)


def supported() -> bool:
    return sys.platform == "win32"
