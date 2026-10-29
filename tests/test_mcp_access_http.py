from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from mirea_lecture_assistant.mcp_access import McpAccess


def test_private_api_rejects_web_requests_and_bad_keys(tmp_path, caplog):
    dispatched = []

    def dispatch(job):
        dispatched.append(job.method)
        job.result = {"result": {"fixture": True}}
        job.done.set()

    access = McpAccess(tmp_path, dispatch)
    access.start()
    config = json.loads((tmp_path / "mcp-connection.json").read_text())

    def request(token, origin=None, protocol=1):
        headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
        if origin:
            headers["Origin"] = origin
        req = urllib.request.Request(
            f"http://127.0.0.1:{config['port']}/rpc",
            data=json.dumps(
                {
                    "protocol": protocol,
                    "client": {"id": "fixture"},
                    "method": "status",
                    "params": {},
                }
            ).encode(),
            headers=headers,
        )
        return urllib.request.urlopen(req, timeout=2)

    try:
        for key, origin in [("wrong", None), (config["token"], "https://example.test")]:
            with pytest.raises(urllib.error.HTTPError) as exc:
                request(key, origin)
            assert exc.value.code == 403
        assert dispatched == []
        with request(config["token"]) as response:
            assert json.load(response)["result"]["fixture"]
        assert dispatched == ["status"]
        assert config["token"] not in caplog.text
        assert len(access.connected_clients()) == 1
    finally:
        access.stop()
    assert not (tmp_path / "mcp-connection.json").exists()


def test_sessions_are_grouped_with_active_connection_preferred_and_independent_leases(
    tmp_path, monkeypatch
):
    from mirea_lecture_assistant import mcp_access

    monkeypatch.setattr(mcp_access.time, "monotonic", lambda: 100)
    access = McpAccess(tmp_path, lambda _job: None)
    access.clients = {
        "active": {"name": "Codex", "version": "0.2.0", "state": "connected", "seen": 80},
        "waiting1": {"name": "Codex", "version": "old", "state": "waiting", "seen": 99},
        "waiting2": {"name": " codex ", "version": "old", "state": "waiting", "seen": 98},
        "other": {"name": "Claude", "version": "0.2.0", "state": "connected", "seen": 95},
        "stale": {"name": "Gone", "version": "old", "state": "connected", "seen": 64},
    }
    clients = access.connected_clients()
    assert [client["name"] for client in clients] == ["Claude", "Codex"]
    assert clients[1]["state"] == "connected" and clients[1]["version"] == "0.2.0"
    assert set(access.clients) == {"active", "waiting1", "waiting2", "other"}
    del access.clients["waiting1"]
    assert len(access.connected_clients()) == 2
    monkeypatch.setattr(mcp_access.time, "monotonic", lambda: 116)
    clients = access.connected_clients()
    assert len(clients) == 2
    assert clients[1]["state"] == "waiting"
    monkeypatch.setattr(mcp_access.time, "monotonic", lambda: 140)
    assert access.connected_clients() == []


def test_stale_sessions_free_the_registration_limit_before_a_new_heartbeat(tmp_path, monkeypatch):
    from mirea_lecture_assistant import mcp_access

    monkeypatch.setattr(mcp_access.time, "monotonic", lambda: 100)
    access = McpAccess(tmp_path, lambda _job: None)
    access.start()
    access.clients = {str(i): {"name": "old", "seen": 0} for i in range(32)}
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{access.server.server_port}/rpc",
            data=json.dumps(
                {"protocol": 1, "client": {"id": "new", "name": "Codex"}, "method": "heartbeat"}
            ).encode(),
            headers={"Authorization": "Bearer " + access.token, "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            assert json.load(response)["ok"]
        assert set(access.clients) == {"new"}
    finally:
        access.stop()
