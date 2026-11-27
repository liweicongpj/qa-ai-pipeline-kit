"""解析函数的细粒度测试：Markdown 表格、字段、测试点、审核表、模块清单、过时资料、JSON 配置、旧用例、缺陷列识别。

自检（python qa.py selftest）把整条流程从头走一遍；这里专门测“靠表头名字和格式来解析”的地方，
改了格式或代码，这里会先报出来。运行：python -m pytest tests
"""
import json
from pathlib import Path

import qa_index
from qakit import common, regress, reqs, wiki


class FakeWs:
    """测模块清单、过时资料用：只要有 wiki 目录。"""

    def __init__(self, root: Path):
        self.root = root
        self.wiki = root / "wiki"
        self.raw = root / "raw"

    def modules(self):
        d = self.wiki / "modules"
        return sorted(p.stem for p in d.glob("*.md")) if d.exists() else []


# ── 通用 ──

def test_md_tables_skips_separator_and_keeps_empty_cells():
    t = common.md_tables("前言\n\n| 甲 | 乙 |\n|---|:---:|\n| 1 |  |\n\n正文\n| x |\n")
    assert t == [[["甲", "乙"], ["1", ""]], [["x"]]]


def test_fields_and_set_field():
    text = "- 严重程度：\n- 归类: 幂等\n"
    assert common.fields(text) == {"严重程度": "", "归类": "幂等"}
    assert common.fields(common.set_field(text, "严重程度", "P1"))["严重程度"] == "P1"


def test_section_stops_at_next_heading():
    text = "## 现象\n到账两次\n\n## 原因\n没做幂等\n"
    assert common.section(text, "现象") == "到账两次"
    assert common.section(text, "不存在") == ""


def test_split_names_drops_placeholders():
    assert common.split_names("广告、渔具店/ 会员，—；无") == ["广告", "渔具店", "会员"]


# ── 测试点和审核表 ──

def points(tmp_path, md):
    f = tmp_path / "测试点.md"
    f.write_text(md, encoding="utf-8")
    return reqs.parse_points(f)


def test_parse_points_reads_columns_by_header_name(tmp_path):
    md = ("| 编号 | 测试点 | 风险等级（高/中/低） | 类型（正常/异常） | 依据（wiki 页） | 怎么验证 |\n|---|---|---|---|---|---|\n"
          "| 1 | 重复领取只到账一次 | 高 | 异常 | wiki/a.md | 对账 |\n")
    p = points(tmp_path, md)[0]
    assert (p["no"], p["point"], p["risk"], p["type"], p["basis"], p["verify"]) == ("1", "重复领取只到账一次", "高", "异常", "wiki/a.md", "对账")


def test_parse_points_change_table_number_not_confused_with_original_number(tmp_path):
    md = "| 原编号 | 编号 | 变更（新增/修改/作废/回归） | 测试点 |\n|---|---|---|---|\n| T03 | 2 | 修改 | 改后的内容 |\n"
    p = points(tmp_path, md)[0]
    assert (p["no"], p["orig"], p["change"]) == ("2", "T03", "修改")


def test_parse_review_and_problems(tmp_path):
    f = tmp_path / "审核.md"
    f.write_text("审核耗时（分钟）：\n模型名称：甲\n\n| 编号 | 测试点 | 结论 | 否决原因 | 修改后的内容 |\n|---|---|---|---|---|\n"
                 "| 1 | a | 采纳 |  |  |\n| 2 | b | 否决 | 太长 |  |\n| 3 | c | 改后采纳 |  |  |\n\n## 审核时补充的测试点\n- 新补的\n",
                 encoding="utf-8")
    rows, meta = reqs.parse_review(f)
    assert [r["verdict"] for r in rows] == ["采纳", "否决", "改后采纳"] and meta["added"] == ["新补的"] and meta["模型名称"] == "甲"
    probs = " ".join(reqs.review_problems(rows, meta))
    assert "否决要填原因" in probs and "改后采纳" in probs and "审核耗时" in probs


# ── 模块清单 ──

def write_catalog(ws: FakeWs):
    (ws.wiki / "modules").mkdir(parents=True)
    (ws.wiki / "modules" / "渔具店.md").write_text("# 渔具店\n", encoding="utf-8")
    (ws.wiki / "模块清单.md").write_text(
        "| 模块 | 上级模块 | 别名 | 说明 |\n|---|---|---|---|\n| 渔具店 | 商城 | 商店、Shop | 买竿 |\n| 会员 | 商城 | VIP |  |\n",
        encoding="utf-8")


def test_normalize_modules_alias_parent_unknown(tmp_path):
    ws = FakeWs(tmp_path)
    write_catalog(ws)
    mods, hints, unknown = wiki.normalize_modules(ws, ["shop", "商城", "抽奖", "渔具店"])
    assert mods == ["渔具店"] and hints == [("商城", ["渔具店", "会员"])] and unknown == ["抽奖"]


