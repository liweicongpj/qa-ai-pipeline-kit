"""示例被测服务：迷你“广告领奖 + 渔具店”。

用来第一次上手时把整条管线跑通（python qa.py init <文件夹> --example 会把它配成被测服务）。
接口和规则见同一文件夹的 api.md。启动：

    python server.py --port 8000
"""
from __future__ import annotations

import argparse
import asyncio
import math
import threading
import time
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, Request
from starlette.concurrency import run_in_threadpool

BJ = timezone(timedelta(hours=8))
# /reset 后服务器时钟从这个时刻开始走（北京时间），跟真实时间同速流逝；/debug/clock 可以往后拨
CLOCK_START = datetime(2026, 11, 26, 12, 0, 0, tzinfo=BJ).timestamp()

PLACEMENTS = {
    "doubleSettle": {"free": 1, "ad": 3},
    "stamina": {"free": 0, "ad": 6},
}
ROD_TIERS = {1: {"price": 0, "unlock": 1}, 2: {"price": 500, "unlock": 2},
             3: {"price": 3000, "unlock": 4}, 4: {"price": 5500, "unlock": 7}}
AD_MIN_INTERVAL_SEC = 15
DOUBLE_CAP_FEE_MULT = 3

app = FastAPI(title="mini-shop", docs_url="/docs", redoc_url=None)
LOCK = threading.RLock()
S: dict = {}


def now() -> float:
    return CLOCK_START + (time.monotonic() - S["started"]) + S["offset"]


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, BJ).isoformat(timespec="seconds")


def day_of(t: float) -> str:
    return datetime.fromtimestamp(t, BJ).strftime("%Y-%m-%d")


