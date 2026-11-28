"""配置表之间的关联：框架按取值自动认（cfg links、cfg show --expand、整理知识库时给模型的关联）。

典型场景：商城表的物品ID → 物品表 → 品质字段 → 品质表。表里不写外键，靠“这一列的值全都能在另一张表的编号列里找到”
再加上字段名和表名对不对得上来认。运行：python -m pytest tests
"""
from pathlib import Path

import qa_index
from qakit import corpus

SHOP = (["商品ID", "物品ID", "价格", "限购"], [["1", "1001", "100", "1"], ["2", "1002", "250", "1"], ["3", "1003", "800", "5"]])
ITEM = (["ID", "名称", "品质"], [["1001", "小木竿", "1"], ["1002", "碳素竿", "2"], ["1003", "金竿", "3"], ["1004", "旧鱼护", "1"]])
QUALITY = (["ID", "名称", "颜色"], [["1", "普通", "白"], ["2", "稀有", "蓝"], ["3", "史诗", "紫"]])


def pairs(links, level=None):
    return {(l["from"], l["col"], l["to"], l["key"]) for l in links if level is None or l["level"] == level}


def test_shop_item_quality_chain():
    links = qa_index.find_links({"商城表": SHOP, "物品表": ITEM, "品质表": QUALITY})
    assert pairs(links, "高") == {("商城表", "物品ID", "物品表", "ID"), ("物品表", "品质", "品质表", "ID")}
    # 价格、限购、商品ID 这些数字碰巧落在别的表编号范围里，也不能算关联
    assert not any(l["col"] in ("价格", "限购", "商品ID") for l in links)


def test_missing_reference_is_reported_as_suspect():
    shop = (SHOP[0], SHOP[1] + [["4", "1009", "50", "1"]])  # 1009 在物品表里没有：配置填错了
    links = qa_index.find_links({"商城表": shop, "物品表": ITEM, "品质表": QUALITY})
    l = next(l for l in links if l["col"] == "物品ID")
    assert l["level"] == "中" and l["missing"] == ["1009"] and "找不到" in l["why"]


def test_json_shapes_kv_rows_list_cells_and_keys_in_field_names():
    tables = {
        "economy/legend": (["键", "值"], [["unlockMain", "m14a"], ["chance.base", "0.05"]]),
        "economy/quests.main.steps": (["id", "title"], [["m14", "给本子"], ["m14a", "领完"], ["m15", "下一步"]]),
        "economy/species": (["键", "name"], [["jiyu", "鲫鱼"], ["liyu", "鲤鱼"], ["caoyu", "草鱼"]]),
        "economy/ponds": (["id", "mix.jiyu", "mix.liyu"], [["p0", "0.75", "0.25"], ["p1", "0.5", "0.5"]]),
        "economy/legend.list": (["id", "pond", "species", "weight"], [["L01", "p0", "jiyu", "[2.6, 3.4]"], ["L02", "p1", "liyu", "[4, 5]"]]),
        "economy/novice": (["键", "值"], [["boost.ponds", '["p0", "p1"]']]),
    }
    got = {(l["from"], l["col"], l["to"], l["level"], l["kind"]) for l in qa_index.find_links(tables)}
    assert ("economy/legend", "unlockMain", "economy/quests.main.steps", "高", "键值行") in got
    assert ("economy/legend.list", "pond", "economy/ponds", "高", "列") in got
    assert ("economy/legend.list", "species", "economy/species", "高", "列") in got
    assert ("economy/novice", "boost.ponds", "economy/ponds", "高", "键值行") in got  # 一格里是 JSON 列表
    assert ("economy/ponds", "mix.*", "economy/species", "中", "字段名") in got  # 编号写在字段名里，名字对不上 → 要确认
    assert not any(l["col"] in ("weight", "chance.base") for l in qa_index.find_links(tables))


def test_same_ids_in_several_tables_picks_the_main_table():
    tables = {"cfg/ponds": (["id", "name", "price", "fee"], [["p0", "小河", "5", "0"], ["p1", "水库", "9", "1"]]),
              "ui/ponds": (["键", "icon"], [["p0", "a.png"], ["p1", "b.png"]]),
              "cfg/legend": (["id", "pond"], [["L1", "p0"], ["L2", "p1"]])}
    ls = [l for l in qa_index.find_links(tables) if l["from"] == "cfg/legend"]
    top = [l for l in ls if l["level"] == "高"]
    assert [(l["to"], l["key"]) for l in top] == [("cfg/ponds", "id")]  # 字段多的当主表，另一张降成“要确认”
    assert any(l["to"] == "ui/ponds" and l["level"] == "中" for l in ls)


