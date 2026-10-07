"""Private loopback delivery to an explicitly installed browser extension.

Codes live only in memory for 90 seconds. No keyboard injection, access logs,
wildcard CORS, or web-page access. An ambiguous pair of login tabs gets no code.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
import shutil
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

log = logging.getLogger(__name__)


class CodeBridge:
    def __init__(self, delivered=lambda: None):
        self.delivered = delivered
        self.token = secrets.token_urlsafe(32)
        self.condition = threading.Condition()
        self.watchers: dict[str, float] = {}
        self.pending: tuple[str, str, float, str | None] | None = None
        self.server: ThreadingHTTPServer | None = None
        self.stopped = False
        self.expiry_timer: threading.Timer | None = None
        # The extension says hello every minute while its browser runs.
        self.extension_seen: float | None = None
        self.extension_browser = ""

    def start(self):
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def setup(self):
                super().setup()
                self.connection.settimeout(30)

            def log_message(self, *_args):
                pass  # URLs, bodies and credentials must never enter logs.

            def do_POST(self):
                try:
                    origin = self.headers.get("Origin", "")
                    auth = self.headers.get("Authorization", "")
                    valid = (
                        re.fullmatch(r"chrome-extension://[a-p]{32}", origin)
                        and secrets.compare_digest(auth, "Bearer " + bridge.token)
                        and self.headers.get("Host") == f"127.0.0.1:{bridge.port}"
                        and self.headers.get("Content-Type", "").split(";")[0] == "application/json"
                    )
                    size = int(self.headers.get("Content-Length", "0"))
                    if not valid or not 0 < size < 4096:
                        self.send_error(403)
                        return
                    data = json.loads(self.rfile.read(size))
                    key = data.get("watch", "")
                    if not isinstance(key, str) or not re.fullmatch(r"[a-f0-9]{64}", key):
                        self.send_error(400)
                        return
                    if self.path == "/hello":
                        bridge.greet(self.headers.get("User-Agent", ""))
                        result = {}
                    else:
                        result = bridge.request(self.path, key, data.get("receipt"), origin)
                    body = json.dumps(result).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Access-Control-Allow-Origin", origin)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except (ValueError, TypeError, AttributeError, OSError):
                    return

            def do_OPTIONS(self):
                # Only extension workers with host permission may access the
                # bridge; ordinary website preflights are deliberately refused.
                self.send_error(403)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        log.info("manual_code_bridge_started")

    @property
    def port(self):
        return self.server.server_port if self.server else 0

    def greet(self, user_agent: str) -> None:
        self.extension_browser = browser_name(user_agent)
        self.extension_seen = time.time()

    def prepare_extension(self, source: Path, target: Path):
        shutil.copytree(source, target, dirs_exist_ok=True)
        # The extension compares this stamp with the one it was loaded with
        # and reloads itself when they differ: updates need no clicks.
        manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
        manifest["version_name"] = f"{manifest['version']} {extension_stamp(source)}"
        (target / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        # Files a newer version no longer has would linger in the browser's copy.
        wanted = {path.relative_to(source) for path in source.rglob("*")}
        for path in sorted(target.rglob("*"), reverse=True):
            if path.relative_to(target) not in wanted:
                if path.is_dir():
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    path.unlink(missing_ok=True)
        # This is a local pairing key, NOT a mailbox password or an OTP. It is
        # not web-accessible and is regenerated on every application launch.
        config = {"port": self.port, "token": self.token}
        (target / "bridge-config.json").write_text(json.dumps(config), encoding="utf-8")

    def clear(self):
        with self.condition:
            self.pending = None
            if self.expiry_timer:
                self.expiry_timer.cancel()
                self.expiry_timer = None
            self.condition.notify_all()

    def publish(self, code: str):
        if not re.fullmatch(r"\d{6}", code):
            return
        with self.condition:
            receipt = secrets.token_hex(16)
            self.pending = (code, receipt, time.monotonic() + 90, None)
            if self.expiry_timer:
                self.expiry_timer.cancel()

            def expire():
                with self.condition:
                    if self.pending and self.pending[1] == receipt:
                        self.pending = None
                        self.condition.notify_all()

            self.expiry_timer = threading.Timer(90, expire)
            self.expiry_timer.daemon = True
            self.expiry_timer.start()
            self.condition.notify_all()

    def request(self, path: str, key: str, receipt=None, origin=""):
        watcher = origin + ":" + key
        with self.condition:
            if path == "/ack":
                if (
                    self.pending
                    and self.pending[1] == receipt
                    and self.pending[2] > time.monotonic()
                    and self.pending[3] == watcher
                ):
                    self.pending = None
                    self.watchers.pop(watcher, None)
                    self.delivered()
                return {}
            if path == "/cancel":
                self.watchers.pop(watcher, None)
                if self.pending and self.pending[3] == watcher:
                    self.pending = None
                return {}
            if path != "/poll" or self.stopped:
                return {}
            if watcher not in self.watchers and len(self.watchers) >= 32:
                return {}
            deadline = time.monotonic() + 20
            while not self.stopped:
                now = time.monotonic()
                self.watchers = {k: t for k, t in self.watchers.items() if t > now - 45}
                self.watchers[watcher] = now
                if self.pending and self.pending[2] <= now:
                    self.pending = None
                if self.pending and len(self.watchers) == 1:
                    code, receipt, expires, owner = self.pending
                    if owner is None or owner == watcher:
                        self.pending = (code, receipt, expires, watcher)
                        return {"code": code, "receipt": receipt}
                if now >= deadline:
                    return {}
                self.condition.wait(min(1, deadline - now))
            return {}

    def stop(self):
        with self.condition:
            self.stopped = True
            self.pending = None
            if self.expiry_timer:
                self.expiry_timer.cancel()
                self.expiry_timer = None
            self.watchers.clear()
            self.condition.notify_all()
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None


def extension_stamp(source: Path) -> str:
    """A short hash of the extension's own files (not of its pairing key)."""
    digest = hashlib.sha256()
    for path in sorted(source.rglob("*")):
        if path.is_file() and path.name != "bridge-config.json":
            digest.update(path.relative_to(source).as_posix().encode() + b"\0")
            digest.update(path.read_bytes())
    return digest.hexdigest()[:12]


def browser_name(user_agent: str) -> str:
    """Which Chromium browser it is, from the extension's own requests."""
    for marker, name in (
        ("YaBrowser/", "Яндекс Браузер"),
        ("Edg/", "Microsoft Edge"),
        ("OPR/", "Opera"),
        ("Vivaldi/", "Vivaldi"),
    ):
        if marker in user_agent:
            return name
    return "Google Chrome" if "Chrome/" in user_agent else "браузер"