def test_module_catalog_includes_pages_not_listed(tmp_path):
    ws = FakeWs(tmp_path)
    write_catalog(ws)
    (ws.wiki / "modules" / "签到.md").write_text("# 签到\n", encoding="utf-8")
    cat = {m["name"]: m for m in wiki.module_catalog(ws)}
    assert cat["渔具店"]["aliases"] == ["商店", "Shop"] and cat["签到"]["listed"] is False


# ── 过时资料 ──

def test_obsolete_marks_only_the_snippet_and_whole_section_goes_last(tmp_path):
    ws = FakeWs(tmp_path)
    ws.wiki.mkdir()
    (ws.wiki / "过时资料.md").write_text(
        "| 资料 | 章节 | 过时的原文 | 被什么替代 | 现行规则在哪 | 日期 |\n|---|---|---|---|---|---|\n"
        "| 设计.docx | 领取 | 零头 留到下次 | R002 | wiki/x.md |  |\n| 设计.docx | 翻倍 |  | R003 |  |  |\n", encoding="utf-8")
    hits = [{"path": "D:/资料/设计.docx", "title": "设计 > 翻倍", "text": "封顶两倍"},
            {"path": "D:/资料/设计.docx", "title": "设计 > 领取", "text": "向下取整，零头留到下次。冷却 10 秒。"}]
    out = wiki.annotate_obsolete(ws, hits)
    assert out[0]["text"] == "向下取整，零头留到下次【⚠️已过时：被 R002 替代，现行规则见 wiki/x.md】。冷却 10 秒。"
    assert out[1]["text"].startswith("【⚠️这一节已整体过时")


# ── 检索层 ──

def test_json_tables_three_shapes_and_inherited_comment(tmp_path):
    f = tmp_path / "economy.json"
    f.write_text(json.dumps({"_comment": "整份文件的说明", "stamina": {"cap": 100, "regen": {"min": 5}},
                             "gear": {"note": "装备说明", "rod": [{"tier": 1, "price": 0}, {"tier": 2, "price": 800}]},
                             "species": {"jiyu": {"name": "鲫鱼", "big": [1, 2]}, "liyu": {"name": "鲤鱼", "big": [3, 4]}}},
                            ensure_ascii=False), encoding="utf-8")
    t = {name: (cols, rows, notes) for name, cols, rows, notes in qa_index.json_tables(f)}
    assert t["economy/gear.rod"][0] == ["tier", "price"] and t["economy/gear.rod"][1][1] == ["2", "800"]
    assert t["economy/gear.rod"][2] == ["装备说明"]  # 继承所在那一节的注释，不带文件最外层的
    assert ["regen.min", "5"] in t["economy/stamina"][1]
    assert t["economy/species"][0][0] == "键" and ["liyu", "鲤鱼", "[3, 4]"] in t["economy/species"][1]
    assert t["economy"][2] == ["整份文件的说明"]


def test_md_defect_reads_template_fields():
    rec = qa_index.md_defect("# D003 回包丢失重复到账\n\n- 模块：广告领奖\n- 严重程度：P1\n- 归类：幂等\n\n## 现象\n到账两次\n", "D003-回包")
    assert (rec["id"], rec["title"], rec["module"], rec["severity"], rec["resolution"]) == ("D003", "回包丢失重复到账", "广告领奖", "P1", "幂等")
    assert "现象：到账两次" in rec["desc"]


def test_map_defect_columns_zentao_and_jira():
    zt = qa_index.map_defect_columns(["Bug编号", "所属模块", "Bug标题", "严重程度", "Bug状态", "重现步骤"])
    jira = qa_index.map_defect_columns(["Issue key", "Summary", "Component/s", "Priority", "Status", "Description"])
    assert zt["title"] == 2 and zt["module"] == 1 and jira["title"] == 1 and jira["module"] == 2


def test_tokens_chinese_bigrams_and_words():
    assert qa_index.tokens("营业款 pondDouble 3") == ["营业", "业款", "ponddouble", "3"]


def test_field_words_split_camel_case():
    assert "unlock=解锁" in qa_index.field_words(["unlockPondLv"]) and "price=价格" in qa_index.field_words(["price"])


# ── 版本回归 ──

def test_read_cases_and_level(tmp_path):
    f = tmp_path / "旧用例.csv"
    f.write_text("说明行\n用例编号,所属模块,用例标题,优先级,预期结果\nC1,商店,买竿扣金币,P1,金币减少\n,,,,\n", encoding="utf-8")
    cases = regress.read_cases(f)
    assert cases == [{"id": "C1", "title": "买竿扣金币", "module": "商店", "level": "P1", "expect": "金币减少"}]
    assert regress.level_hit("P1", ["高"]) and regress.level_hit("中", ["中"]) and not regress.level_hit("P3", ["高", "中"])
