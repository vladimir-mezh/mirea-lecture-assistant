"""Versioned local API for the separately installed MCP, not an embedded AI."""

from __future__ import annotations

import json
import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

log = logging.getLogger(__name__)
PROTOCOL = 1
SETTINGS = {
    "group": (str, "", None),
    "student_name": (str, "", None),
    "join_before": (int, 5, (0, 30)),
    "scan_interval": (int, 2, (1, 5)),
    "direct_capture": (bool, True, None),
    "hd_capture": (bool, True, None),
    "compact_window": (bool, True, None),
    "mute_lecture": (bool, True, None),
    "minimize_on_open": (bool, True, None),
    "minimize_after_qr": (bool, True, None),
    "close_tab_after": (bool, True, None),
    "auto_login": (bool, True, None),
    "copy_manual_codes": (bool, True, None),
    "type_manual_codes": (bool, True, None),
}


def validate_settings(values):
    if not isinstance(values, dict) or not values or len(values) > len(SETTINGS):
        raise ValueError("Expected a nonempty settings object")
    for key, value in values.items():
        if key not in SETTINGS:
            raise ValueError("Setting is not available through MCP")
        kind, _, bounds = SETTINGS[key]
        if type(value) is not kind:
            raise ValueError(f"Invalid type for {key}")
        if kind is str and (len(value) > 200 or any(ord(c) < 32 for c in value)):
            raise ValueError(f"Invalid text for {key}")
        if bounds and not bounds[0] <= value <= bounds[1]:
            raise ValueError(f"Invalid range for {key}")
    return values


@dataclass
class ApiJob:
    method: str
    params: dict
    done: threading.Event = field(default_factory=threading.Event)
    result: dict = field(default_factory=dict)
    cancelled: bool = False


class McpAccess:
    def __init__(self, root: Path, dispatch):
        self.root, self.dispatch = root, dispatch
        self.server = None
        self.token = secrets.token_urlsafe(32)
        self.clients: dict[str, dict] = {}
        self.lock = threading.Lock()

    def start(self):
        if self.server:
            return
        access = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def setup(self):
                super().setup()
                self.connection.settimeout(15)

            def do_POST(self):
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    allowed = (
                        self.path == "/rpc"
                        and not self.headers.get("Origin")
                        and self.headers.get("Host") == f"127.0.0.1:{access.server.server_port}"
                        and secrets.compare_digest(
                            self.headers.get("Authorization", ""), "Bearer " + access.token
                        )
                        and self.headers.get("Content-Type") == "application/json"
                    )
                    if not allowed or not 0 < length <= 16384:
                        self.send_error(403)
                        return
                    data = json.loads(self.rfile.read(length))
                    if not isinstance(data, dict) or data.get("protocol") != PROTOCOL:
                        self.send_error(400)
                        return
                    client = data.get("client", {})
                    if not isinstance(client, dict):
                        raise TypeError()
                    client_id = str(client.get("id", ""))
                    if not client_id or len(client_id) > 80:
                        raise ValueError()
                    method = data.get("method")
                    with access.lock:
                        if method == "disconnect":
                            access.clients.pop(client_id, None)
                        elif len(access.clients) < 32 or client_id in access.clients:
                            access.clients[client_id] = {
                                "name": str(client.get("name", "MCP"))[:80],
                                "version": str(client.get("version", "unknown"))[:30],
                                "state": "connected"
                                if client.get("state") == "connected"
                                else "waiting",
                                "seen": time.monotonic(),
                            }
                    if method in {"heartbeat", "disconnect"}:
                        result = {"protocol": PROTOCOL, "ok": True}
                    else:
                        params = data.get("params", {})
                        if not isinstance(params, dict):
                            raise TypeError()
                        job = ApiJob(str(method), params)
                        access.dispatch(job)
                        if not job.done.wait(_answer_within(job)):
                            job.cancelled = True
                            result = {"error": "Application is busy; request cancelled"}
                        else:
                            result = job.result
                    body = json.dumps(result, ensure_ascii=False).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except (ValueError, TypeError, OSError, AttributeError):
                    return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / "mcp-connection.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {"protocol": PROTOCOL, "port": self.server.server_port, "token": self.token}
            ),
            encoding="utf-8",
        )
        temporary.replace(path)
        log.info("mcp_access_started protocol=%s", PROTOCOL)

    def connected_clients(self):
        now = time.monotonic()
        with self.lock:
            self.clients = {k: v for k, v in self.clients.items() if now - v["seen"] < 35}
            return list(self.clients.values())

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
        try:
            (self.root / "mcp-connection.json").unlink(missing_ok=True)
        except OSError:
            log.warning("mcp_connection_cleanup_failed")
        with self.lock:
            self.clients.clear()


def _answer_within(job: ApiJob) -> float:
    """Seconds to wait for the window: a waited check takes as long as it asks for."""
    if job.method == "wait_and_check":
        seconds = job.params.get("seconds", 60)
        return 20 + (seconds if type(seconds) is int and 0 <= seconds <= 120 else 0)
    if job.method == "check_health":
        return 20  # with a network check of a few seconds
    return 8
