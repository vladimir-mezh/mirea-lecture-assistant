from __future__ import annotations

import hashlib
import io
import json
import zipfile

import pytest

from mirea_lecture_assistant.mcp_access import ApiJob, validate_settings
from mirea_lecture_assistant.mcp_install import client_config, install_archive, installed

pytest_plugins = ["test_ui_reliability"]


def archive(version="0.1.0", **manifest_changes):
    files = {"MireaAssistantMcp.exe": b"MZ-fixture", "McpLauncher.exe": b"MZ-launcher"}
    manifest = {
        "version": version,
        "app_protocol": 1,
        "sha256": {name: hashlib.sha256(data).hexdigest() for name, data in files.items()},
    }
    manifest.update(manifest_changes)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as bundle:
        for name, data in files.items():
            bundle.writestr(name, data)
        bundle.writestr("manifest.json", json.dumps(manifest))
        bundle.writestr("README.md", "Fixture")
        bundle.writestr("LICENSE", "MIT Fixture")
    return out.getvalue()


def test_separate_install_and_stable_launcher_follow_independent_versions(tmp_path):
    root = tmp_path / "mcp"
    assert installed(root) is None
    install_archive(root, archive(), "0.1.0")
    assert installed(root)["version"] == "0.1.0"
    config = client_config(root, tmp_path)
    assert "token" not in json.dumps(config)
    install_archive(root, archive("0.2.0"), "0.2.0")
    assert installed(root)["version"] == "0.2.0"
    # Nothing runs the old version here, so it is removed right away.
    assert not (root / "versions" / "0.1.0").exists()
    assert config == client_config(root, tmp_path)


def test_bad_archive_cannot_replace_current_version(tmp_path):
    install_archive(tmp_path, archive(), "0.1.0")
    with pytest.raises(ValueError):
        install_archive(tmp_path, archive("0.2.0", app_protocol=2), "0.2.0")
    with pytest.raises(ValueError):
        install_archive(tmp_path, archive("0.2.0", sha256={}), "0.2.0")
    assert installed(tmp_path)["version"] == "0.1.0"


def test_archive_traversal_is_rejected(tmp_path):
    out = io.BytesIO(archive())
    with zipfile.ZipFile(out, "a") as bundle:
        bundle.writestr("../outside.exe", "bad")
    with pytest.raises(ValueError):
        install_archive(tmp_path, out.getvalue(), "0.1.0")
    assert not (tmp_path.parent / "outside.exe").exists()


@pytest.mark.parametrize(
    "values",
    [{"password": "secret"}, {"scan_interval": True}, {"scan_interval": 6}, {"group": "bad\ntext"}],
)
def test_settings_whitelist_and_strict_types(values):
    with pytest.raises(ValueError):
        validate_settings(values)


def enable(window):
    window.mcp_enabled.setChecked(True)
    # No external server/real-profile I/O in unit tests.
    window.mcp_access.server = object()


def rpc(window, method, params=None):
    job = ApiJob(method, params or {})
    window._mcp_rpc(job)
    assert job.done.is_set()
    return job.result


def test_mcp_page_follows_settings_and_is_disabled_by_default(window):
    assert window.nav_buttons[-1].text() == "MCP"
    assert window.pages.count() == 5
    assert not window.mcp_enabled.isChecked()
    assert not window.mcp_allow_changes.isChecked()


def test_reads_never_return_credentials_and_writes_require_permission(window):
    enable(window)
    window.db.set_setting("password", "must-not-leak")
    settings = rpc(window, "get_settings")
    assert "must-not-leak" not in json.dumps(settings)
    assert "error" in rpc(window, "update_settings", {"settings": {"mute_lecture": False}})
    assert window.db.get_setting("mute_lecture", True) is True


def test_allowed_write_applies_in_running_gui_but_does_not_overwrite_unsaved_edits(window):
    enable(window)
    window.mcp_allow_changes.setChecked(True)
    window.settings_dirty_label.setText("")
    result = rpc(window, "update_settings", {"settings": {"mute_lecture": False}})
    assert result["result"]["updated"] == ["mute_lecture"]
    assert not window.mute_lecture.isChecked()
    window.settings_dirty_label.setText("unsaved")
    assert "error" in rpc(window, "update_settings", {"settings": {"mute_lecture": True}})
    assert not window.db.get_setting("mute_lecture")


def test_cancelled_queued_request_cannot_change_settings(window):
    enable(window)
    window.mcp_allow_changes.setChecked(True)
    job = ApiJob("update_settings", {"settings": {"mute_lecture": False}}, cancelled=True)
    window._mcp_rpc(job)
    assert window.db.get_setting("mute_lecture", True) is True


