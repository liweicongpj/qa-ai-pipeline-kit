"""造一套像真实游戏配置的 Excel（多工作簿、多工作表、三行表头或中文表头），写出标准答案 truth.json。
用法：python tools/links_bench/gen_excel.py 输出目录 [系统个数，默认 15]
每条标准答案：[来源表, 字段, 被引用表, 被引用字段, 写法]。写法里 hard_ 开头的是事先就知道框架靠取值认不出的。"""
import json
import random
import sys
from pathlib import Path

import openpyxl

random.seed(20261128)
OUT = Path(sys.argv[1])
OUT.mkdir(parents=True, exist_ok=True)
books: dict[str, list] = {}   # 工作簿 → [(工作表, 字段, 数据行, 表头风格)]
truth: list[list] = []         # (工作簿, 工作表, 字段, 工作簿, 工作表, 字段, 写法)


def add(book, sheet, cols, rows, style="cn"):
    books.setdefault(book, []).append((sheet, cols, rows, style))


def rel(src, col, dst, dcol, how):
    truth.append([*src, col, *dst, dcol, how])


def pick(xs, k=1):
    return random.sample(xs, k) if k > 1 else random.choice(xs)


# ── 基础表 ──
ITEM = list(range(100001, 102001))
ITEM_NAME = [f"物品{n}" for n in range(len(ITEM))]
QUAL_CODE = ["A", "B", "C", "D"]
add("品质表", "品质", ["ID", "类型", "等级段", "颜色"],
    [[i + 1, QUAL_CODE[i // 2], i % 2 + 1, ["绿", "蓝", "紫", "橙"][i // 2]] for i in range(8)])            # 类型有重复：一对多
add("道具", "物品类型", ["ID", "名称"], [[i, f"类型{i}"] for i in range(1, 7)])
add("道具", "物品", ["ID", "名称", "品质", "类型", "售价", "堆叠上限", "图标"],
    [[ITEM[i], ITEM_NAME[i], pick(QUAL_CODE), random.randint(1, 6), random.randint(1, 50) * 10, pick([1, 99, 999]), f"icon/i{i}.png"]
     for i in range(len(ITEM))])
rel(("道具", "物品"), "品质", ("品质表", "品质"), "类型", "文字代码对非唯一字段（用户的例子）")
rel(("道具", "物品"), "类型", ("道具", "物品类型"), "ID", "hard_通用字段名+小数字")
add("货币", "货币", ["ID", "名称"], [[1, "金币"], [2, "钻石"], [3, "体力"]])

SKILL = list(range(3001, 3301))
BUFF = list(range(4001, 4101))
add("技能", "Buff", ["ID", "名称", "持续秒数"], [[b, f"buff{b}", random.randint(1, 30)] for b in BUFF])
add("技能", "技能", ["ID", "名称", "前置技能", "BuffID", "冷却"],
    [[s, f"技能{s}", (SKILL[i - 1] if i and random.random() < 0.6 else ""), pick(BUFF), random.randint(1, 20)] for i, s in enumerate(SKILL)])
rel(("技能", "技能"), "前置技能", ("技能", "技能"), "ID", "自己引用自己")
rel(("技能", "技能"), "BuffID", ("技能", "Buff"), "ID", "英文字段对英文表名")

DROP = list(range(9001, 9401))
add("掉落", "掉落", ["DropId", "物品列表", "额外掉落", "权重"],
    [[d, json.dumps(pick(ITEM, 3)), ";".join(f"{x}:{random.randint(1, 5)}" for x in pick(ITEM, 2)), json.dumps([random.random() for _ in range(3)])]
     for d in DROP], style="en")
rel(("掉落", "掉落"), "物品列表", ("道具", "物品"), "ID", "JSON 列表")
rel(("掉落", "掉落"), "额外掉落", ("道具", "物品"), "ID", "物品ID:数量，名字对不上")

MON = list(range(20001, 20501))
add("怪物", "怪物", ["MonsterID", "Name", "Level", "DropID", "SkillIDs", "掉落物品名", "HP", "Attack"],
    [[m, f"monster{m}", random.randint(1, 60), pick(DROP), "|".join(map(str, pick(SKILL, 2))), pick(ITEM_NAME[:300]), random.randint(100, 9999), random.randint(10, 999)]
     for m in MON], style="en")
rel(("怪物", "怪物"), "DropID", ("掉落", "掉落"), "DropId", "英文字段对中文表名，大编号")
rel(("怪物", "怪物"), "SkillIDs", ("技能", "技能"), "ID", "一格多个，英文对中文")
rel(("怪物", "怪物"), "掉落物品名", ("道具", "物品"), "名称", "引用名称")
add("角色", "升级", ["等级", "所需经验"], [[lv, lv * lv * 100] for lv in range(1, 61)])
rel(("怪物", "怪物"), "Level", ("角色", "升级"), "等级", "hard_英文对中文+小数字")

QUEST = list(range(7001, 7801))
add("任务", "任务", ["任务ID", "名称", "前置任务", "奖励", "目标怪物", "类型", "经验"],
    [[q, f"任务{q}", (QUEST[i - 1] if i else ""), "|".join(f"{x}:{random.randint(1, 3)}" for x in pick(ITEM, 2)), pick(MON), random.randint(1, 3), random.randint(10, 500)]
     for i, q in enumerate(QUEST)])
rel(("任务", "任务"), "前置任务", ("任务", "任务"), "任务ID", "自己引用自己")
rel(("任务", "任务"), "奖励", ("道具", "物品"), "ID", "物品ID:数量，名字对不上")
rel(("任务", "任务"), "目标怪物", ("怪物", "怪物"), "MonsterID", "中文字段对中文表名")

add("关卡", "关卡", ["ID", "名称", "怪物组", "解锁关卡", "首通奖励", "推荐等级", "体力消耗"],
    [[i, f"关卡{i}", ";".join(map(str, pick(MON, 3))), (i - 1 if i > 1 else ""), pick(DROP), random.randint(1, 60), random.randint(5, 10)]
     for i in range(1, 51)])
rel(("关卡", "关卡"), "怪物组", ("怪物", "怪物"), "MonsterID", "一格多个")
rel(("关卡", "关卡"), "解锁关卡", ("关卡", "关卡"), "ID", "自己引用自己，小数字")
rel(("关卡", "关卡"), "首通奖励", ("掉落", "掉落"), "DropId", "名字对不上，大编号")

add("角色", "职业", ["ID", "名称"], [[i, f"职业{i}"] for i in range(1, 6)])
add("角色", "角色", ["ID", "名称", "职业", "初始武器", "初始等级"],
    [[i, f"角色{i}", random.randint(1, 5), pick(ITEM), 1] for i in range(1, 21)])
rel(("角色", "角色"), "职业", ("角色", "职业"), "ID", "小数字，名字对得上")
rel(("角色", "角色"), "初始武器", ("道具", "物品"), "ID", "名字对不上，大编号")

GOODS = list(range(1, 301))
shop_rows = [[g, pick(ITEM), random.randint(1, 3), random.randint(1, 100) * 10, pick([0, 1, 5, 10])] for g in GOODS]
shop_rows[17][1] = 109999  # 配置填错：物品表里没有这个编号
add("商城", "商城", ["商品ID", "物品ID", "货币类型", "价格", "限购"], shop_rows)
rel(("商城", "商城"), "物品ID", ("道具", "物品"), "ID", "名字对得上，有一个填错")
rel(("商城", "商城"), "货币类型", ("货币", "货币"), "ID", "小数字，名字对得上")
PACK = list(range(5001, 5051))
add("商城", "礼包", ["礼包ID", "内容", "价格"],
    [[p, ";".join(f"{x}:{random.randint(1, 9)}" for x in pick(ITEM, 3)), random.randint(1, 50) * 6] for p in PACK])
rel(("商城", "礼包"), "内容", ("道具", "物品"), "ID", "物品ID:数量，名字对不上")

add("活动", "活动", ["ID", "名称", "商城商品"], [[8000 + i, f"活动{i}", ";".join(map(str, pick(GOODS, 3)))] for i in range(1, 21)])
rel(("活动", "活动"), "商城商品", ("商城", "商城"), "商品ID", "一格多个小数字，名字对得上")
add("VIP特权", "VIP特权", ["ID", "描述"], [[i, f"特权{i}"] for i in range(1, 11)])
add("VIP", "VIP", ["等级", "每日礼包", "特权列表"], [[lv, pick(PACK), ",".join(map(str, sorted(pick(list(range(1, 11)), 3))))] for lv in range(0, 16)])
rel(("VIP", "VIP"), "每日礼包", ("商城", "礼包"), "礼包ID", "名字对得上")
rel(("VIP", "VIP"), "特权列表", ("VIP特权", "VIP特权"), "ID", "一格多个小数字，名字对得上")

add("坐骑", "坐骑", ["ID", "名称", "品质", "解锁道具"], [[6000 + i, f"坐骑{i}", pick(QUAL_CODE), pick(ITEM)] for i in range(1, 61)])
rel(("坐骑", "坐骑"), "品质", ("品质表", "品质"), "类型", "文字代码对非唯一字段")
rel(("坐骑", "坐骑"), "解锁道具", ("道具", "物品"), "ID", "名字对不上，大编号")

add("Achievements", "Achievements", ["Id", "Name", "RewardItemId", "QuestId", "Points"],
    [[i, f"ach{i}", pick(ITEM), pick(QUEST), random.randint(5, 50)] for i in range(1, 101)], style="en")
rel(("Achievements", "Achievements"), "RewardItemId", ("道具", "物品"), "ID", "英文字段对中文表名，大编号")
rel(("Achievements", "Achievements"), "QuestId", ("任务", "任务"), "任务ID", "英文字段对中文表名，大编号")

SET = list(range(120001, 120021))
add("套装", "套装", ["ID", "名称"], [[s, f"套装{s}"] for s in SET])
add("装备部位", "装备部位", ["ID", "名称"], [[i, f"部位{i}"] for i in range(1, 7)])
add("装备", "装备", ["EquipId", "Quality", "Slot", "SetId", "Attack"],
    [[110000 + i, pick(QUAL_CODE), random.randint(1, 6), pick(SET), random.randint(1, 500)] for i in range(1, 301)], style="en")
rel(("装备", "装备"), "Quality", ("品质表", "品质"), "类型", "hard_英文对中文+文字代码对非唯一字段")
rel(("装备", "装备"), "Slot", ("装备部位", "装备部位"), "ID", "hard_英文对中文+小数字")
rel(("装备", "装备"), "SetId", ("套装", "套装"), "ID", "英文字段对中文表名，大编号")

# ── 再加 15 个“系统”，每个引用几张基础表，写法随机，模拟真实项目里表多、关系杂 ──
TARGETS = [(("道具", "物品"), "ID", ITEM, "物品"), (("怪物", "怪物"), "MonsterID", MON, "怪物"), (("技能", "技能"), "ID", SKILL, "技能"),
           (("任务", "任务"), "任务ID", QUEST, "任务"), (("货币", "货币"), "ID", [1, 2, 3], "货币"), (("角色", "职业"), "ID", [1, 2, 3, 4, 5], "职业"),
           (("商城", "礼包"), "礼包ID", PACK, "礼包"), (("关卡", "关卡"), "ID", list(range(1, 51)), "关卡")]
NSYS = int(sys.argv[2]) if len(sys.argv) > 2 else 15
for k in range(1, NSYS + 1):
    book = f"系统{k:03d}"
    n = random.randint(20, 120)
    cols, data = ["ID", "名称"], [[k * 1000 + i, f"{book}_{i}"] for i in range(1, n + 1)]
    for j in range(random.randint(2, 4)):
        (dst, dcol, ids, short) = pick(TARGETS)
        style = pick(["名字对得上", "名字对得上", "一格多个，名字对得上", "物品ID:数量，名字对得上", "名字对不上"])
        if style == "名字对不上":
            col = f"参数{j + 1}"
        elif style.startswith("物品ID"):
            col = f"{short}奖励"
        elif style.startswith("一格"):
            col = f"{short}列表"
        else:
            col = f"{short}ID"
        if col in cols:
            continue
        for r in data:
            if style.startswith("一格"):
                r.append(";".join(map(str, pick(ids, min(2, len(ids))))))
            elif style.startswith("物品ID"):
                r.append("|".join(f"{x}:{random.randint(1, 9)}" for x in pick(ids, min(2, len(ids)))))
            else:
                r.append(pick(ids))
        cols.append(col)
        small = max(ids) < 100
        how = style if not (style == "名字对不上" and small) else "hard_名字对不上+小数字"
        rel((book, "配置"), col, dst, dcol, how)
    for d in ("数量", "权重", "排序", "开关", "等级"):  # 干扰列：小数字，容易和小编号碰巧对上
        cols.append(d)
        for r in data:
            r.append({"数量": random.randint(1, 10), "权重": random.randint(1, 100), "排序": random.randint(1, 50),
                      "开关": random.randint(0, 1), "等级": random.randint(1, 60)}[d])
    add(book, "配置", cols, data, style=pick(["cn", "en"]))

# ── 写文件；表名规则和框架一致：一个工作簿多张表时是“工作簿/工作表”，只有一张时就是工作簿名 ──
EN_TYPE = {int: "int", float: "float", str: "string"}
for book, sheets in books.items():
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for sheet, cols, rows, style in sheets:
        ws = wb.create_sheet(sheet)
        if style == "en":  # 英文字段名 + 类型行 + 中文注释行（很多项目的格式）
            ws.append([f"f_{c}" if not c.isascii() else c for c in cols])
            ws.append([EN_TYPE.get(type(next((r[i] for r in rows if r[i] != ""), "")), "string") for i in range(len(cols))])
            ws.append([f"{c}的说明" for c in cols])
        else:
            ws.append(cols)
        for r in rows:
            ws.append(r)
    wb.save(OUT / f"{book}.xlsx")


def tname(book, sheet):
    return f"{book}/{sheet}" if len(books[book]) > 1 else book


def colname(book, sheet, c):
    style = next(s for sh, _, _, s in books[book] if sh == sheet)
    return f"f_{c}" if style == "en" and not c.isascii() else c


final = [[tname(a, b), colname(a, b, c), tname(d, e), colname(d, e, f), how] for a, b, c, d, e, f, how in truth]
(OUT / "truth.json").write_text(json.dumps(final, ensure_ascii=False, indent=1), encoding="utf-8")
print(f"{len(books)} 个工作簿、{sum(len(s) for s in books.values())} 张表、{sum(len(r) for s in books.values() for _, _, r, _ in s)} 行；标准答案 {len(final)} 条")
