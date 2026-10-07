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
        data = json.loads(path.read_text(encoding="utf-8-sig") or "{}")
    except (OSError, ValueError) as exc:
        # Comments, a typo: rewriting it would lose what the person wrote.
        raise NotSafe(path.name) from exc
    if not isinstance(data, dict):
        raise NotSafe(path.name)
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
        except (OSError, tomllib.TOMLDecodeError):
            return None
        return data.get("mcp_servers", {}).get(NAME)
    try:
        entry = _read_json(path).get(_section(client), {})
    except NotSafe:
        return None
    return entry.get(NAME) if isinstance(entry, dict) else None


def status(client: Client, launcher: Path, profile: Path) -> str:
    """``connected``, ``outdated`` (another path) or ``absent``."""
    entries = [_configured(client, path) for path in client.files]
    if not any(entries):
        return "absent"
    wanted = _entry(client, launcher, profile)
    if all(
        e and e.get("command") == wanted["command"] and e.get("args") == wanted["args"]
        for e in entries
    ):
        return "connected"
    return "outdated"


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
