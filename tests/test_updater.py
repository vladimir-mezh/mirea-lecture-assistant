from __future__ import annotations

import hashlib

import httpx
import pytest

from mirea_lecture_assistant import updater

BASE = "https://github.com/vladimir-mezh/mirea-lecture-assistant/releases/download/v9.9.9/"
NEW_EXE = b"MZ" + b"new build " * 1000


def _payload(exe_url=BASE + updater.EXE_NAME, checksum=True):
    assets = [{"name": updater.EXE_NAME, "browser_download_url": exe_url, "size": len(NEW_EXE)}]
    if checksum:
        assets.append(
            {"name": updater.CHECKSUM_NAME, "browser_download_url": BASE + updater.CHECKSUM_NAME}
        )
    return {
        "tag_name": "v9.9.9",
        "body": "# 9.9.9\n\n- Новое\n\n# 9.9.8\n\n- Старое",
        "assets": assets,
    }


def test_a_release_is_read_with_only_its_own_notes():
    release = updater.parse_release(_payload())

    assert release.version == "9.9.9"
    assert release.notes == "- Новое"
    assert release.checksum_url.endswith(updater.CHECKSUM_NAME)


def test_a_download_from_anywhere_else_is_never_accepted():
    assert updater.parse_release(_payload(exe_url="https://evil.example/app.exe")) is None


def test_versions_compare_as_numbers():
    assert updater.is_newer("0.2.10", "0.2.9")
    assert not updater.is_newer("0.2.7", "0.2.7")


def _serve(monkeypatch, checksum: str):
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(".sha256"):
            return httpx.Response(200, text=f"{checksum}  {updater.EXE_NAME}\n")
        return httpx.Response(200, content=NEW_EXE)

    monkeypatch.setattr(
        updater, "_client", lambda timeout: httpx.Client(transport=httpx.MockTransport(handle))
    )


def test_a_verified_download_is_ready_to_install(tmp_path, monkeypatch):
    _serve(monkeypatch, hashlib.sha256(NEW_EXE).hexdigest())

    ready = updater.download(updater.parse_release(_payload()), tmp_path)

    assert ready.read_bytes() == NEW_EXE


def test_a_file_that_does_not_match_its_checksum_is_thrown_away(tmp_path, monkeypatch):
    _serve(monkeypatch, "0" * 64)

    with pytest.raises(updater.UpdateError, match="контрольной суммой"):
        updater.download(updater.parse_release(_payload()), tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_a_release_without_a_checksum_is_not_installed_automatically(tmp_path):
    with pytest.raises(updater.UpdateError):
        updater.download(updater.parse_release(_payload(checksum=False)), tmp_path)


def test_the_new_file_takes_the_place_of_the_running_one(tmp_path):
    current = tmp_path / updater.EXE_NAME
    current.write_bytes(b"old build")
    new = tmp_path / (updater.EXE_NAME + ".new")
    new.write_bytes(NEW_EXE)

    assert updater.install(new, current) == current
    assert current.read_bytes() == NEW_EXE
    assert (tmp_path / (updater.EXE_NAME + ".old")).read_bytes() == b"old build"

    updater.clean_leftovers(current)
    assert sorted(path.name for path in tmp_path.iterdir()) == [updater.EXE_NAME]


def test_the_new_version_is_started_as_a_separate_copy(tmp_path, monkeypatch):
    monkeypatch.setenv("MIREA_ASSISTANT_CHILD", "1")
    monkeypatch.setenv("_PYI_APPLICATION_HOME_DIR", "old-bundle")
    calls = []
    monkeypatch.setattr(updater.subprocess, "Popen", lambda *args, **kw: calls.append((args, kw)))
    executable = tmp_path / updater.EXE_NAME

    updater.start(executable)

    args, kwargs = calls[0]
    assert args[0] == [str(executable)]
    assert kwargs["env"]["PYINSTALLER_RESET_ENVIRONMENT"] == "1"
    assert "MIREA_ASSISTANT_CHILD" not in kwargs["env"]
    assert kwargs["cwd"] == str(tmp_path)


def test_old_version_cleanup_preserves_an_active_download(tmp_path):
    current = tmp_path / updater.EXE_NAME
    current.write_bytes(b"running")
    old = current.with_name(current.name + ".old")
    old.write_bytes(b"old")
    for suffix in (".download", ".new"):
        current.with_name(current.name + suffix).write_bytes(b"in progress")
    assert updater.clean_leftovers(current)
    assert not old.exists()
    assert current.read_bytes() == b"running"
    assert current.with_name(current.name + ".download").read_bytes() == b"in progress"
    assert current.with_name(current.name + ".new").read_bytes() == b"in progress"


def test_locked_old_version_is_retried_after_it_is_released(tmp_path, monkeypatch):
    current = tmp_path / updater.EXE_NAME
    old = current.with_name(current.name + ".old")
    old.write_bytes(b"old")
    unlink = type(old).unlink
    locked = [True]

    def guarded(path, **kwargs):
        if path == old and locked[0]:
            raise PermissionError("old bootloader still exiting")
        return unlink(path, **kwargs)

    monkeypatch.setattr(type(old), "unlink", guarded)
    assert not updater.clean_leftovers(current)
    assert old.exists()
    locked[0] = False
    assert updater.clean_leftovers(current)
    assert not old.exists()
