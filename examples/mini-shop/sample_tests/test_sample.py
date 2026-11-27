"""示例项目的参考用例：按 api.md 写的接口用例长什么样。

框架自检（python qa.py selftest）会把它放进临时工作区跑一遍，确认“启动服务 → 跑用例 → 出摘要”这条路是通的。
自己的项目里，用例由 AI 按接口文档和知识库写，人审清单和断言。
"""
import os
import threading
import uuid

import httpx
import pytest

URL = os.environ.get("QA_TARGET_URL", "http://127.0.0.1:8000")


def op() -> str:
    return "op-" + uuid.uuid4().hex[:12]


@pytest.fixture()
def c():
    with httpx.Client(base_url=URL, timeout=10) as client:
        assert client.post("/reset").json()["ok"]
        yield client


def acc(c, pid):
    return c.get(f"/accounts/{pid}").json()["account"]


def claim(c, **body):
    return c.post("/ads/claim", json=body).json()


def advance(c, sec):
    return c.post("/debug/clock", json={"advance_sec": sec}).json()


# 结算双倍封顶的两边
def test_double_settle_cap(c):
    r = claim(c, player="A", placement="doubleSettle", operation_id=op(), free=True, session_id="S1")
    assert r["ok"] and r["grant"]["amount"] == 300          # min(500, 3 × 100)
    r = claim(c, player="A", placement="doubleSettle", operation_id=op(), session_id="S2")
    assert r["ok"] and r["grant"]["amount"] == 120          # 封顶不起作用的一边
    assert acc(c, "A")["gold"] == 1000 + 300 + 120


# 同一个 operation_id 重放
def test_replay_same_operation(c):
    oid = op()
    first = claim(c, player="A", placement="stamina", operation_id=oid)
    assert first["ok"]
    again = claim(c, player="A", placement="stamina", operation_id=oid)
    assert again == first
    advance(c, 20)
    later = claim(c, player="A", placement="stamina", operation_id=oid)
    assert later == first
    a = acc(c, "A")
    assert a["stamina"] == 6 and a["used"]["stamina"]["ad"] == 1
    assert len([e for e in c.get("/ledger", params={"player": "A"}).json()["entries"] if e["operation_id"] == oid]) == 1


# 回包丢失后用同一个 operation_id 重试
def test_drop_response_retry(c):
    oid = op()
    body = {"player": "A", "placement": "stamina", "operation_id": oid}
    with pytest.raises(httpx.TimeoutException):
        c.post("/ads/claim", json=body, headers={"X-Fault": "drop-response"}, timeout=1)
    r = claim(c, **body)
    assert r["ok"] and r["grant"]["amount"] == 1
    assert acc(c, "A")["stamina"] == 6


def test_operation_conflict(c):
    oid = op()
    assert claim(c, player="A", placement="stamina", operation_id=oid)["ok"]
    r = claim(c, player="A", placement="doubleSettle", operation_id=oid, session_id="S1")
    assert r == {"ok": False, "error": "OPERATION_CONFLICT"}
    assert acc(c, "A")["gold"] == 1000


# 领别人那一场的双倍
def test_ownership(c):
    r = claim(c, player="B", placement="doubleSettle", operation_id=op(), free=True, session_id="S1")
    assert r == {"ok": False, "error": "NO_SESSION"}
    assert acc(c, "B")["gold"] == 1000


def test_preconditions(c):
    assert claim(c, player="B", placement="doubleSettle", operation_id=op(), free=True, session_id="S3")["error"] == "NOTHING_TO_DOUBLE"
    assert claim(c, player="A", placement="doubleSettle", operation_id=op(), free=True, session_id="S4")["error"] == "NOT_SETTLED"
    assert claim(c, player="A", placement="doubleSettle", operation_id=op(), free=True)["error"] == "NO_CONTEXT"
    assert claim(c, player="A", placement="doubleSettle", operation_id=op(), free=True, session_id="S1")["ok"]
    assert claim(c, player="A", placement="doubleSettle", operation_id=op(), session_id="S1")["error"] == "ALREADY_DOUBLED"


def test_interval_and_free(c):
    assert claim(c, player="A", placement="stamina", operation_id=op())["ok"]
    assert claim(c, player="A", placement="stamina", operation_id=op())["error"] == "TOO_FREQUENT"
    # 免费次数不受间隔限制
    assert claim(c, player="A", placement="doubleSettle", operation_id=op(), free=True, session_id="S1")["ok"]
    assert claim(c, player="A", placement="doubleSettle", operation_id=op(), free=True, session_id="S2")["error"] == "NO_FREE_LEFT"
    advance(c, 15)
    assert claim(c, player="A", placement="stamina", operation_id=op())["ok"]


def test_daily_cap_and_rollover(c):
    for _ in range(6):
        assert claim(c, player="A", placement="stamina", operation_id=op())["ok"]
        advance(c, 15)
    assert claim(c, player="A", placement="stamina", operation_id=op())["error"] == "CAP_REACHED"
    left = c.get("/debug/clock").json()["seconds_to_midnight"]
    advance(c, left + 1)
    assert acc(c, "A")["used"]["stamina"]["ad"] == 0
    assert claim(c, player="A", placement="stamina", operation_id=op())["ok"]


def test_client_amount_ignored(c):
    r = claim(c, player="A", placement="doubleSettle", operation_id=op(), free=True, session_id="S1", amount=99999)
    assert r["ok"] and r["grant"]["amount"] == 300


def test_shop_rules(c):
    buy = lambda tier: c.post("/shop/buy", json={"player": "A", "kind": "rod", "tier": tier}).json()
    assert buy(1)["error"] == "GEAR_OWNED"
    assert buy(4)["error"] == "GEAR_LOCKED"
    assert buy(3)["error"] == "NO_GOLD"
    r = buy(2)
    assert r["ok"] and r["price"] == 500 and r["account"]["gold"] == 500 and r["account"]["rod"] == 2
    assert buy(2)["error"] == "GEAR_OWNED"
    assert acc(c, "A")["gold"] == 500


# 并发买同一档
def test_concurrent_buy(c):
    results = []

    def worker():
        with httpx.Client(base_url=URL, timeout=10) as cc:
            results.append(cc.post("/shop/buy", json={"player": "A", "kind": "rod", "tier": 2}).json())

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(r["ok"] for r in results) == [False, True]
    assert acc(c, "A")["gold"] == 500