def test_ref_values_and_name_parts():
    assert qa_index.ref_values('["p0", "p1"]') == ["p0", "p1"]
    assert qa_index.ref_values('{"jiyu": 0.7}') == ["jiyu"]
    assert qa_index.ref_values("1001;1002|1003") == ["1001", "1002", "1003"]
    assert qa_index.ref_values("[2.6, 3.4]") == [] and qa_index.ref_values("True") == []
    assert qa_index.link_hint("物品ID", "物品表", "ID") == "表名"
    assert qa_index.link_hint("pond", "economy/ponds", "id") == "表名"
    assert qa_index.link_hint("unlockMain", "economy/quests.main.steps", "id") == "表名"
    assert qa_index.link_hint("价格", "物品表", "ID") == ""
    assert qa_index.link_hint("ItemID", "shop/items", "id") == "表名" and qa_index.link_hint("quality_id", "Quality", "ID") == "表名"


def test_english_excel_chain():
    tables = {"Shop": (["ID", "ItemID", "Price"], [["1", "1001", "100"], ["2", "1002", "250"]]),
              "Items": (["ID", "Name", "Quality"], [["1001", "Rod", "1"], ["1002", "Net", "2"]]),
              "Quality": (["ID", "Name"], [["1", "White"], ["2", "Blue"], ["3", "Purple"]])}
    assert pairs(qa_index.find_links(tables), "高") == {("Shop", "ItemID", "Items", "ID"), ("Items", "Quality", "Quality", "ID")}


def test_expand_follows_the_chain_through_index(tmp_path, capsys):
    cfg = tmp_path / "配置"
    cfg.mkdir()
    for name, (cols, rows) in (("商城表", SHOP), ("物品表", ITEM), ("品质表", QUALITY)):
        (cfg / f"{name}.csv").write_text("\n".join(",".join(r) for r in [cols] + rows) + "\n", encoding="utf-8")
    idx = qa_index.Index(tmp_path / "index.sqlite")
    try:
        qa_index.build(idx, [{"name": "配置", "kind": "config", "path": str(cfg)}], None)
        hits, _ = idx.table_rows("商城表", [("商品ID", "2")], 10)
        corpus.expand_links(idx, idx.links(), "商城表", SHOP[0], hits, 2, {"商城表"})
        text = capsys.readouterr().out
        assert "物品ID → 物品表.ID：表 物品表 里对上 1 行" in text and "碳素竿" in text
        assert "品质 → 品质表.ID：表 品质表 里对上 1 行" in text and "稀有" in text and "史诗" not in text
        assert idx.links() == idx.links()  # 第二次直接用缓存
    finally:
        idx.close()


# ── 完整代码权限时的读代码工具 ──

def test_source_tools_stay_inside_source_paths(tmp_path):
    from qakit import steps
    src = tmp_path / "game"
    (src / "cloud").mkdir(parents=True)
    (src / "node_modules" / "x").mkdir(parents=True)
    (src / "cloud" / "legend.ts").write_text("const cfg = economy.legend;\nif (step >= cfg.unlockMain) open();\n", encoding="utf-8")
    (src / "node_modules" / "x" / "a.ts").write_text("unlockMain", encoding="utf-8")
    (tmp_path / "secret.ts").write_text("unlockMain", encoding="utf-8")
    hits = steps.source_grep([src], "UNLOCKMAIN")
    assert "game/cloud/legend.ts:2:" in hits and "node_modules" not in hits
    assert "economy.legend" in steps.source_read([src], "game/cloud/legend.ts", 1, 1)
    assert "cfg.unlockMain" in steps.source_read([src], "cloud/legend.ts", 2, 5)  # 不带代码文件夹名也行
    assert "没有这个文件" in steps.source_read([src], "../secret.ts")
    assert "没有这个文件" in steps.source_read([src], "game/../secret.ts")


def test_source_tools_skip_secret_files(tmp_path):
    from qakit import steps
    src = tmp_path / "game"
    src.mkdir()
    (src / ".env").write_text("DB_PASSWORD=abc unlockMain", encoding="utf-8")
    (src / "secrets.json").write_text('{"unlockMain": "x"}', encoding="utf-8")
    (src / "tokenizer.ts").write_text("// unlockMain", encoding="utf-8")  # 名字里带 token 但不是密钥文件，照常搜
    hits = steps.source_grep([src], "unlockMain")
    assert "tokenizer.ts" in hits and "secrets.json" not in hits and ".env" not in hits
    assert "不给读" in steps.source_read([src], "game/.env") and "不给读" in steps.source_read([src], "secrets.json")


