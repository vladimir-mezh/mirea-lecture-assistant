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
        req = urllib.request.Request(f"http://127.0.0.1:{config['port']}/rpc",
            data=json.dumps({"protocol": protocol, "client": {"id": "fixture"},
                             "method": "status", "params": {}}).encode(), headers=headers)
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
