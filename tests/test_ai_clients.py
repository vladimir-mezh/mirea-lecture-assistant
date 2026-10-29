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


def registration(command="C:/old/McpLauncher.exe", profile="C:/old/profile", **extra):
    return {"command": command, "args": ["--profile", profile, "--client-name", "Custom"], **extra}


def test_auto_repair_preserves_custom_options_other_servers_and_is_idempotent(tmp_path):
    path = tmp_path / "mcp.json"
    entry = registration(env={"CUSTOM": "fixture"}, autoApprove=["check_health"])
    original = {
        "mcpServers": {ai_clients.NAME: entry, "other": {"command": "other"}},
        "theme": "dark",
    }
    path.write_text(json.dumps(original), encoding="utf-8")
    client = ai_clients.Client("fixture", "Fixture", [path, tmp_path / "not-configured.json"])
    assert ai_clients.repair_links(client, LAUNCHER, PROFILE)
    data = json.loads(path.read_text(encoding="utf-8"))
    repaired = data["mcpServers"][ai_clients.NAME]
    assert repaired["command"] == str(LAUNCHER)
    assert repaired["args"] == ["--profile", str(PROFILE), "--client-name", "Custom"]
    assert repaired["env"] == entry["env"] and repaired["autoApprove"] == entry["autoApprove"]
    assert data["theme"] == "dark" and data["mcpServers"]["other"] == {"command": "other"}
    assert not client.files[1].exists()
    assert ai_clients.status(client, LAUNCHER, PROFILE) == "connected"
    mtime = path.stat().st_mtime_ns
    assert not ai_clients.repair_links(client, LAUNCHER, PROFILE)
    assert path.stat().st_mtime_ns == mtime
    assert json.loads(path.with_name(path.name + ".mirea-backup").read_text()) == original


@pytest.mark.parametrize("entry", [None, {"enabled": False}, {"disabled": True}])
def test_auto_repair_never_connects_absent_or_disabled_clients(tmp_path, entry):
    path = tmp_path / "mcp.json"
    data = {"mcpServers": {ai_clients.NAME: registration(**entry)} if entry else {}}
    text = json.dumps(data)
    path.write_text(text, encoding="utf-8")
    client = ai_clients.Client("fixture", "Fixture", [path])
    assert not ai_clients.repair_links(client, LAUNCHER, PROFILE)
    assert path.read_text(encoding="utf-8") == text
    assert ai_clients.status(client, LAUNCHER, PROFILE) == ("disabled" if entry else "absent")


@pytest.mark.parametrize(
    "entry",
    [
        [],
        "broken",
        {},
        {"url": "https://example.test"},
        registration(command="custom-wrapper.exe"),
        {"command": "McpLauncher.exe", "args": ["--profile"]},
    ],
)
def test_auto_repair_refuses_unrecognized_entries_without_overwriting(tmp_path, entry):
    path = tmp_path / "mcp.json"
    text = json.dumps({"mcpServers": {ai_clients.NAME: entry}})
    path.write_text(text, encoding="utf-8")
    client = ai_clients.Client("fixture", "Fixture", [path])
    assert ai_clients.status(client, LAUNCHER, PROFILE) == "outdated"
    with pytest.raises(ai_clients.NotSafe):
        ai_clients.repair_links(client, LAUNCHER, PROFILE)
    assert path.read_text(encoding="utf-8") == text


def test_local_unc_alias_and_custom_client_name_need_no_rewrite(tmp_path):
    path = tmp_path / "mcp.json"
    entry = registration(r"\\localhost\C$\tools\McpLauncher.exe", r"\\127.0.0.1\C$\profile")
    entry["args"] = ["--profile=" + r"\\127.0.0.1\C$\profile", "--client-name", "Mine"]
    path.write_text(json.dumps({"mcpServers": {ai_clients.NAME: entry}}), encoding="utf-8")
    client = ai_clients.Client("fixture", "Fixture", [path])
    assert (
        ai_clients.status(client, Path("c:/TOOLS/McpLauncher.exe"), Path("C:/profile"))
        == "connected"
    )
    assert not ai_clients.repair_links(client, Path("c:/TOOLS/McpLauncher.exe"), Path("C:/profile"))
    assert ai_clients._path_key(r"\\foreign\C$\profile") != ai_clients._path_key("C:/profile")