# ── 出测试点时不给看这个需求自己的旧答案 ──

def test_in_req_dir_survives_moved_workspace():
    from qakit.corpus import in_req_dir
    old = r"D:\老位置\工作区\reqs\R001-营业款\最终测试点.md"  # 索引里记的还是搬家前的路径
    assert in_req_dir(old, Path(r"C:\新位置\reqs\R001-营业款")) and in_req_dir(old, "R001-营业款")
    assert not in_req_dir(old, "R002-别的需求") and not in_req_dir(old, None)


def test_tool_runner_hides_own_requirement_answers(tmp_path):
    from qakit import steps
    d = tmp_path / "reqs" / "R001-营业款"
    d.mkdir(parents=True)
    (d / "需求.md").write_text("需求原文", encoding="utf-8")
    (d / "最终测试点.md").write_text("旧答案", encoding="utf-8")
    ws = type("W", (), {"root": tmp_path, "access": "blackbox", "cfg": {}})()
    run = steps.tool_runner(ws, 5000, hide="R001-营业款")
    assert "不给看" in run("read_file", {"path": "reqs/R001-营业款/最终测试点.md"})
    assert run("read_file", {"path": "reqs/R001-营业款/需求.md"}) == "需求原文"
    assert steps.tool_runner(ws, 5000)("read_file", {"path": "reqs/R001-营业款/最终测试点.md"}) == "旧答案"  # 别的步骤照常能看


# ── 用户提的情况和复杂 Excel 实测里暴露的问题 ──

ITEM_Q = (["ID", "名称", "品质"], [["1001", "木竿", "A"], ["1002", "碳竿", "B"], ["1003", "金竿", "C"], ["1004", "龙竿", "D"], ["1005", "旧竿", "A"]])


def test_value_points_to_non_key_field():
    # 物品表的品质填 A/B/C/D，品质表的唯一 ID 是 1、2、3…，另有“类型”字段填 A/B/C/D
    unique = (["ID", "类型", "颜色"], [["1", "A", "绿色"], ["2", "B", "蓝色"], ["3", "C", "紫色"], ["4", "D", "橙色"]])
    many = (["ID", "类型", "等级", "颜色"], [["1", "A", "1", "绿色"], ["2", "A", "2", "绿色"], ["3", "B", "1", "蓝色"],
                                          ["4", "B", "2", "蓝色"], ["5", "C", "1", "紫色"], ["6", "D", "1", "橙色"]])
    l1 = qa_index.find_links({"物品表": ITEM_Q, "品质表": unique})
    l2 = qa_index.find_links({"物品表": ITEM_Q, "品质表": many})
    assert [(l["col"], l["to"], l["key"], l["level"]) for l in l1] == [("品质", "品质表", "类型", "高")]
    assert [(l["col"], l["to"], l["key"], l["level"], l["target"]) for l in l2] == [("品质", "品质表", "类型", "高", "分类")]
    assert "一对多" in l2[0]["why"]


def test_english_field_to_chinese_table_and_generic_type_column():
    tables = {"品质表": (["ID", "类型"], [["1", "A"], ["2", "B"], ["3", "C"]]),
              "装备部位": (["ID", "名称"], [[str(i), f"部位{i}"] for i in range(1, 7)]),
              "装备": (["EquipId", "Quality", "Slot"], [["110001", "A", "1"], ["110002", "B", "3"], ["110003", "C", "6"], ["110004", "A", "2"]]),
              "道具/物品类型": (["ID", "名称"], [[str(i), f"类型{i}"] for i in range(1, 7)]),
              "道具/物品": (["ID", "类型", "售价"], [["100001", "1", "10"], ["100002", "4", "20"], ["100003", "6", "30"]])}
    got = pairs(qa_index.find_links(tables), "高")
    assert ("装备", "Quality", "品质表", "类型") in got      # quality ↔ 品质
    assert ("装备", "Slot", "装备部位", "ID") in got         # slot ↔ 部位
    assert ("道具/物品", "类型", "道具/物品类型", "ID") in got  # 通用字段名“类型”：表名正好是“物品 + 类型”
    assert not any(c == "售价" for _, c, _, _ in got)


def test_comment_row_under_header_is_not_data():
    # 英文字段名里夹了中文，下面一行是中文说明：以前只在字段名全是英文时才认说明行
    rows = [["ID", "f_物品列表", "Price"], ["int", "string", "int"], ["编号", "物品列表的说明", "价格"],
            ["1", "1001;1002", "10"], ["2", "1003", "20"], ["3", "1001", "30"], ["4", "1002", "40"]]
    cols, notes, data = qa_index.parse_table(rows)
    assert cols == ["ID", "f_物品列表", "Price"] and len(notes) == 2 and data[0] == ["1", "1001;1002", "10"]


