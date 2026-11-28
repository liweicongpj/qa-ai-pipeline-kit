"""扫描密钥、框架自检（临时文件夹里用示例项目把整条流程走一遍）。"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from .common import CONFIG, EXAMPLE, KIT, LEAK_PATTERNS, MAX_FILE, TEXT_EXT, out, read, read_table, run, set_field, today, write
from .workspace import Ws, cmd_remote, find_ws, git_commit
from .ingest import cmd_ingest
from .corpus import cmd_corpus, cmd_embed, cmd_index, corpus_changes, index_path, index_pending, open_index, retrieved_context
from .wiki import CATALOG, COUPLING, OBSOLETE, annotate_obsolete, cmd_combos, cmd_coupling, cmd_wiki, coupling_gaps, normalize_modules, obsolete_problems, pairwise, suggest_modules, todo_items, unmerged_reqs, wiki_check
from .reqs import cmd_accept, cmd_defect, cmd_req, cmd_review, defect_info, new_req, parse_points, pending_change
from .prompts import cmd_prompt, last_run, triage_target, untriaged_runs
from .gen import bench_runs, cmd_bench
from .steps import cmd_gen
from .access import cmd_access, cmd_exec, cmd_impact
from .autotest import cmd_autotest
from .metrics import cmd_metrics
from .regress import cmd_regress, regress_file, write_regress
from .status import daily, onboarding
from .start import cmd_init, cmd_target


def scan_leaks(root: Path) -> list[str]:
    found = []
    for p in root.rglob("*"):
        if not p.is_file() or any(x in p.parts for x in (".git", "__pycache__", ".pytest_cache", "venv", ".venv", "node_modules")):
            continue
        if p.suffix.lower() not in TEXT_EXT or p.stat().st_size > MAX_FILE:
            continue
        text = read(p)
        for pat, what in LEAK_PATTERNS:
            for m in re.finditer(pat, text):
                found.append(f"{p.relative_to(root)}:{text.count(chr(10), 0, m.start()) + 1}  {what}：{m.group(0)[:40]}")
    return found


def cmd_check(args) -> int:
    targets = [("框架", KIT)]
    root = find_ws(args)
    if root:
        targets.append(("工作区", root))
    bad = 0
    for name, r in targets:
        found = scan_leaks(r)
        out(f"{name}（{r}）：" + ("没发现密钥、AppID、云环境 ID" if not found else f"发现 {len(found)} 处："))
        for f in found:
            out("   · " + f)
        bad += len(found)
    return 1 if bad else 0


def cmd_selftest(args) -> int:
    """在临时文件夹里用示例项目把整条管线走一遍（模型用假模型，不花钱），不碰你的工作区。"""
    try:
        import fastapi, uvicorn, httpx, pytest  # noqa: F401
    except Exception:
        out("自检要用示例服务，需要 fastapi、uvicorn、httpx、pytest：先运行 python qa.py setup")
        return 1
    tmp = Path(tempfile.mkdtemp(prefix="qa-selftest-")).resolve()
    root = tmp / "示例工作区"
    fails = []

    def step(name, fn):
        import traceback
        detail = ""
        try:
            r = fn()
            ok = r in (None, 0, True)
        except Exception as e:  # noqa: BLE001
            ok = False
            frames = [f for f in traceback.extract_tb(e.__traceback__) if f.filename.endswith(("qa.py", "qa_index.py", "qa_models.py"))]
            where = f"（{Path(frames[-1].filename).name} 第 {frames[-1].lineno} 行：{(frames[-1].line or '').strip()[:120]}）" if frames else ""
            detail = f"{type(e).__name__}: {str(e)[:300]}{where}"
            out(f"     {detail}")
        out(f"  {'✅' if ok else '❌'} {name}")
        if not ok:
            fails.append(name)
            if os.environ.get("GITHUB_ACTIONS"):  # GitHub 上不登录看不到日志，失败项写成页面上的注释，谁都能看到
                msg = f"{name}：{detail or '检查结果不对'}".replace("%", "%25").replace("\r", "").replace("\n", "%0A")
                print(f"::error title=自检没通过::{msg}", flush=True)

    ns = lambda **kw: argparse.Namespace(ws=str(root), **kw)
    out("在临时文件夹里用示例项目走一遍：")
    step("建工作区（示例）", lambda: cmd_init(argparse.Namespace(dir=str(root), name=None, example=True, force=False, no_register=True)))
    cfg = json.loads(read(root / CONFIG))  # 自检换一个不常用的端口，免得和正在跑的服务撞上；配两个假模型
    t = cfg["targets"]["default"]
    t["base_url"], t["start_cmd"] = "http://127.0.0.1:8790", t["start_cmd"].replace("--port 8000", "--port 8790")
    cfg["models"] = {"假模型甲": {"provider": "mock", "seed": "甲", "price_in": 1.0, "price_out": 5.0}, "假模型乙": {"provider": "mock", "seed": "乙"}}
    cfg["stage_models"] = {"testpoints": "假模型甲", "triage": "假模型乙"}
    write(root / CONFIG, json.dumps(cfg, ensure_ascii=False, indent=2))
    step("上手进度停在“生成知识库”", lambda: next(s for s in onboarding(Ws(root)) if not s["done"])["id"] == "wiki")

    def fake_wiki():
        write(root / "wiki" / "index.md", "# 目录\n\n- modules/广告领奖.md：广告领奖规则\n- modules/渔具店.md：渔具店\n"
                                          f"- {COUPLING}：模块之间共用的数值\n- risks/风险清单.md：通用风险\n")
        write(root / "wiki" / "modules" / "广告领奖.md", "# 广告领奖\n\n来源：raw/广告领奖规则.md\n\n- 每日 0 点重置（raw/广告领奖规则.md）\n")
        write(root / "wiki" / "modules" / "渔具店.md", "# 渔具店\n\n来源：raw/渔具店规则.md\n\n- 金币不能为负（raw/渔具店规则.md）\n")
        write(root / "wiki" / COUPLING, "# 耦合关系\n\n来源：raw/广告领奖规则.md、raw/渔具店规则.md\n\n"
                                       "| 共用的数值或状态 | 会改它的模块 | 会读它的模块 | 约束 | 来源 |\n|---|---|---|---|---|\n"
                                       "| 金币 | 广告领奖、渔具店 | 对账 | 不能为负，向下取整 | raw/渔具店规则.md |\n")
        write(root / "wiki" / "risks" / "风险清单.md", "# 风险清单\n\n来源：raw/常见缺陷.md\n\n## 幂等\n- 回包丢失重试\n")
        write(root / "wiki" / "log.md", f"# 变更记录\n\n- {today()} 初版\n")
        return cmd_wiki(ns(action="check", message=None, feedback=None, force=False))
    step("知识库格式检查", fake_wiki)
    step("提交知识库", lambda: cmd_wiki(ns(action="commit", message="初版", feedback=None, force=False)))

    def todo_flow():
        mod = root / "wiki" / "modules" / "渔具店.md"
        risk = root / "wiki" / "risks" / "风险清单.md"
        write(mod, read(mod) + "\n## 待确认\n\n- 跳档要不要补中间档差价（raw/渔具店规则.md）→ 待确认\n")
        write(risk, read(risk) + "\n## 待确认\n\n- 跳档要不要补中间档差价（raw/常见缺陷.md）→ 待确认\n")
        items = todo_items(Ws(root))
        write(mod, read(mod).split("\n## 待确认")[0] + "\n")
        write(risk, read(risk).split("\n## 待确认")[0] + "\n")
        return len(items) == 1  # 标题不算，同一个问题抄在两页只算一次
    step("待确认只数真正的问题：标题不算、两页重复的只算一次", todo_flow)

    def review_flow():
        d = Ws(root).req_dir("R001")
        write(d / "测试点.md", "| 编号 | 测试点 | 风险等级 | 类型 | 依据 | 怎么验证 |\n|---|---|---|---|---|---|\n"
                             "| 1 | 同一宝箱重复领取 | 高 | 异常 | wiki/risks/风险清单.md | 对账 |\n"
                             "| 2 | 23:59 看完 0 点后领 | 中 | 边界 | wiki/modules/广告领奖.md | 日志 |\n"
                             "| 3 | 宝箱可以分享给好友 | 低 | 正常 | 无 | 界面 |\n")
        assert cmd_review(ns(req="R001", force=False)) == 0
        txt = read(d / "审核.md").replace("审核耗时（分钟）：", "审核耗时（分钟）：12")
        txt = txt.replace("| 1 | 同一宝箱重复领取 |  |  |  |", "| 1 | 同一宝箱重复领取 | 采纳 |  |  |")
        txt = txt.replace("| 2 | 23:59 看完 0 点后领 |  |  |  |", "| 2 | 23:59 看完 0 点后领 | 改后采纳 |  | 23:59 看完第 3 次，0 点后领木箱 |")
        txt = txt.replace("| 3 | 宝箱可以分享给好友 |  |  |  |", "| 3 | 宝箱可以分享给好友 | 否决 | 不符合规则 |  |")
        write(d / "审核.md", txt.replace("## 审核时补充的测试点\n- ", "## 审核时补充的测试点\n- 免费次数算不算进累计"))
        assert cmd_accept(ns(req="R001")) == 0
        m = Ws(root).req_meta("R001")
        return m["generated"] == 3 and m["fabricated"] == 1 and m["added"] == 1 and m["final"] == 3
    step("需求：审核表 → 人工审核 → 定稿，指标正确", review_flow)
    step("耦合提醒：需求只测了广告领奖，没提到和渔具店共用的“金币” → 查出 1 处",
         lambda: len(coupling_gaps(Ws(root), "R001", parse_points(Ws(root).req_dir("R001") / "最终测试点.md"))) == 1
         and cmd_coupling(ns(req="R001")) == 0)

    def combos_flow():
        f = root / "autotest" / "组合因素.md"
        write(f, "| 因素 | 取值 |\n|---|---|\n| 商品价 | 0、1、500、3000 |\n| 券 | 无券、满减券、折扣券、两张叠加 |\n| 会员 | 否、是 |\n| 数量 | 1、2、99 |\n")
        assert cmd_combos(ns(file=str(f))) == 0
        rows = pairwise([("商品价", ["0", "1", "500", "3000"]), ("券", ["无", "满减", "折扣", "叠加"]), ("会员", ["否", "是"]), ("数量", ["1", "2", "99"])])
        pairs = {(i, r[i], j, r[j]) for r in rows for i in range(4) for j in range(i + 1, 4)}
        need = 4 * 4 + 4 * 2 + 4 * 3 + 4 * 2 + 4 * 3 + 2 * 3
        return len(pairs) == need and len(rows) < 4 * 4 * 2 * 3
    step("两两组合：4 个因素全组合 96 种，生成的组合覆盖全部两两搭配且行数少得多", combos_flow)

    def gen_flow():
        w = Ws(root)
        new_req(w, "渔具店特惠", EXAMPLE / "需求-广告宝箱.md", ["广告领奖"], quiet=True)
        w.save()
        assert cmd_gen(ns(what="testpoints", req="R002", model=None, force=False)) == 0
        assert cmd_review(ns(req="R002", force=False)) == 0
        assert "模型名称：假模型甲" in read(Ws(root).req_dir("R002") / "审核.md")  # 审核表自动记下是哪个模型出的
        return len(parse_points(Ws(root).req_dir("R002") / "测试点.md")) >= 4
    step("直接调模型生成测试点（假模型），记下用量", gen_flow)

    def bench_flow():
        w = Ws(root)
        new_req(w, "模型对比用", EXAMPLE / "需求-广告宝箱.md", ["广告领奖"], quiet=True)
        w.save()
        assert cmd_bench(ns(action="testpoints", req="R003", models="假模型甲,假模型乙", repeat=1, stamp=None, label=None, force=False)) == 0
        meta = bench_runs(Ws(root), "R003")[-1]
        bdir = root / meta["dir"]
        assert cmd_bench(ns(action="report", req="R003", models=None, repeat=1, stamp=None, label=None, force=False)) == 1  # 没审完不揭晓
        for e in meta["entries"]:
            rv = bdir / f"审核_{e['label']}.md"
            txt = read(rv).replace("审核耗时（分钟）：", "审核耗时（分钟）：5")
            txt = re.sub(r"^(\| \d+ \| [^|]+ \|)  \|  \|  \|$", r"\1 采纳 |  |  |", txt, flags=re.M)
            write(rv, txt)
        assert cmd_bench(ns(action="report", req="R003", models=None, repeat=1, stamp=None, label=None, force=False)) == 0
        rep = read(bdir / "对比报告.md")
        assert "假模型甲" in rep and "假模型乙" in rep and "100%" in rep
        first = meta["entries"][0]["label"]
        assert cmd_bench(ns(action="adopt", req="R003", models=None, repeat=1, stamp=None, label=first, force=False)) == 0
        return cmd_accept(ns(req="R003")) == 0
    step("模型对比：两个模型 → 盲审 → 揭晓报告 → 采用其中一份并定稿", bench_flow)

    def defect_flow():
        assert cmd_defect(ns(action="new", title="回包丢失重试重复到账", req="R001", stage="测试", from_test=None, ids=[])) == 0
        f = Ws(root).defect_files()[0]
        txt = read(f)
        for k, val in (("严重程度", "P1"), ("被最终测试点覆盖", "否"), ("归类", "幂等")):
            txt = set_field(txt, k, val)
        write(f, txt.replace("## 现象\n", "## 现象\n领奖回包丢失后重试，奖励到账两次\n", 1))
        assert "广告领奖" in defect_info(f)["module"]
        assert any("待回流" in x for x in daily(Ws(root)))
        assert cmd_prompt(ns(name="feedback", req=None, force=False, target=None)) == 0
        assert cmd_wiki(ns(action="commit", message="回流 D001", feedback="D001,R001", force=False)) == 0
        return not any("待回流" in x for x in daily(Ws(root)))
    step("缺陷登记（自动带模块）→ 回流 → 标记已回流", defect_flow)

    def merge_flow():
        import contextlib
        import io
        w = Ws(root)
        assert "R001" in unmerged_reqs(w) and any("还没并进知识库" in x for x in daily(w))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            assert cmd_prompt(ns(name="wiki-merge", req="R001", force=False, target=None)) == 0
        assert "宝箱可以分享给好友" in buf.getvalue()  # 审核时因“不符合规则”否决的点要带上，告诉 AI 哪里理解错了
        assert cmd_prompt(ns(name="wiki-merge", req="R002", force=False, target=None)) == 1  # 没定稿的不能并
        mod = root / "wiki" / "modules" / "渔具店.md"
        write(mod, read(mod) + "- 购买第 2 档起要口碑达标（需求 R001）\n")
        assert cmd_wiki(ns(action="commit", message="并入需求 R001", feedback=None, merged="R001", force=False)) == 0
        assert cmd_req(ns(action="merged", title="R003", src=None, module=None)) == 0  # 没有新规则的直接标记
        return not unmerged_reqs(Ws(root)) and not any("还没并进知识库" in x for x in daily(Ws(root)))
    step("需求定稿后：提醒把新规则并进知识库 → 任务说明带上被否决的错误理解 → 提交后不再提醒", merge_flow)

    def req_from_file():
        f = tmp / "禅道需求单.html"
        write(f, "<html><body><h1>鱼竿口碑门槛</h1><table><tr><th>档位</th><th>口碑</th></tr>"
                 "<tr><td>2</td><td>50</td></tr></table><script>var x=1</script></body></html>")
        w = Ws(root)
        d = new_req(w, "鱼竿口碑门槛", f, ["渔具店"], quiet=True)
        w.save()
        txt = read(d / "需求.md")
        return "鱼竿口碑门槛" in txt and "50" in txt and "<td>" not in txt and "var x" not in txt and "来源：禅道需求单.html" in txt
    step("需求文件可以是网页 / Word / PDF：导入时转成文字", req_from_file)

    def auto_flow():
        shutil.copyfile(EXAMPLE / "sample_tests" / "test_sample.py", root / "autotest" / "tests" / "test_sample.py")
        shutil.copyfile(EXAMPLE / "sample_tests" / "invariants.py", root / "autotest" / "invariants.py")
        r = cmd_autotest(ns(action="run", start=True, k=None, target=None))
        lr = last_run(Ws(root))
        return r == 0 and lr["total"] >= 10 and not lr["failed"]
    step("接口自动化：临时启动示例服务 → 跑样例用例（每个用例后自动查不变量）全过 → 关服务", auto_flow)

    def invariant_catches():
        # 故意写一个把金币改成负数也不报错的“用例”，不变量检查要在用例结束时把它抓出来
        write(root / "autotest" / "tests" / "test_zz_bad.py",
              "import httpx\n\ndef test_buy_then_break(base_url):\n"
              "    with httpx.Client(base_url=base_url) as c:\n        c.post('/reset')\n"
              "        c.post('/shop/buy', json={'player': 'A', 'kind': 'rod', 'tier': 2})\n")
        inv = root / "autotest" / "invariants.py"
        good = read(inv)
        write(inv, good.replace("START_GOLD = 1000", "START_GOLD = 999999"))  # 让对账基准对不上，模拟“价格算错”（长度也变，免得撞上编译缓存）
        cmd_autotest(ns(action="run", start=True, k="buy_then_break", target=None))
        lr = last_run(Ws(root))
        write(inv, good)
        (root / "autotest" / "tests" / "test_zz_bad.py").unlink()
        return len(lr["failed"]) == 1
    step("不变量兜底：用例本身没断言，账对不上也会在用例结束时被抓出来", invariant_catches)

    def untriaged_flow():
        t = root / "autotest" / "tests" / "test_zz_fix.py"
        write(t, "def test_zz_fix(base_url):\n    assert 1 + 1 == 3\n")
        rc_fail = cmd_autotest(ns(action="run", start=True, k="zz_fix", target=None, exit_code=True))  # Jenkins 用：有失败退出码 1
        failing = last_run(Ws(root))
        time.sleep(1.2)
        write(t, "def test_zz_fix(base_url):\n    assert 1 + 1 == 2  # 改成通过\n")  # 没写归因就把用例改成通过
        cmd_autotest(ns(action="run", start=True, k="zz_fix", target=None))
        w = Ws(root)
        lr, tt = last_run(w), triage_target(w, None)
        ok = (rc_fail == 1 and lr.get("untriaged_fix") and any("改了用例才通过" in x for x in daily(w)) and tt and tt["dir"] == failing["dir"])
        why = (f"第一次 {failing['dir']} 失败 {failing['failed']}；第二次 {lr['dir']} 失败 {lr['failed']}、修好 {lr.get('fixed')}、"
               f"untriaged_fix={lr.get('untriaged_fix')}；要归因的 {tt and tt['dir']}；用例改动时间 {t.stat().st_mtime:.2f}，第一次记录时间 {failing['time']}")
        for r in untriaged_runs(w):  # 补上归因，提醒消失
            write(root / r["dir"] / "归因.md", "用例写错了：1 + 1 应该等于 2\n")
        t.unlink()
        assert ok, why
        return not untriaged_runs(Ws(root))
    step("失败后没写归因就改用例通过：运行摘要和待办都会提醒，补上归因后消失", untriaged_flow)
    step("第二个被测服务可以单独配置", lambda: cmd_target(ns(action="set", name="admin", url="http://127.0.0.1:9", start=None, cwd=None, health=None)) == 0
         and "admin" in Ws(root).cfg["targets"] and (root / "autotest" / "tests" / "admin").exists())
    step("上手进度全部完成", lambda: all(s["done"] for s in onboarding(Ws(root))))
    def push_flow():
        bare = tmp / "项目仓库.git"
        run(["git", "init", "-q", "--bare", str(bare)])
        assert cmd_remote(ns(action="set", url=str(bare), no_auto=False)) == 0
        write(root / "raw" / "新资料.md", "# 新资料\n")
        git_commit(Ws(root), "试一下自动推送")
        local = run(["git", "rev-parse", "HEAD"], cwd=root).stdout.strip()
        remote = run(["git", "--git-dir", str(bare), "rev-parse", "HEAD"]).stdout.strip()
        (root / "raw" / "新资料.md").unlink()
        git_commit(Ws(root), "删掉试推送的资料")
        return bool(local) and local == remote
    step("设了项目仓库后，提交会自动推送过去", push_flow)
    step("指标（含按模块、模型用量）", lambda: cmd_metrics(ns()) == 0 and "编造 1 条" in read(root / "metrics" / "指标.md")
         and "假模型甲" in read(root / "metrics" / "指标.md"))
    step("资料改动后提示更新知识库", lambda: (write(root / "raw" / "广告领奖规则.md", "# 改了\n"), any("知识库要更新" in x for x in daily(Ws(root))))[1])
    step("工作区里没有密钥", lambda: not scan_leaks(root))

    def corpus_flow():
        try:
            import docx as docx_lib
            import openpyxl
        except ImportError:
            out("     （没装 python-docx / openpyxl，跳过这一项；python qa.py setup 后再测）")
            return True
        src = tmp / "资料"
        for sub in ("策划", "配置", "缺陷"):
            (src / sub).mkdir(parents=True)
        doc = src / "策划" / "营业款.docx"
        d = docx_lib.Document()
        d.add_heading("营业款设计", 1)
        d.add_heading("翻倍规则", 2)
        d.add_paragraph("营业款翻倍封顶为参考场费的两倍，翻完本金清零。")
        d.add_heading("领取", 2)
        d.add_paragraph("领取向下取整，零头留到下次。")
        d.save(doc)
        wb = openpyxl.Workbook()
        sh = wb.active
        sh.title = "鱼竿"
        for row in (["id", "name", "price"], ["int", "string", "int"], ["编号", "名称", "价格"], [1, "竹节入门竿", 0], [6, "龙骨巨物竿", 38000]):
            sh.append(row)
        wb.save(src / "配置" / "装备.xlsx")
        with open(src / "缺陷" / "禅道导出.csv", "w", newline="", encoding="gbk") as fh:  # 禅道默认导出 GBK
            fh.write("Bug编号,所属模块,Bug标题,严重程度,Bug状态,重现步骤\n1,/营业款,营业款翻倍后本金没有清零,1,激活,翻倍后再领一次\n"
                     "2,/体力,体力跨天没重置,3,已关闭,改到第二天\n")
        write(src / "配置" / "economy.json", json.dumps({  # 很多游戏的配置是 JSON：对象数组、结构相近的子项、单个数值三种都要能查
            "version": 3, "stamina": {"cap": 100, "regen": {"min": 5}},
            "gear": {"note": "装备", "rod": [{"tier": 1, "price": 0, "repMin": 0}, {"tier": 2, "price": 800, "repMin": 50}]},
            "species": {"jiyu": {"name": "鲫鱼", "median": 0.6}, "liyu": {"name": "鲤鱼", "median": 3}},
            "legend": {"list": [{"id": "L01", "species": "jiyu"}, {"id": "L02", "species": "liyu"}]}}, ensure_ascii=False))
        nsc = lambda **kw: ns(**kw)
        assert cmd_corpus(nsc(action="add", target=str(src / "策划"), kind="doc", name="策划案")) == 0
        assert cmd_corpus(nsc(action="add", target=str(src / "配置"), kind="config", name="配置表")) == 0
        assert cmd_corpus(nsc(action="add", target=str(src / "缺陷"), kind="defect", name="缺陷")) == 0
        assert cmd_embed(nsc(action="set", provider="mock", model="假向量", url=None, key_env=None)) == 0
        assert cmd_index(nsc(action="build", rebuild=False, no_embed=False)) == 0
        w = Ws(root)
        idx = open_index(w)
        try:
            doc_hit = idx.search("营业款翻倍 封顶", ("doc",), k=1, emb=w.cfg.get("embedding"))
            t = idx.table("装备")
            rows, _ = idx.table_rows(t["name"], [("name", "龙骨巨物竿")], 5)
            bug = idx.search("本金没有清零", ("defect",), k=1)
        finally:
            idx.close()
        ok = (doc_hit and "翻倍规则" in doc_hit[0]["title"] and rows and rows[0][1][2] == "38000"
              and bug and "本金" in bug[0]["title"] and ".qa/index.sqlite" in read(root / ".gitignore"))
        time.sleep(1.1)
        d.add_paragraph("新增：领取冷却 10 秒。")
        d.save(doc)
        stale = index_pending(w)
        ctx = retrieved_context(w, "营业款翻倍")
        return bool(ok) and len(stale["changed"]) == 1 and "翻倍规则" in ctx and "装备" in ctx
    step("大批资料：Word / Excel / 禅道 CSV 建索引 → 检索、精确查表、查缺陷 → 改文件后提示重建 → 生成时带上检索内容", corpus_flow)

    def idx_ok_json(w: Ws) -> bool:
        idx = open_index(w)
        try:
            rod, _ = idx.table_rows("economy/gear.rod", [("tier", "2")], 5)
            cols = idx.table("economy/gear.rod")["columns"]
            cap, _ = idx.table_rows("economy/stamina", [("键", "regen.min")], 5)
            fish, _ = idx.table_rows("economy/species", [("键", "liyu")], 5)
        finally:
            idx.close()
        return bool(rod and rod[0][1][cols.index("repMin")] == "50" and cap and cap[0][1][1] == "5"
                    and fish and fish[0][1][1] == "鲤鱼")
    step("JSON 配置：对象数组、结构相近的子项、深层单个数值都拆成表，cfg show 能精确查", lambda: idx_ok_json(Ws(root)))

    def links_flow() -> bool:
        import contextlib
        import io
        from .corpus import cmd_cfg
        if not index_path(Ws(root)).exists():  # 上一项因为没装 python-docx / openpyxl 跳过了
            out("     （没建资料索引，跳过这一项）")
            return True
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc1 = cmd_cfg(ns(action="links", words=["economy/legend.list"], where=None, cols=None, expand=0, limit=20, n=8))
            rc2 = cmd_cfg(ns(action="show", words=["economy/legend.list"], where=["id=L02"], cols=None, expand=1, limit=20, n=8))
        text = buf.getvalue()
        return (rc1 == 0 and rc2 == 0 and "[高] species → economy/species.键" in text
                and "表 economy/species 里对上 1 行" in text and "鲤鱼" in text and "鲫鱼" not in text)
    step("配置表关联：自动认出名鱼表 species → 鱼种表，cfg show --expand 把对应的行一起带出来", links_flow)

    def builtin_flow():
        import contextlib
        import io
        w = Ws(root)
        idx = open_index(w)
        try:
            own_bug = idx.search("回包丢失 重复到账", ("defect",), k=3)
            own_req = idx.search("鱼竿口碑门槛", ("doc",), k=3)
        finally:
            idx.close()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            shown = cmd_index(ns(action="show", file="营业款.docx", rebuild=False, no_embed=False))
        return (any(h["corpus"] == "本工作区缺陷" for h in own_bug) and any(h["corpus"] == "本工作区需求" for h in own_req)
                and shown == 0 and "翻倍封顶" in buf.getvalue())
    step("本工作区的需求和缺陷自动进索引；index show 能看 Word 全文", builtin_flow)

    def corpus_wiki_flow():
        w = Ws(root)
        cc = corpus_changes(w)  # 建索引时已把当时的文档记为基线，刚才又改了营业款.docx
        assert cc and len(cc["changed"]) == 1 and any("知识库可能要跟着更新" in x for x in daily(w))
        assert cmd_prompt(ns(name="wiki-update", req=None, force=False, target=None)) == 1  # 索引还没更新，先 index
        assert cmd_index(ns(action="build", rebuild=False, no_embed=False, file=None)) == 0
        assert cmd_prompt(ns(name="wiki-update", req=None, force=False, target=None)) == 0
        assert cmd_wiki(ns(action="commit", message="按资料更新", feedback=None, merged=None, force=False)) == 0
        cc = corpus_changes(Ws(root))
        return cc is not None and not (cc["new"] or cc["changed"] or cc["deleted"])
    step("资料文件夹里的文档变了：提醒更新知识库 → 先更新索引 → 任务说明 → 提交后清零", corpus_wiki_flow)

    def quiet(fn):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            r = fn()
        return r, buf.getvalue()

    def catalog_flow():
        write(root / "wiki" / CATALOG, "# 模块清单\n\n来源：raw/广告领奖规则.md、raw/渔具店规则.md\n\n| 模块 | 上级模块 | 别名 | 说明 |\n|---|---|---|---|\n"
                                       "| 广告领奖 | 运营 | 广告宝箱、看广告 | 看广告领奖励 |\n| 渔具店 | 商城 | 商店、shop | 买竿 |\n| 签到 | 运营 | 每日签到 | 还没整理 |\n")
        idx_md = root / "wiki" / "index.md"
        write(idx_md, read(idx_md) + f"- {CATALOG}：全项目统一的模块名\n")
        w = Ws(root)
        mods, hints, unknown = normalize_modules(w, ["商店", "运营", "抽奖"])
        assert mods == ["渔具店"] and hints and hints[0][0] == "运营" and set(hints[0][1]) == {"广告领奖", "签到"} and unknown == ["抽奖"]
        sug = [s[0] for s in suggest_modules(w, "去商店买鱼竿要扣金币")]
        assert sug and sug[0] == "渔具店", sug
        r, txt = quiet(lambda: cmd_req(ns(action="new", title="签到送竿", src=None, module="运营,抽奖", set=None)))
        rid = Ws(root).req_dirs()[-1].name.split("-")[0]
        assert r == 0 and "框架推荐" in txt and any("抽奖" in x and "模块清单里" in x for x in daily(Ws(root)))
        assert cmd_req(ns(action="modules", title=rid, src=None, module=None, set="每日签到,商店")) == 0
        assert Ws(root).req_meta(rid)["modules"] == ["签到", "渔具店"]  # 别名统一成标准名
        r, txt = quiet(lambda: cmd_prompt(ns(name="wiki-build", req=None, force=False, target=None, module=None)))
        r2, txt2 = quiet(lambda: cmd_prompt(ns(name="wiki-build", req=None, force=False, target=None, module="每日签到")))
        probs, _ = wiki_check(Ws(root))
        return "签到" in txt and "这次只整理这些模块：签到" in txt2 and not probs
    step("模块清单：别名统一成标准名、大模块交给推荐细化、对不上的提醒；按需求内容推荐模块；知识库按模块分批整理", catalog_flow)

    def obsolete_flow():
        write(root / "wiki" / OBSOLETE, "# 过时资料\n\n来源：资料索引里的策划案\n\n| 资料 | 章节 | 过时的原文 | 被什么替代 | 现行规则在哪 | 日期 |\n|---|---|---|---|---|---|\n"
                                        "| 营业款.docx | 领取 | 零头留到下次 | R001 | wiki/modules/广告领奖.md | 2026-11-27 |\n")
        idx_md = root / "wiki" / "index.md"
        write(idx_md, read(idx_md) + f"- {OBSOLETE}：已经不用的规则\n")
        w = Ws(root)
        idx = open_index(w)
        try:
            hits = idx.search("领取 向下取整 冷却", ("doc",), k=5)
        finally:
            idx.close()
        part = next(h for h in annotate_obsolete(w, hits) if "领取" in h["title"])
        ok_part = ("零头留到下次【⚠️已过时" in part["text"] and "冷却" in part["text"].split("【⚠️已过时")[1]  # 同一节里还在用的规则不标
                   and not part["text"].startswith("【"))
        write(root / "wiki" / OBSOLETE, read(root / "wiki" / OBSOLETE) + "| 营业款.docx | 翻倍规则 |  | R001 | wiki | 2026-11-27 |\n"
                                                                     "| 营业款.docx | 领取 | 根本没有这一句 | R001 | wiki | 2026-11-27 |\n")
        whole = annotate_obsolete(Ws(root), [{"path": part["path"], "title": "营业款 > 翻倍规则", "text": "封顶两倍"}, part])
        probs = obsolete_problems(Ws(root))
        write(root / "wiki" / OBSOLETE, "\n".join(read(root / "wiki" / OBSOLETE).splitlines()[:-1]) + "\n")  # 删掉对不上原文的那条
        return (ok_part and whole[-1]["text"].startswith("【⚠️这一节已整体过时") and len(probs) == 1 and "根本没有" in probs[0]
                and not obsolete_problems(Ws(root)))
    step("过时资料：只标过时的那一句（同节其他规则照常）、整节作废的排到后面、登记对不上原文会报出来", obsolete_flow)

    def change_flow():
        w = Ws(root)
        d = w.req_dir("R001")
        src = tmp / "广告宝箱_改.md"
        write(src, read(d / "需求.md") + "\n## 新规则\n\n二档鱼竿要口碑 50 才能买。\n")
        assert cmd_req(ns(action="refresh", title="R001", src=str(src), module=None, set=None)) == 0
        w = Ws(root)
        ch = pending_change(w, "R001")
        assert ch and any("prompt req-change R001" in x for x in daily(w))
        r, txt = quiet(lambda: cmd_prompt(ns(name="req-change", req="R001", force=False, target=None)))
        assert r == 0 and "回归" in txt
        write(root / ch["dir"] / "测试点变更.md",
              "| 编号 | 变更（新增/修改/作废/回归） | 原编号 | 测试点 | 风险等级 | 类型 | 依据 | 怎么验证 |\n|---|---|---|---|---|---|---|---|\n"
              "| 1 | 新增 |  | 口碑 49 买二档被拒，待配表：二档口碑门槛 | 高 | 边界 | economy/gear.rod | 接口 |\n"
              "| 2 | 修改 | T01 | 同一宝箱重复领取只到账一次（含口碑变化） | 高 | 异常 | wiki | 对账 |\n"
              "| 3 | 作废 | T03 | 免费次数规则删了 | 低 | 正常 | 需求改动 | — |\n"
              "| 4 | 回归 | R003-T01 | 买一档竿不受口碑影响 | 中 | 回归 | 耦合表 | 接口 |\n")
        assert cmd_review(ns(req="R001", force=False)) == 0
        rv = root / ch["dir"] / "审核.md"
        txt = read(rv).replace("审核耗时（分钟）：", "审核耗时（分钟）：3")
        write(rv, re.sub(r"^(\| \d+ \| [^|]+ \|)  \|  \|  \|$", r"\1 采纳 |  |  |", txt, flags=re.M))
        assert cmd_accept(ns(req="R001")) == 0
        w = Ws(root)
        _, final = read_table(d / "最终测试点.md")
        nos = {r["编号"]: r["测试点"] for r in final}
        ok = ("含口碑变化" in nos.get("T01", "") and "T03" not in nos and any("口碑 49" in t for t in nos.values())
              and "R003-T01" in read(root / ch["dir"] / "回归清单.md") and "R001" in unmerged_reqs(w)
              and w.req_meta("R001")["cfg_pending"] == 1 and "economy/gear.rod" in w.req_meta("R001")["cfg_refs"])
        time.sleep(1.1)
        write(src, read(src) + "\n补充：三档要 100。\n")
        return ok and any("原文件改过了" in x for x in daily(Ws(root)))
    step("需求变更：重新导入 → 对比改动 → 测试点变更（新增 / 修改 / 作废 / 回归）→ 只审变动 → 合进最终测试点、出回归清单", change_flow)

    def cfg_late_flow():
        cfgf = tmp / "资料" / "配置" / "economy.json"
        data = json.loads(read(cfgf))
        data["gear"]["rod"][1]["repMin"] = 60
        write(cfgf, json.dumps(data, ensure_ascii=False))
        w = Ws(root)
        assert any("economy/gear.rod 改过了" in x for x in daily(w))
        r, txt = quiet(lambda: cmd_prompt(ns(name="cfg-check", req="R001", force=False, target=None)))
        assert r == 0 and "economy/gear.rod" in txt
        assert cmd_index(ns(action="build", rebuild=False, no_embed=False, file=None)) == 0
        assert cmd_req(ns(action="cfg-checked", title="R001", src=None, module=None, set=None)) == 0
        return not any("R001 引用的配置表" in x or "R001 有" in x for x in daily(Ws(root)))
    step("配置晚到：测试点引用的配置表改了会提醒校对 → 校对后记下新状态，提醒消失", cfg_late_flow)

    def api_late_flow():
        r, txt = quiet(lambda: cmd_prompt(ns(name="tests-plan", req=None, force=False, target="admin")))
        assert r == 0 and "等接口" in txt  # 接口文档还没有，也能先出用例清单
        _, t = Ws(root).target("admin")
        tf = root / "autotest" / t["tests"] / "test_admin_wait.py"
        write(tf, 'import pytest\n\n@pytest.mark.skip(reason="等接口")\ndef test_wait():\n    """R001-T02"""\n')
        assert not any("等接口" in x for x in daily(Ws(root)))
        write(root / "autotest" / t["api"] / "admin.md", "# admin 接口\n")
        ok = any("等接口" in x and "admin" in x for x in daily(Ws(root)))
        tf.unlink()
        (root / "autotest" / t["api"] / "admin.md").unlink()
        return ok
    step("接口没出：先出用例清单和骨架（标等接口），接口文档到了提醒补全", api_late_flow)

    def regress_flow():
        write(root / "autotest" / "tests" / "test_zz_reg.py", 'def test_zz_reg(base_url):\n    """R001-T01：重复领取只到账一次"""\n    assert True\n')
        cases = tmp / "旧用例.csv"
        write(cases, "用例编号,所属模块,用例标题,优先级,预期结果\nC1,商店,买鱼竿扣金币,P1,金币减少\nC2,抽奖,抽奖不出负数,P3,不为负\n")
        assert cmd_regress(ns(action="new", round="v1", modules=None, level=None, reqs="R001", cases=[str(cases)], target=None, start=False, force=False)) == 0
        f = regress_file(Ws(root), "v1")
        _, rows = read_table(f)
        assert any(r["自动化用例"] == "test_zz_reg::test_zz_reg" for r in rows) and any(r["模块"] == "渔具店" for r in rows)  # 旧用例的“商店”统一成渔具店
        assert cmd_regress(ns(action="run", round="v1", modules=None, level=None, reqs=None, cases=None, target=None, start=True, force=False)) == 0
        _, rows = read_table(f)
        auto_row, lr = next(r for r in rows if r["自动化用例"]), last_run(Ws(root))
        junit = root / lr["dir"] / "junit.xml" if lr else None
        names = [f"{tc.get('classname')}::{tc.get('name')}" for tc in ET.parse(junit).getroot().iter("testcase")] if junit and junit.exists() else "没有 junit.xml"
        assert auto_row["结果"] == "通过", (f"自动化那条结果“{auto_row['结果']}”备注“{auto_row['备注']}”；最近运行 {lr and lr['dir']}："
                                          f"共 {lr and lr.get('total')} 个、失败 {lr and lr.get('failed')}；junit 里的用例 {names}")
        assert cmd_regress(ns(action="check", round="v1", modules=None, level=None, reqs=None, cases=None, target=None, start=False, force=False)) == 1
        assert any("版本回归 v1" in x for x in daily(Ws(root)))
        for r in rows:
            if not r["结果"]:
                r["结果"], r["缺陷编号"] = ("失败", "D001") if "抽奖" in r["测试点"] else ("通过", "")
        write_regress(f, "v1", rows, "拼出时间：自检")
        ok = cmd_regress(ns(action="check", round="v1", modules=None, level=None, reqs=None, cases=None, target=None, start=False, force=False)) == 0
        (root / "autotest" / "tests" / "test_zz_reg.py").unlink()
        return ok and "通过率" in read(f.parent / "回归报告.md") and not any("版本回归 v1" in x for x in daily(Ws(root)))
    step("版本回归：定稿测试点 + 旧 Excel 用例拼回归清单 → 自动化部分自动填结果 → 手工填完 → 回归报告", regress_flow)

    def direct_flow():
        import sys as _sys
        cfg = json.loads(read(root / CONFIG))
        page = "# 广告领奖\n\n来源：raw/广告领奖规则.md\n\n- 每日 0 点重置（raw/广告领奖规则.md）\n- 资料里查到：{{tool:0}}\n"
        bad = ("===== 文件：wiki/modules/广告领奖.md =====\n# 广告领奖\n\n- 没写来源\n===== 文件结束 =====\n\n"
               "===== 文件：reqs/R001-偷改/需求.md =====\n不该写这里\n===== 文件结束 =====\n")
        good = f"===== 文件：wiki/modules/广告领奖.md =====\n{page}===== 文件结束 =====\n\n## 给人看的说明\n只补了一条\n"
        t_bad = "===== 文件：autotest/tests/test_zz_gen.py =====\ndef test_zz_gen(base_url)\n    assert True\n===== 文件结束 =====\n"
        t_good = ('===== 文件：autotest/tests/test_zz_gen.py =====\ndef test_zz_gen(base_url):\n    """R001-T01"""\n    assert True\n'
                  "===== 文件结束 =====\n")
        cfg["models"].update({
            "自动甲": {"provider": "mock", "seed": "自动甲", "replies": {"wiki-build": [bad, good], "tests-write": [t_bad, t_good]},
                     "tool_calls": {"wiki-build": [{"name": "search", "args": {"query": "广告 领奖"}}]}},
            "坏模型": {"provider": "mock", "seed": "坏", "errors": {"wiki-lint": ["error"]}},
            "好模型": {"provider": "mock", "seed": "好", "errors": {"wiki-lint": ["transient"]}, "replies": {"wiki-lint": "## 互相矛盾的规则\n- 没发现\n"}},
        })
        cfg["gen"] = {"retry_wait": 0}
        hook = tmp / "hook_review.py"
        write(hook, "import os\nopen('hook_review.txt', 'w', encoding='utf-8').write(os.environ['QA_REQ'])\n")
        cfg["hooks"] = {**(cfg.get("hooks") or {}), "after_review_ready": f'"{_sys.executable}" "{hook}"'}
        write(root / CONFIG, json.dumps(cfg, ensure_ascii=False, indent=2))
        g = lambda **kw: ns(**{"req": None, "model": None, "run": None, "target": None, "module": None, "review": False,
                               "no_tools": False, "force": False, **kw})
        # 1 知识库：越权写文件被拦下、格式检查不过发回去改、查资料的结果写进了页里；改动不自动提交
        assert cmd_gen(g(what="wiki-build", module="广告领奖", model="自动甲")) == 0
        text = read(root / "wiki" / "modules" / "广告领奖.md")
        assert "来源：" in text and "资料里查到：[1]" in text, text
        assert not (root / "reqs" / "R001-偷改").exists(), "越权写的文件不该落盘"
        assert "直接调模型整理知识库" in read(root / "wiki" / "log.md")
        st = run(["git", "-c", "core.quotepath=false", "status", "--porcelain"], cwd=root).stdout
        assert "wiki/modules/广告领奖.md" in st, f"知识库改动应该留给人审、不自动提交：{st}"
        # 2 出错：普通错误换下一个模型，临时故障等一会儿重试
        assert cmd_gen(g(what="wiki-lint", model="坏模型,好模型")) == 0
        recs = [json.loads(x) for x in read(root / ".qa" / "calls.jsonl").splitlines() if x.strip()]
        got = [(r["model"], r["ok"]) for r in recs if r["stage"] == "wiki-lint"]
        assert got == [("坏模型", False), ("好模型", False), ("好模型", True)], got
        assert list((root / "metrics").glob("知识库检查_*.md"))
        # 3 用例代码：语法错误发回去改，改好后 pytest 能收集
        assert cmd_gen(g(what="tests-write", model="自动甲")) == 0
        assert "def test_zz_gen(base_url):" in read(root / "autotest" / "tests" / "test_zz_gen.py")
        (root / "autotest" / "tests" / "test_zz_gen.py").unlink()
        # 4 出测试点 → 马上出审核表 → 触发“审核表生成后”钩子（可以挂通知脚本）
        w = Ws(root)
        rid = new_req(w, "自动流转", EXAMPLE / "需求-广告宝箱.md", ["广告领奖"], quiet=True).name.split("-")[0]
        w.save()
        assert cmd_gen(g(what="testpoints", req=rid, model="自动甲", review=True)) == 0
        assert (Ws(root).req_dir(rid) / "审核.md").exists()
        assert read(root / "hook_review.txt") == rid, "审核表生成后的钩子没触发"
        (root / "hook_review.txt").unlink()
        cfg = json.loads(read(root / CONFIG))
        cfg["hooks"].pop("after_review_ready")
        write(root / CONFIG, json.dumps(cfg, ensure_ascii=False, indent=2))
        return True
    step("每一步都能直接调模型：越权写文件被拦、检查不过让模型改、查资料、出错重试换模型、出测试点后自动出审核表并触发钩子", direct_flow)

    def blackbox_flow():
        bb = tmp / "无代码权限"
        assert cmd_init(argparse.Namespace(dir=str(bb), name="黑盒项目", example=False, access="blackbox", force=False, no_register=True)) == 0
        assert "黑盒项目" in read(bb / "README.md") and "对 AI 说" in read(bb / "README.md")  # 给人看的说明和导航
        ids_ = [s["id"] for s in onboarding(Ws(bb))]
        assert "exec" in ids_ and "tests" not in ids_ and "target" not in ids_
        shutil.copytree(root / "wiki", bb / "wiki", dirs_exist_ok=True)
        shutil.copytree(root / "raw", bb / "raw", dirs_exist_ok=True)
        w = Ws(bb)
        d = new_req(w, "示例", None, [], quiet=True)
        w.save()
        write(d / "最终测试点.md", "| 编号 | 测试点 | 风险等级 | 类型 |\n|---|---|---|---|\n| T01 | 重复领取只到账一次 | 高 | 异常 |\n| T02 | 跨 0 点重置 | 中 | 边界 |\n")
        nsb = lambda **kw: argparse.Namespace(ws=str(bb), **kw)
        assert cmd_exec(nsb(req="R001", round="v1", force=False)) == 0
        f = d / "执行记录_v1.md"
        txt = read(f).replace("| T01 | 重复领取只到账一次 |  |", "| T01 | 重复领取只到账一次 | 通过 |")
        write(f, txt.replace("| T02 | 跨 0 点重置 |  |", "| T02 | 跨 0 点重置 | 失败 |"))
        assert cmd_exec(nsb(req="R001", round="v1", force=False)) == 1  # 失败没填缺陷编号
        write(f, read(f).replace("| T02 | 跨 0 点重置 | 失败 |  |", "| T02 | 跨 0 点重置 | 失败 | D001 |"))
        return cmd_exec(nsb(req="R001", round="v1", force=False)) == 0 and next(s for s in onboarding(Ws(bb)) if s["id"] == "exec")["done"]
    step("无代码权限：上手步骤换成手工执行；执行清单 → 填结果 → 失败必须挂缺陷", blackbox_flow)

    def har_flow():
        fake_key = "sk" + "-" + "a" * 24  # 运行时拼出来的假密钥，源码里不留像密钥的字面值
        har = {"log": {"entries": [
            {"request": {"method": "POST", "url": "https://game.example.com/api/shop/buy?x=1", "queryString": [{"name": "x", "value": "1"}],
                         "postData": {"text": json.dumps({"item": 3, "token": fake_key})}},
             "response": {"status": 200, "content": {"text": '{"ok": true}'}}},
            {"request": {"method": "GET", "url": "https://game.example.com/api/me"}, "response": {"status": 200, "content": {"text": "{}"}}}]}}
        f = tmp / "抓包.har"
        write(f, json.dumps(har))
        assert cmd_ingest(ns(paths=[], api=False, target=None, refresh=False, redact=False, har=str(f))) == 0
        doc = read(root / "autotest" / "api" / "抓包_抓包.md")
        return "POST /api/shop/buy" in doc and "GET /api/me" in doc and fake_key not in doc
    step("抓包导入：HAR 整理成接口文档，密钥自动打码", har_flow)

    def source_flow():
        code = tmp / "代码"
        code.mkdir()
        run(["git", "init", "-q"], cwd=code)
        run(["git", "config", "user.email", "qa@localhost"], cwd=code)
        run(["git", "config", "user.name", "qa"], cwd=code)
        write(code / "shop.py", "PRICE = 500\n")
        run(["git", "add", "-A"], cwd=code)
        run(["git", "commit", "-q", "-m", "初版"], cwd=code)
        write(code / "shop.py", "PRICE = 450\n")
        run(["git", "commit", "-q", "-am", "改价"], cwd=code)
        assert cmd_access(ns(level="source", source=[str(code)])) == 0
        assert "source" in [s["id"] for s in onboarding(Ws(root))]
        return cmd_impact(ns(range=None)) == 0 and "完整代码权限" in read(root / "AGENTS.md")
    step("完整代码权限：登记代码位置 → 按 git 改动出回归范围任务", source_flow)
    shutil.rmtree(tmp, ignore_errors=True)
    out("")
    out("框架自检：" + ("全部通过。" if not fails else f"没通过：{'；'.join(fails)}"))
    return 0 if not fails else 1
