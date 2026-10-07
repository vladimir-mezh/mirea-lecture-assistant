"""Install only the independent MCP's verified Windows release assets."""

from __future__ import annotations

import hashlib
import io
import json
import re
import secrets
import shutil
import tempfile
import zipfile
from pathlib import Path

from . import updater

REPOSITORY = "vladimir-mezh/mirea-lecture-assistant-mcp"
ASSET = "MireaAssistantMcp-windows-x64.zip"
API = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
TRUSTED = re.compile(rf"^https://github\.com/{re.escape(REPOSITORY)}/releases/download/")
MAX_SIZE = 80 * 1024 * 1024


def installed(root: Path):
    try:
        manifest = json.loads((root / "current.json").read_text(encoding="utf-8"))
        version = manifest["version"]
        if not re.fullmatch(r"\d+\.\d+\.\d+", version):
            return None
        if not (root / "versions" / version / "MireaAssistantMcp.exe").is_file():
            return None
        if not (root / "McpLauncher.exe").is_file():
            return None
        return manifest
    except (OSError, ValueError, KeyError, TypeError):
        return None


def latest():
    with updater._client(20) as client:
        response = client.get(API)
        response.raise_for_status()
        data = response.json()
    if data.get("draft") or data.get("prerelease"):
        raise ValueError("No stable MCP release")
    version = str(data.get("tag_name", "")).removeprefix("v")
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("Invalid MCP release version")
    assets = {asset["name"]: asset for asset in data.get("assets", [])}
    urls = [
        str(assets.get(name, {}).get("browser_download_url", ""))
        for name in [ASSET, ASSET + ".sha256"]
    ]
    if not all(TRUSTED.match(url) for url in urls):
        raise ValueError("MCP release must include archive and SHA-256")
    return {"version": version, "url": urls[0], "checksum_url": urls[1]}


def install(root: Path, release: dict):
    if not all(TRUSTED.match(str(release.get(key, ""))) for key in ["url", "checksum_url"]):
        raise ValueError("Untrusted MCP download")
    with updater._client(60) as client:
        checksum = client.get(release["checksum_url"])
        checksum.raise_for_status()
        digest = checksum.text.split()[0].lower()
        if not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise ValueError("Invalid MCP checksum")
        archive = bytearray()
        with client.stream("GET", release["url"]) as response:
            response.raise_for_status()
            for chunk in response.iter_bytes():
                archive.extend(chunk)
                if len(archive) > MAX_SIZE:
                    raise ValueError("MCP archive exceeds size limit")
    if hashlib.sha256(archive).hexdigest() != digest:
        raise ValueError("MCP archive checksum mismatch")
    return install_archive(root, bytes(archive), release["version"])


def install_archive(root: Path, archive: bytes, expected_version: str):
    if not re.fullmatch(r"\d+\.\d+\.\d+", expected_version):
        raise ValueError("Invalid version")
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        names = [item.filename for item in bundle.infolist()]
        expected = {
            "MireaAssistantMcp.exe",
            "McpLauncher.exe",
            "manifest.json",
            "README.md",
            "LICENSE",
        }
        if set(names) != expected or len(names) != len(expected):
            raise ValueError("Unexpected archive contents")
        if sum(item.file_size for item in bundle.infolist()) > MAX_SIZE:
            raise ValueError("MCP unpacked size limit exceeded")
        manifest = json.loads(bundle.read("manifest.json"))
        if (
            not isinstance(manifest, dict)
            or manifest.get("version") != expected_version
            or type(manifest.get("app_protocol")) is not int
            or manifest.get("app_protocol") != 1
        ):
            raise ValueError("MCP release is incompatible with this application")
        files = {name: bundle.read(name) for name in expected}
    for name in ["MireaAssistantMcp.exe", "McpLauncher.exe"]:
        if not files[name].startswith(b"MZ"):
            raise ValueError("Not a Windows executable")
        if hashlib.sha256(files[name]).hexdigest() != manifest.get("sha256", {}).get(name):
            raise ValueError("MCP executable checksum mismatch")
    destination = root / "versions" / expected_version
    if destination.exists():
        existing = destination / "MireaAssistantMcp.exe"
        if (
            not existing.is_file()
            or hashlib.sha256(existing.read_bytes()).hexdigest()
            != manifest["sha256"][existing.name]
        ):
            raise ValueError("Version directory already exists with different contents")
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(
            tempfile.mkdtemp(prefix=f".staging-{expected_version}-", dir=destination.parent)
        )
        for name in ["MireaAssistantMcp.exe", "manifest.json", "README.md", "LICENSE"]:
            (staging / name).write_bytes(files[name])
        staging.replace(destination)
    _place_launcher(root, files["McpLauncher.exe"])
    temporary = root / "current.tmp"
    temporary.write_text(json.dumps(manifest), encoding="utf-8")
    temporary.replace(root / "current.json")
    clean_leftovers(root)
    return manifest


def _place_launcher(root: Path, data: bytes) -> None:
    """Put the release's launcher at the stable path the AI client runs.

    A connected client keeps the old launcher running, and Windows lets a running
    program be renamed, not overwritten: the old one moves aside to a ``.old``
    name that ``clean_leftovers`` removes once the client lets go of it.
    """
    target = root / "McpLauncher.exe"
    try:
        if target.is_file() and target.read_bytes() == data:
            return
        fresh = root / "McpLauncher.exe.new"
        fresh.write_bytes(data)
        if target.exists():
            target.replace(root / f"McpLauncher.exe.{secrets.token_hex(4)}.old")
        fresh.replace(target)
    except OSError:
        # The launcher only reads current.json; the old one still starts the new
        # version, so a failed swap waits for the next install.
        if not target.is_file():
            raise


def clean_leftovers(root: Path) -> bool:
    """Remove MCP versions other than the current one and interrupted installs.

    A version an AI client still runs is locked on Windows and stays until a
    later pass. Returns True when nothing is left to remove.
    """
    current = installed(root)
    if current is None:
        return True  # nothing verified to keep: never guess which version is in use
    done = True
    versions = root / "versions"
    for entry in versions.iterdir() if versions.is_dir() else ():
        if entry.name == current["version"]:
            continue
        try:
            if entry.is_dir() and not entry.name.startswith(".staging-"):
                # The program first: if it is running, the folder stays whole.
                (entry / "MireaAssistantMcp.exe").unlink(missing_ok=True)
            if entry.is_dir():
                shutil.rmtree(entry)
            else:
                entry.unlink()
        except OSError:
            done = False
    for leftover in [*root.glob("McpLauncher.exe.*.old"), root / "McpLauncher.exe.new"]:
        try:
            leftover.unlink(missing_ok=True)
        except OSError:
            done = False
    return done


def client_config(root: Path, profile: Path):
    return {
        "mcpServers": {
            "mirea-lecture-assistant": {
                "command": str(root / "McpLauncher.exe"),
                "args": ["--profile", str(profile)],
            }
        }
    }