def test_id_count_cells_and_own_key_coincidence():
    assert qa_index.ref_values("100001:2;100005:1") == ["100001", "100005"] and qa_index.ref_values("1001*5") == ["1001"]
    item = (["ID", "名称"], [[str(100000 + i), f"物品{i}"] for i in range(1, 30)])
    pack = (["礼包ID", "内容"], [["5001", "100001:5;100002:3"], ["5002", "100003:1"], ["5003", "100004:2|100005:9"]])
    other = (["ID", "名称"], [[str(100000 + i), f"活动{i}"] for i in range(1, 10)])  # 编号范围碰巧和物品表重叠
    links = qa_index.find_links({"道具/物品": item, "商城/礼包": pack, "活动": other})
    assert ("商城/礼包", "内容", "道具/物品", "ID") in pairs(links)
    assert not any(l["from"] == "活动" and l["col"] == "ID" for l in links)  # 本表自己的编号、名字对不上：不算引用


# ── 认不出的由人补、认错的由人否决：wiki/配置关联.md ──

def test_link_page_confirm_add_and_reject(tmp_path, capsys):
    from qakit.corpus import all_links, expand_links, link_page_problems
    cfg = tmp_path / "配置"
    cfg.mkdir()
    job = (["ID", "名称"], [["1", "战士"], ["2", "法师"], ["3", "弓手"]])
    hero = (["英雄ID", "名称", "参数1"], [["1", "甲", "2"], ["2", "乙", "3"], ["3", "丙", "1"]])  # 参数1 其实是职业：名字对不上、小数字，框架认不出
    for name, (cols, rows) in (("商城表", SHOP), ("物品表", ITEM), ("品质表", QUALITY), ("职业", job), ("英雄", hero)):
        (cfg / f"{name}.csv").write_text("\n".join(",".join(r) for r in [cols] + rows) + "\n", encoding="utf-8")
    (tmp_path / ".qa").mkdir()
    idx = qa_index.Index(tmp_path / ".qa" / "index.sqlite")
    qa_index.build(idx, [{"name": "配置", "kind": "config", "path": str(cfg)}], None)
    ws = type("W", (), {"root": tmp_path, "wiki": tmp_path / "wiki"})()
    ws.wiki.mkdir()
    (ws.wiki / "配置关联.md").write_text(
        "来源：人审\n\n| 表 | 字段 | 关联表 | 关联字段 | 结论 | 依据 |\n|---|---|---|---|---|---|\n"
        "| 英雄 | 参数1 | 职业 | ID | 确认 | 策划案 3.2 |\n| 物品表 | 品质 | 品质表 | ID | 否决 | 测试用 |\n"
        "| 英雄 | 名称 | 物品表 | 名称 | 待确认 | 猜的 |\n| 英雄 | 不存在的字段 | 职业 | ID | 待确认 |  |\n", encoding="utf-8")
    try:
        links = all_links(ws, idx)
        got = {(l["from"], l["col"], l["to"]): l["level"] for l in links}
        assert got[("英雄", "参数1", "职业")] == "确认"          # 人补的：和“高”一样用
        assert ("物品表", "品质", "品质表") not in got           # 人否决的：不再出现
        assert got[("英雄", "名称", "物品表")] == "中"            # AI 写的待确认：当“中”
        hits, _ = idx.table_rows("英雄", [("英雄ID", "1")], 5)
        expand_links(idx, links, "英雄", hero[0], hits, 1, {"英雄"})
        assert "法师" in capsys.readouterr().out                  # 连带查走人确认的关联
    finally:
        idx.close()
    probs = link_page_problems(ws)
    assert any("不存在的字段" in p for p in probs) and len(probs) == 1


# ── 本地小模型装不下时压短提示词 ──

def test_shrink_prompt_keeps_task_and_tail_lines():
    from qakit import steps
    text = ("为下面的需求生成测试点。\n\n## 需求（R001）\n\n" + "需求原文。" * 200 + "\n\n## 知识库\n\n" + "知识库内容。" * 3000 +
            "\n\n## 检索到的\n\n" + "检索段落。" * 3000 + "\n历史缺陷里出过的问题要写进测试点。\n\n## 输出要求\n\n只输出表格。\n")
    short = steps.shrink_prompt(text, 12000)
    assert len(short) <= 13000 and "需求原文。" * 200 in short and "只输出表格。" in short
    assert "历史缺陷里出过的问题要写进测试点。" in short and "中间" in short and "没放进来" in short
    assert steps.shrink_prompt("短", 100) == "短"
