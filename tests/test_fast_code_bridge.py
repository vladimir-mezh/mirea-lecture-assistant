from __future__ import annotations

import json
import threading
import time
import urllib.request

import pytest

from mirea_lecture_assistant.code_bridge import CodeBridge
from mirea_lecture_assistant.email_otp import Code

ORIGIN = "chrome-extension://" + "b" * 32
A, B, C = "a" * 64, "b" * 64, "c" * 64


@pytest.fixture
def bridge():
    instance = CodeBridge()
    yield instance
    instance.stop()


def tagged(tag):
    code = Code("123456")
    code.tag = tag
    return code


def watch(bridge, key, tag=None):
    return bridge.request("/watch", key, origin=ORIGIN, tag=tag)


def poll(bridge, key, tag=None):
    return bridge.request("/poll", key, origin=ORIGIN, tag=tag)


def test_unique_matching_mark_is_not_blocked_by_another_login_tab(bridge):
    watch(bridge, A, "OLD")
    watch(bridge, B, "A2")
    bridge.publish(tagged("A2"))
    assert poll(bridge, B, "A2")["code"] == "123456"


def test_unknown_page_cannot_steal_code_from_a_known_matching_form(bridge):
    watch(bridge, A)
    watch(bridge, B, "A2")
    bridge.publish(tagged("A2"))
    assert poll(bridge, B, "A2")["code"] == "123456"


def test_cancel_releases_a_long_poll_without_leaving_a_ghost_waiter(bridge):
    watch(bridge, A, "OLD")
    completed = threading.Event()
    worker = threading.Thread(target=lambda: (poll(bridge, A, "OLD"), completed.set()), daemon=True)
    worker.start()
    bridge.request("/cancel", A, origin=ORIGIN)
    assert completed.wait(1)
    assert ORIGIN + ":" + A not in bridge.watchers
    watch(bridge, B, "A2")
    bridge.publish(tagged("A2"))
    assert poll(bridge, B, "A2")["code"] == "123456"


def test_two_matching_forms_remain_ambiguous_not_arbitrarily_chosen(bridge):
    watch(bridge, A, "A2")
    watch(bridge, B, "A2")
    bridge.publish(tagged("A2"))
    result, completed = [], threading.Event()
    worker = threading.Thread(
        target=lambda: (result.append(poll(bridge, A, "A2")), completed.set()), daemon=True
    )
    worker.start()
    assert not completed.wait(0.05)
    bridge.request("/cancel", A, origin=ORIGIN)
    assert completed.wait(1)
    assert result == [{}]
    assert poll(bridge, B, "A2")["code"] == "123456"


def test_acknowledgement_is_idempotent_and_contains_no_code_in_logs(bridge, caplog):
    delivered = []
    bridge.delivered = lambda: delivered.append(True)
    watch(bridge, A, "A2")
    bridge.publish(tagged("A2"))
    receipt = poll(bridge, A, "A2")["receipt"]
    assert bridge.request("/ack", A, receipt, ORIGIN)["accepted"]
    assert bridge.request("/ack", A, receipt, ORIGIN)["accepted"]
    assert delivered == [True]
    assert "123456" not in caplog.text and receipt not in caplog.text


def test_publish_wakes_the_waiter_without_waiting_for_poll_timeout(bridge):
    watch(bridge, A, "A2")
    result, done = [], threading.Event()
    worker = threading.Thread(
        target=lambda: (result.append(poll(bridge, A, "A2")), done.set()), daemon=True
    )
    worker.start()
    started = time.perf_counter()
    bridge.publish(tagged("A2"))
    assert done.wait(1)
    assert result[0]["code"] == "123456"
    assert time.perf_counter() - started < 1


def test_form_change_can_reclaim_an_unacknowledged_code(bridge):
    watch(bridge, A, "A2")
    bridge.publish(tagged("A2"))
    old = poll(bridge, A, "A2")
    bridge.request("/cancel", A, origin=ORIGIN)
    watch(bridge, C, "A2")
    new = poll(bridge, C, "A2")
    assert new["code"] == old["code"]
    assert not bridge.request("/ack", A, old["receipt"], ORIGIN)["accepted"]


def test_real_http_long_poll_is_released_immediately_by_a_new_code(bridge, monkeypatch):
    bridge.start()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def post(route, receipt=None):
        request = urllib.request.Request(
            f"http://127.0.0.1:{bridge.port}{route}",
            data=json.dumps({"watch": A, "tag": "A2", "receipt": receipt}).encode(),
            headers={
                "Origin": ORIGIN,
                "Authorization": "Bearer " + bridge.token,
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with opener.open(request, timeout=3) as response:
            return json.load(response)

    post("/watch")
    waiting, done = threading.Event(), threading.Event()
    original_wait = bridge.condition.wait

    def wait(timeout):
        waiting.set()
        return original_wait(timeout)

    monkeypatch.setattr(bridge.condition, "wait", wait)
    result = []
    worker = threading.Thread(
        target=lambda: (result.append(post("/poll")), done.set()), daemon=True
    )
    worker.start()
    assert waiting.wait(1)
    started = time.perf_counter()
    bridge.publish(tagged("A2"))
    assert done.wait(1)
    elapsed = (time.perf_counter() - started) * 1000
    assert post("/ack", result[0]["receipt"])["accepted"]
    print(f"synthetic HTTP delivery {elapsed:.1f} ms")