def seconds_to_midnight(t: float) -> float:
    d = datetime.fromtimestamp(t, BJ)
    nxt = (d + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return round((nxt - d).total_seconds(), 3)


def fresh_used() -> dict:
    return {p: {"free": 0, "ad": 0} for p in PLACEMENTS}


def reset_store() -> None:
    S.clear()
    S["started"] = time.monotonic()
    S["offset"] = 0.0
    today = day_of(now())
    S["accounts"] = {
        pid: {"id": pid, "gold": 1000, "stamina": 5, "pond_level": 4, "rod": 1,
              "date": today, "used": fresh_used(), "last_ad_at": None}
        for pid in ("A", "B", "C")
    }
    S["sessions"] = {
        "S1": {"id": "S1", "owner": "A", "settled": True, "gross": 500, "fee": 100, "doubled": False},
        "S2": {"id": "S2", "owner": "A", "settled": True, "gross": 120, "fee": 100, "doubled": False},
        "S3": {"id": "S3", "owner": "B", "settled": True, "gross": 0, "fee": 100, "doubled": False},
        "S4": {"id": "S4", "owner": "A", "settled": False, "gross": 300, "fee": 100, "doubled": False},
    }
    S["receipts"] = {}
    S["ledger"] = []


reset_store()


def rollover(acc: dict) -> None:
    today = day_of(now())
    if today > acc["date"]:
        acc["date"] = today
        acc["used"] = fresh_used()


def view(acc: dict) -> dict:
    return {
        "id": acc["id"], "gold": acc["gold"], "stamina": acc["stamina"],
        "pond_level": acc["pond_level"], "rod": acc["rod"], "date": acc["date"],
        "used": {p: dict(v) for p, v in acc["used"].items()},
        "last_ad_at": iso(acc["last_ad_at"]) if acc["last_ad_at"] is not None else None,
    }


def ok(**kw) -> dict:
    return {"ok": True, **kw}


def fail(code: str) -> dict:
    return {"ok": False, "error": code}


def ledger(player: str, field: str, delta: int, reason: str, op: str | None) -> None:
    S["ledger"].append({"seq": len(S["ledger"]) + 1, "time": iso(now()), "player": player,
                        "field": field, "delta": delta, "reason": reason, "operation_id": op})


async def body_of(request: Request) -> dict | None:
    try:
        data = await request.json()
    except Exception:
        return None
    return data if isinstance(data, dict) else None


async def maybe_drop(request: Request) -> None:
    # 故障注入：已经处理并提交，只是晚 3 秒才回，模拟回包丢失
    if request.headers.get("x-fault", "").lower() == "drop-response":
        await asyncio.sleep(3)


def claim_ad(data: dict) -> dict:
    player = data.get("player")
    placement = data.get("placement")
    op = data.get("operation_id")
    free = data.get("free", False)
    sid = data.get("session_id")
    if not isinstance(player, str) or player not in S["accounts"]:
        return fail("UNKNOWN_PLAYER")
    if not isinstance(op, str) or not (8 <= len(op) <= 64):
        return fail("BAD_OPERATION_ID")
    if not isinstance(free, bool) or (sid is not None and not isinstance(sid, str)):
        return fail("BAD_REQUEST")
    if placement not in PLACEMENTS:
        return fail("UNKNOWN_PLACEMENT")
    with LOCK:
        acc = S["accounts"][player]
        rollover(acc)
        key = (player, op)
        signature = (placement, free, sid if placement == "doubleSettle" else None)
        if key in S["receipts"]:
            rec = S["receipts"][key]
            return rec["resp"] if rec["sig"] == signature else fail("OPERATION_CONFLICT")
        t = now()
        if not free and acc["last_ad_at"] is not None and t - acc["last_ad_at"] < AD_MIN_INTERVAL_SEC:
            return fail("TOO_FREQUENT")
        quota = PLACEMENTS[placement]
        if free and acc["used"][placement]["free"] >= quota["free"]:
            return fail("NO_FREE_LEFT")
        if not free and acc["used"][placement]["ad"] >= quota["ad"]:
            return fail("CAP_REACHED")
        session = None
        if placement == "doubleSettle":
            if sid is None:
                return fail("NO_CONTEXT")
            session = S["sessions"].get(sid)
            if session is None or session["owner"] != player:
                return fail("NO_SESSION")
            if not session["settled"]:
                return fail("NOT_SETTLED")
            if session["doubled"]:
                return fail("ALREADY_DOUBLED")
            if session["gross"] <= 0:
                return fail("NOTHING_TO_DOUBLE")
            amount = min(session["gross"], math.floor(DOUBLE_CAP_FEE_MULT * session["fee"]))
            acc["gold"] += amount
            session["doubled"] = True
            ledger(player, "gold", amount, "doubleSettle", op)
            grant = {"kind": "gold", "amount": amount}
        else:
            acc["stamina"] += 1
            ledger(player, "stamina", 1, "stamina", op)
            grant = {"kind": "stamina", "amount": 1}
        acc["used"][placement]["free" if free else "ad"] += 1
        if not free:
            acc["last_ad_at"] = t
        resp = ok(grant=grant, account=view(acc))
        S["receipts"][key] = {"sig": signature, "resp": resp}
        return resp


def buy(data: dict) -> dict:
    player = data.get("player")
    if not isinstance(player, str) or player not in S["accounts"]:
        return fail("UNKNOWN_PLAYER")
    if data.get("kind") != "rod":
        return fail("BAD_REQUEST")
    tier = data.get("tier")
    with LOCK:
        acc = S["accounts"][player]
        if not isinstance(tier, int) or isinstance(tier, bool) or tier not in ROD_TIERS:
            return fail("BAD_TIER")
        if tier <= acc["rod"]:
            return fail("GEAR_OWNED")
        if ROD_TIERS[tier]["unlock"] > acc["pond_level"]:
            return fail("GEAR_LOCKED")
        price = ROD_TIERS[tier]["price"]
        if acc["gold"] < price:
            return fail("NO_GOLD")
        acc["gold"] -= price
        acc["rod"] = tier
        ledger(player, "gold", -price, f"buy_rod_{tier}", None)
        return ok(price=price, account=view(acc))


@app.get("/health")
def health():
    return ok(service="mini-shop")


@app.post("/reset")
def reset():
    with LOCK:
        reset_store()
    return ok()


@app.post("/ads/claim")
async def ads_claim(request: Request):
    data = await body_of(request)
    resp = fail("BAD_REQUEST") if data is None else await run_in_threadpool(claim_ad, data)
    await maybe_drop(request)
    return resp


@app.post("/shop/buy")
async def shop_buy(request: Request):
    data = await body_of(request)
    resp = fail("BAD_REQUEST") if data is None else await run_in_threadpool(buy, data)
    await maybe_drop(request)
    return resp


@app.get("/accounts/{pid}")
def account(pid: str):
    with LOCK:
        acc = S["accounts"].get(pid)
        if acc is None:
            return fail("UNKNOWN_PLAYER")
        rollover(acc)
        return ok(account=view(acc))


@app.get("/sessions/{sid}")
def session(sid: str):
    with LOCK:
        s = S["sessions"].get(sid)
        return ok(session=dict(s)) if s else fail("NO_SESSION")


@app.get("/ledger")
def get_ledger(player: str | None = None):
    with LOCK:
        rows = [dict(r) for r in S["ledger"] if player is None or r["player"] == player]
    return ok(entries=rows)


def clock_view() -> dict:
    t = now()
    return ok(now=iso(t), date=day_of(t), seconds_to_midnight=seconds_to_midnight(t))


@app.get("/debug/clock")
def get_clock():
    with LOCK:
        return clock_view()


@app.post("/debug/clock")
async def set_clock(request: Request):
    data = await body_of(request)
    sec = None if data is None else data.get("advance_sec")
    if not isinstance(sec, (int, float)) or isinstance(sec, bool) or sec < 0:
        return fail("BAD_REQUEST")
    with LOCK:
        S["offset"] += float(sec)
        return clock_view()


def main() -> None:
    import uvicorn
    ap = argparse.ArgumentParser(description="示例被测服务：迷你广告领奖 + 渔具店")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
