"""示例项目的不变量：每个接口用例跑完后，框架生成的 conftest.py 会自动调用 check(base_url)。

不变量是“不管做了什么操作都必须成立”的规则，来自知识库的耦合关系表（约束那一列）。
它是兜底：哪怕没人想到某个组合（比如买东西 + 打折券），只要某个用例让账变得不对，就会在用例结束时被抓出来。
返回空列表表示没问题；返回的每一条都会让这个用例判失败。
"""
import httpx

START_GOLD = 1000  # 测试数据：/reset 后每个账号的金币
PLAYERS = ("A", "B", "C")


def check(base_url: str) -> list[str]:
    bad = []
    with httpx.Client(base_url=base_url, timeout=5) as c:
        ledger = c.get("/ledger").json().get("entries", [])
        for pid in PLAYERS:
            acc = c.get(f"/accounts/{pid}").json().get("account")
            if not acc:
                continue
            if acc["gold"] < 0:
                bad.append(f"{pid} 的金币变成负数：{acc['gold']}")
            if acc["stamina"] < 0:
                bad.append(f"{pid} 的体力变成负数：{acc['stamina']}")
            # 资产守恒：当前金币 = 初始金币 + 流水里所有金币变化之和
            delta = sum(e["delta"] for e in ledger if e["player"] == pid and e["field"] == "gold")
            if acc["gold"] != START_GOLD + delta:
                bad.append(f"{pid} 的金币和流水对不上：现在 {acc['gold']}，初始 {START_GOLD} + 流水 {delta}")
    return bad
