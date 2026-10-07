from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from mirea_lecture_assistant import ai_clients

LAUNCHER = Path("C:/Users/Студент/AppData/Local/MireaLectureAssistant/mcp/McpLauncher.exe")
PROFILE = Path("C:/Users/Студент/AppData/Local/MireaLectureAssistant")


@pytest.fixture
def pc(tmp_path, monkeypatch):
    home = tmp_path / "home"
    appdata, local = home / "AppData" / "Roaming", home / "AppData" / "Local"
    for folder in (home, appdata, local):
        folder.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setattr(ai_clients.shutil, "which", lambda _name: None)
    return home, appdata, local


def by_key(key):
    return next(client for client in ai_clients.detect() if client.key == key)


def test_only_installed_clients_are_found(pc):
    home, appdata, _local = pc
    assert ai_clients.detect() == []
    (appdata / "Claude").mkdir()
    (home / ".codex").mkdir()
    (home / ".cursor").mkdir()
    (appdata / "Code" / "User" / "globalStorage" / "saoudrizwan.claude-dev").mkdir(parents=True)
    names = [client.name for client in ai_clients.detect()]
    assert names == [
        "Claude Desktop",
        "Codex",
        "Cursor",
        "VS Code (GitHub Copilot)",
        "Cline (VS Code)",
    ]


def test_store_version_of_claude_desktop_is_found_too(pc):
    _home, _appdata, local = pc
    store = local / "Packages" / "Claude_pzs8sxrjxfjjc" / "LocalCache" / "Roaming" / "Claude"
    store.mkdir(parents=True)
    client = by_key("claude-desktop")
    assert client.files == [store / "claude_desktop_config.json"]


def test_connect_keeps_everything_else_and_disconnect_removes_only_ours(pc):
    _home, appdata, _local = pc
    (appdata / "Claude").mkdir()
    config = appdata / "Claude" / "claude_desktop_config.json"
    original = {
        "mcpServers": {"files": {"command": "npx", "args": ["files"]}},
        "preferences": {"theme": "dark"},
    }
    config.write_text(json.dumps(original), encoding="utf-8")
    client = by_key("claude-desktop")
    assert ai_clients.status(client, LAUNCHER, PROFILE) == "absent"

    ai_clients.connect(client, LAUNCHER, PROFILE)
    ai_clients.connect(client, LAUNCHER, PROFILE)  # twice is the same as once

    data = json.loads(config.read_text(encoding="utf-8"))
    assert data["preferences"] == {"theme": "dark"}
    assert data["mcpServers"]["files"] == original["mcpServers"]["files"]
    assert data["mcpServers"][ai_clients.NAME] == {
        "command": str(LAUNCHER),
        "args": ["--profile", str(PROFILE), "--client-name", "Claude Desktop"],
    }
    assert ai_clients.status(client, LAUNCHER, PROFILE) == "connected"
    assert ai_clients.status(client, LAUNCHER.with_name("other.exe"), PROFILE) == "outdated"
    assert (config.parent / "claude_desktop_config.json.mirea-backup").exists()

    ai_clients.disconnect(client)
    assert json.loads(config.read_text(encoding="utf-8")) == original


def test_a_file_with_comments_is_never_rewritten(pc):
    home, _appdata, _local = pc
    (home / ".cursor").mkdir()
    config = home / ".cursor" / "mcp.json"
    text = '{\n  // my servers\n  "mcpServers": {}\n}\n'
    config.write_text(text, encoding="utf-8")
    with pytest.raises(ai_clients.NotSafe):
        ai_clients.connect(by_key("cursor"), LAUNCHER, PROFILE)
    assert config.read_text(encoding="utf-8") == text


def test_vscode_gets_its_own_format(pc):
    _home, appdata, _local = pc
    (appdata / "Code" / "User").mkdir(parents=True)
    client = by_key("vscode")
    ai_clients.connect(client, LAUNCHER, PROFILE)
    data = json.loads((appdata / "Code" / "User" / "mcp.json").read_text(encoding="utf-8"))
    assert data["servers"][ai_clients.NAME]["type"] == "stdio"
    assert ai_clients.status(client, LAUNCHER, PROFILE) == "connected"


def test_codex_toml_stays_valid_and_keeps_other_tables(pc):
    home, _appdata, _local = pc
    (home / ".codex").mkdir()
    config = home / ".codex" / "config.toml"
    config.write_text(
        'model = "gpt-5"\n\n[mcp_servers.other]\ncommand = "other.exe"\n\n'
        '[projects."C:\\\\work"]\ntrust_level = "trusted"\n',
        encoding="utf-8",
    )
    client = by_key("codex")
    ai_clients.connect(client, LAUNCHER, PROFILE)
    ai_clients.connect(client, LAUNCHER.with_name("Moved.exe"), PROFILE)  # replaced, not doubled

    data = tomllib.loads(config.read_text(encoding="utf-8"))
    assert data["model"] == "gpt-5"
    assert data["mcp_servers"]["other"] == {"command": "other.exe"}
    assert data["projects"]["C:\\work"] == {"trust_level": "trusted"}
    ours = data["mcp_servers"][ai_clients.NAME]
    assert ours["command"] == str(LAUNCHER.with_name("Moved.exe"))
    assert ours["args"][-1] == "Codex"
    assert config.read_text(encoding="utf-8").count(f"[mcp_servers.{ai_clients.NAME}]") == 1

    ai_clients.disconnect(client)
    data = tomllib.loads(config.read_text(encoding="utf-8"))
    assert ai_clients.NAME not in data["mcp_servers"] and data["projects"]


def test_broken_codex_settings_are_not_touched(pc):
    home, _appdata, _local = pc
    (home / ".codex").mkdir()
    config = home / ".codex" / "config.toml"
    config.write_text("model = \n", encoding="utf-8")
    with pytest.raises(ai_clients.NotSafe):
        ai_clients.connect(by_key("codex"), LAUNCHER, PROFILE)
    assert config.read_text(encoding="utf-8") == "model = \n"


def test_claude_code_is_connected_through_its_own_command(pc, monkeypatch):
    home, _appdata, _local = pc
    (home / ".claude.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(ai_clients.shutil, "which", lambda name: "C:/bin/claude.exe")
    calls = []

    class Done:
        returncode = 0

    monkeypatch.setattr(ai_clients.subprocess, "run", lambda args, **_: calls.append(args) or Done)
    ai_clients.connect(by_key("claude-code"), LAUNCHER, PROFILE)
    assert calls[-1][:6] == ["C:/bin/claude.exe", "mcp", "add", "--scope", "user", ai_clients.NAME]
    assert calls[-1][6:] == [
        "--",
        str(LAUNCHER),
        "--profile",
        str(PROFILE),
        "--client-name",
        "Claude Code",
    ]
    assert (home / ".claude.json").read_text(encoding="utf-8") == "{}"  # not edited by us
