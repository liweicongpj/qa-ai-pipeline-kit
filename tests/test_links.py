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