def test_toml_auto_repair_keeps_nested_settings_comments_and_multiline_args(tmp_path):
    path = tmp_path / "config.toml"
    text = (
        'model = "fixture"\n# keep me\n[mcp_servers."mirea-lecture-assistant"]\n'
        'command = "C:/old/McpLauncher.exe"\nargs = [\n"--profile", "C:/old",\n'
        '"--client-name", "Mine",\n]\nstartup_timeout_sec = 45\n'
        '[mcp_servers."mirea-lecture-assistant".env]\nCUSTOM = "keep"\n'
        '[mcp_servers.other]\ncommand = "other"\n'
    )
    path.write_text(text, encoding="utf-8")
    client = ai_clients.Client("codex", "Codex", [path], kind="toml")
    assert ai_clients.repair_links(client, LAUNCHER, PROFILE)
    after = path.read_text(encoding="utf-8")
    assert "# keep me" in after and '[mcp_servers."mirea-lecture-assistant".env]' in after
    before_data, after_data = tomllib.loads(text), tomllib.loads(after)
    before_data["mcp_servers"][ai_clients.NAME]["command"] = str(LAUNCHER)
    before_data["mcp_servers"][ai_clients.NAME]["args"][1] = str(PROFILE)
    assert after_data == before_data


def test_auto_repair_plans_all_files_before_touching_any(tmp_path):
    good, bad = tmp_path / "good.json", tmp_path / "bad.json"
    text = json.dumps({"mcpServers": {ai_clients.NAME: registration()}})
    good.write_text(text, encoding="utf-8")
    bad.write_text("// comments", encoding="utf-8")
    client = ai_clients.Client("fixture", "Fixture", [good, bad])
    with pytest.raises(ai_clients.NotSafe):
        ai_clients.repair_links(client, LAUNCHER, PROFILE)
    assert good.read_text(encoding="utf-8") == text


def test_claude_auto_repair_uses_cli_and_rolls_back_failed_update(tmp_path, monkeypatch):
    path = tmp_path / ".claude.json"
    entry = registration(env={"CUSTOM": "keep"})
    path.write_text(
        json.dumps({"mcpServers": {ai_clients.NAME: entry}, "other": True}), encoding="utf-8"
    )
    client = ai_clients.Client("claude-code", "Claude Code", [path], cli=["fixture-cli"])
    calls = []

    def run(_client, args):
        calls.append(args)
        data = json.loads(path.read_text(encoding="utf-8"))
        if args[1] == "remove":
            del data["mcpServers"][ai_clients.NAME]
        elif len(calls) == 2:
            return False
        else:
            data["mcpServers"][ai_clients.NAME] = json.loads(args[-1])
        path.write_text(json.dumps(data), encoding="utf-8")
        return True

    monkeypatch.setattr(ai_clients, "_run_cli", run)
    with pytest.raises(ai_clients.NotSafe):
        ai_clients.repair_links(client, LAUNCHER, PROFILE)
    assert len(calls) == 3 and calls[1][1] == "add-json"
    assert json.loads(path.read_text())["mcpServers"][ai_clients.NAME] == entry
    assert json.loads(path.read_text())["other"] is True

    calls.clear()

    def success(_client, args):
        data = json.loads(path.read_text())
        if args[1] == "remove":
            del data["mcpServers"][ai_clients.NAME]
        else:
            data["mcpServers"][ai_clients.NAME] = json.loads(args[-1])
        path.write_text(json.dumps(data), encoding="utf-8")
        return True

    monkeypatch.setattr(ai_clients, "_run_cli", success)
    assert ai_clients.repair_links(client, LAUNCHER, PROFILE)
    assert ai_clients.status(client, LAUNCHER, PROFILE) == "connected"


def test_one_invalid_client_does_not_stop_other_clients(tmp_path, monkeypatch, caplog):
    good, bad = tmp_path / "good.json", tmp_path / "bad.json"
    good.write_text(json.dumps({"mcpServers": {ai_clients.NAME: registration()}}), encoding="utf-8")
    bad.write_text("{", encoding="utf-8")
    monkeypatch.setattr(
        ai_clients,
        "detect",
        lambda: [ai_clients.Client("bad", "Bad", [bad]), ai_clients.Client("good", "Good", [good])],
    )
    assert ai_clients.sync_links(LAUNCHER, PROFILE) == {"updated": ["good"], "errors": ["bad"]}
    assert str(PROFILE) not in caplog.text


def test_background_repair_never_silently_discards_duplicate_json_keys(tmp_path):
    path = tmp_path / "mcp.json"
    text = (
        '{"mcpServers":{"mirea-lecture-assistant":'
        + json.dumps(registration())
        + '},"custom":{"value":1,"value":2}}'
    )
    path.write_text(text, encoding="utf-8")
    client = ai_clients.Client("fixture", "Fixture", [path])
    with pytest.raises(ai_clients.NotSafe):
        ai_clients.repair_links(client, LAUNCHER, PROFILE)
    assert path.read_text(encoding="utf-8") == text