def test_old_versions_and_interrupted_installs_are_removed(tmp_path):
    from mirea_lecture_assistant.mcp_install import clean_leftovers

    install_archive(tmp_path, archive(), "0.1.0")
    (tmp_path / "versions" / ".staging-0.1.5-x").mkdir()
    install_archive(tmp_path, archive("0.2.0"), "0.2.0")

    assert sorted(p.name for p in (tmp_path / "versions").iterdir()) == ["0.2.0"]
    assert installed(tmp_path)["version"] == "0.2.0"
    assert clean_leftovers(tmp_path)


def test_a_version_an_ai_client_still_runs_stays_until_it_is_released(tmp_path, monkeypatch):
    from pathlib import Path

    from mirea_lecture_assistant.mcp_install import clean_leftovers

    install_archive(tmp_path, archive(), "0.1.0")
    running = tmp_path / "versions" / "0.1.0" / "MireaAssistantMcp.exe"
    unlink = Path.unlink

    def locked(path, missing_ok=False):
        if path == running:
            raise PermissionError("in use")  # how Windows refuses a running program
        unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", locked)
    install_archive(tmp_path, archive("0.2.0"), "0.2.0")
    assert running.exists() and (running.parent / "manifest.json").exists()
    assert not clean_leftovers(tmp_path)

    monkeypatch.setattr(Path, "unlink", unlink)
    assert clean_leftovers(tmp_path)
    assert not running.parent.exists()


def test_a_new_launcher_replaces_the_running_one_by_rename(tmp_path, monkeypatch):
    from mirea_lecture_assistant.mcp_install import clean_leftovers

    install_archive(tmp_path, archive(), "0.1.0")
    launcher = tmp_path / "McpLauncher.exe"
    assert launcher.read_bytes() == b"MZ-launcher"

    files = {"MireaAssistantMcp.exe": b"MZ-fixture-2", "McpLauncher.exe": b"MZ-launcher-2"}
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as bundle:
        for name, data in files.items():
            bundle.writestr(name, data)
        manifest = {
            "version": "0.2.0",
            "app_protocol": 1,
            "sha256": {n: hashlib.sha256(d).hexdigest() for n, d in files.items()},
        }
        bundle.writestr("manifest.json", json.dumps(manifest))
        bundle.writestr("README.md", "Fixture")
        bundle.writestr("LICENSE", "MIT Fixture")
    install_archive(tmp_path, out.getvalue(), "0.2.0")

    assert launcher.read_bytes() == b"MZ-launcher-2"
    assert clean_leftovers(tmp_path)
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "McpLauncher.exe",
        "current.json",
        "versions",
    ]


def test_cleanup_without_a_valid_install_touches_nothing(tmp_path):
    from mirea_lecture_assistant.mcp_install import clean_leftovers

    (tmp_path / "versions" / "0.1.0").mkdir(parents=True)
    assert clean_leftovers(tmp_path)
    assert (tmp_path / "versions" / "0.1.0").exists()


def test_one_click_connects_a_found_ai_client(window, tmp_path, monkeypatch):
    from mirea_lecture_assistant import ai_clients, ui

    install_archive(window._mcp_root(), archive(), "0.1.0")
    config = tmp_path / "cursor-mcp.json"
    cursor = ai_clients.Client("cursor", "Cursor", [config])
    monkeypatch.setattr(ui.ai_clients, "supported", lambda: True)
    monkeypatch.setattr(ui.ai_clients, "detect", lambda: [cursor])
    monkeypatch.setattr(window.mcp_access, "start", lambda: None)
    monkeypatch.setattr(window, "_run", lambda function, done, *_a, **_k: done(function()))
    told = []
    monkeypatch.setattr(ui.QMessageBox, "information", lambda *args: told.append(args[2]))

    window._show_page(ui.MCP_PAGE)
    buttons = [b.text() for b in window.ai_clients_box.findChildren(ui.QPushButton)]
    assert buttons == ["Подключить"]
    window._connect_ai_client(cursor)

    assert window.mcp_enabled.isChecked()
    assert ai_clients.NAME in json.loads(config.read_text(encoding="utf-8"))["mcpServers"]
    assert told and told[0].startswith("Готово")
    window._refresh_ai_clients()
    labels = [label.text() for label in window.ai_clients_box.findChildren(ui.QLabel)]
    assert labels == ["Cursor — ✓ подключён"]
