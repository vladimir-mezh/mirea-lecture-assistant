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
    manifest = {"version": version, "app_protocol": 1,
                "sha256": {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}}
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
    assert (root / "versions" / "0.1.0" / "MireaAssistantMcp.exe").exists()
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


@pytest.mark.parametrize("values", [{"password": "secret"}, {"scan_interval": True},
                                    {"scan_interval": 6}, {"group": "bad\ntext"}])
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
