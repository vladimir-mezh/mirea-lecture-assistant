"""Updates from the project's GitHub releases, installed with one click.

The executable carries no user data: the database, the session and the saved
passwords live in the user's profile and the Windows Credential Manager, so
replacing the file never touches them. Windows cannot overwrite a running
executable but lets it be renamed: the new file takes its name, is started,
and the running copy hands over to it (see ``app.ask_running_copy_to_show``).
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

REPOSITORY = "vladimir-mezh/mirea-lecture-assistant"
LATEST_RELEASE_API = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPOSITORY}/releases/latest"
EXE_NAME = "MireaLectureAssistant.exe"
CHECKSUM_NAME = EXE_NAME + ".sha256"
# Only files from the project's own releases are ever installed.
TRUSTED_DOWNLOAD = re.compile(rf"^https://github\.com/{re.escape(REPOSITORY)}/releases/download/")


class UpdateError(RuntimeError):
    pass


@dataclass(frozen=True)
class Release:
    version: str
    notes: str
    exe_url: str
    size: int
    checksum_url: str | None


def version_tuple(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", text or "")[:3])


def is_newer(candidate: str, current: str) -> bool:
    return version_tuple(candidate) > version_tuple(current)


def can_self_update() -> bool:
    """Only the built Windows program replaces itself; a source checkout does not."""
    return bool(getattr(sys, "frozen", False)) and sys.platform == "win32"


def current_executable() -> Path:
    return Path(sys.executable).resolve()


def _client(timeout: float):
    import httpx

    return httpx.Client(
        follow_redirects=True,
        timeout=httpx.Timeout(timeout, connect=10.0),
        headers={"User-Agent": "MireaLectureAssistant-updater"},
    )


def parse_release(payload: dict) -> Release | None:
    """The release's version, its notes and the executable, or None if it has none."""
    version = str(payload.get("tag_name") or "").lstrip("v")
    assets = {asset.get("name"): asset for asset in payload.get("assets") or []}
    exe = assets.get(EXE_NAME)
    if not version or not exe:
        return None
    exe_url = str(exe.get("browser_download_url") or "")
    if not TRUSTED_DOWNLOAD.match(exe_url):
        return None
    checksum = assets.get(CHECKSUM_NAME)
    checksum_url = str(checksum.get("browser_download_url") or "") if checksum else None
    if checksum_url and not TRUSTED_DOWNLOAD.match(checksum_url):
        checksum_url = None
    return Release(
        version=version,
        notes=_first_section(str(payload.get("body") or "")),
        exe_url=exe_url,
        size=int(exe.get("size") or 0),
        checksum_url=checksum_url,
    )


def _first_section(body: str) -> str:
    """The notes of this version only; release bodies carry the whole changelog."""
    parts = re.split(r"(?m)^# ", body)
    section = parts[1] if len(parts) > 1 else body
    lines = section.splitlines()[1:] if len(parts) > 1 else section.splitlines()
    return "\n".join(lines).strip()[:1500]


def latest_release(timeout: float = 15.0) -> Release | None:
    with _client(timeout) as client:
        response = client.get(LATEST_RELEASE_API, headers={"Accept": "application/vnd.github+json"})
        response.raise_for_status()
        return parse_release(response.json())


def download(release: Release, folder: Path, timeout: float = 60.0) -> Path:
    """Fetch and verify the new executable; returns the verified file."""
    if not release.checksum_url:
        raise UpdateError("У этой версии нет контрольной суммы — скачайте её со страницы релиза")
    folder.mkdir(parents=True, exist_ok=True)
    partial = folder / (EXE_NAME + ".download")
    ready = folder / (EXE_NAME + ".new")
    digest = hashlib.sha256()
    written = 0
    with _client(timeout) as client:
        expected = client.get(release.checksum_url)
        expected.raise_for_status()
        match = re.search(r"\b[0-9a-fA-F]{64}\b", expected.text)
        if not match:
            raise UpdateError("Контрольная сумма обновления не читается")
        with client.stream("GET", release.exe_url) as response:
            response.raise_for_status()
            with partial.open("wb") as file:
                for chunk in response.iter_bytes(1 << 16):
                    file.write(chunk)
                    digest.update(chunk)
                    written += len(chunk)
    if release.size and written != release.size:
        partial.unlink(missing_ok=True)
        raise UpdateError("Обновление скачалось не полностью; попробуйте ещё раз")
    if digest.hexdigest().lower() != match.group(0).lower():
        partial.unlink(missing_ok=True)
        raise UpdateError("Скачанный файл не совпал с контрольной суммой; он удалён")
    os.replace(partial, ready)
    log.info("update_downloaded version=%s bytes=%s", release.version, written)
    return ready


def install(new_exe: Path, current: Path) -> Path:
    """Put the new file in place of the running one; returns the path to start."""
    previous = current.with_name(current.name + ".old")
    previous.unlink(missing_ok=True)
    os.replace(current, previous)
    try:
        os.replace(new_exe, current)
    except OSError:
        os.replace(previous, current)
        raise
    log.info("update_installed path=%s", current)
    return current


def start(executable: Path) -> None:
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    from .supervisor import child_environment

    subprocess.Popen(
        [str(executable)],
        close_fds=True,
        creationflags=flags,
        cwd=str(executable.parent),
        # A separate copy with its own watchdog, not a child of this one's.
        env=child_environment(),
    )


def clean_leftovers(current: Path, *, partial: bool = False) -> bool:
    """Delete the replaced executable; True once it is gone.

    ``partial`` also removes an unfinished ``.download`` or ``.new`` (an update
    interrupted by a crash or power loss, ~50 MB each). Only at startup, before an
    update of this copy can have started: never while a download may be running.
    """
    if partial:
        for suffix in (".download", ".new"):
            try:
                current.with_name(current.name + suffix).unlink(missing_ok=True)
            except OSError:
                pass
    previous = current.with_name(current.name + ".old")
    try:
        existed = previous.exists()
        previous.unlink(missing_ok=True)
        if existed:
            log.info("update_old_version_removed path=%s", previous)
        return True
    except OSError:
        return False  # bootloader/antivirus may still hold it; a timer retries
