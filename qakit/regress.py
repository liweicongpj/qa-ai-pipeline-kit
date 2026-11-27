"""版本回归：拼回归清单（含旧用例）、自动化部分填结果、回归报告。"""
from __future__ import annotations

import argparse
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import qa_index

from .common import EXEC_RESULTS, now_iso, out, pct, qa_cmd, read, read_table, safe_name, split_names, write
from .workspace import Ws, git_commit, ws_from_args
from .wiki import normalize_modules
from .reqs import parse_points
from .prompts import last_run
from .autotest import cmd_autotest


REG_HEAD = ["编号", "来源", "模块", "测试点", "风险等级", "自动化用例", "结果", "缺陷编号", "备注"]


CASE_COLUMNS = {  # 以前写的用例（Excel / CSV）常见的列名
    "id": ["用例编号", "用例id", "编号", "id", "caseid"],
    "title": ["用例标题", "用例名称", "测试点", "标题", "用例", "名称", "title", "summary"],
    "module": ["所属模块", "功能模块", "模块", "module"],
    "level": ["优先级", "用例等级", "用例级别", "重要程度", "风险等级", "等级", "级别", "priority"],
    "expect": ["预期结果", "预期", "expected"],
}


def read_cases(p: Path) -> list[dict]:
    """读以前写的用例表：自动认出用例标题、模块、优先级、预期结果这几列。"""
    res = []
    for _, rows in qa_index.read_sheets(p):
        keys = {f: [qa_index.norm_header(n) for n in names] for f, names in CASE_COLUMNS.items()}
        hi = next((i for i, r in enumerate(rows[:10]) if any(qa_index.norm_header(c) in keys["title"] for c in r)), None)
        if hi is None:
            continue
        head = [qa_index.norm_header(c) for c in rows[hi]]
        mp = {f: next(head.index(k) for k in ks if k in head) for f, ks in keys.items() if any(k in head for k in ks)}
        for r in rows[hi + 1:]:
            get = lambda f: (r[mp[f]] if f in mp and mp[f] < len(r) else "").strip()
            if get("title"):
                res.append({f: get(f) for f in CASE_COLUMNS})
    return res


def test_tags(ws: Ws) -> dict[str, list[str]]:
    """用例文档字符串里写的最终测试点编号（R002-T03）→ 用例（文件名::函数名）。"""
    import ast
    tags = {}
    for p in sorted(ws.auto.rglob("test_*.py")):
        if "runs" in p.relative_to(ws.auto).parts:
            continue
        try:
            tree = ast.parse(read(p))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
                for tag in sorted(set(re.findall(r"R\d+-T\d+", ast.get_docstring(node) or ""))):
                    tags.setdefault(tag, []).append(f"{p.stem}::{node.name}")
    return tags


def level_hit(s: str, levels: list[str]) -> bool:
    """风险等级筛选：高 / 中 / 低，旧用例常写 P0–P3，也认。"""
    u = s.upper()
    lv = "高" if any(x in u for x in ("高", "P0", "P1", "紧急")) else "中" if any(x in u for x in ("中", "P2")) else "低" if any(x in u for x in ("低", "P3", "P4")) else s
    return lv in levels


def regress_file(ws: Ws, rnd: str) -> Path:
    return ws.root / "regress" / safe_name(rnd) / "回归清单.md"


def write_regress(f: Path, rnd: str, rows: list[dict], note: str) -> None:
    lines = [f"# 版本回归清单：{rnd}", "", note, "", "结果填 通过 / 失败 / 阻塞 / 未测；失败的先 qa.py defect new 登记缺陷，把编号填进来。"
             "有“自动化用例”的条目，qa.py regress run 会自动填结果。", "",
             "| " + " | ".join(REG_HEAD) + " |", "|" + "---|" * len(REG_HEAD)]
    lines += ["| " + " | ".join(str(r.get(h, "")).replace("|", "/") for h in REG_HEAD) + " |" for r in rows]
    write(f, "\n".join(lines) + "\n")


def regress_status(ws: Ws, rnd: str) -> tuple[list[dict], dict, list[str]]:
    _, rows = read_table(regress_file(ws, rnd))
    cnt = {k: sum(r.get("结果", "") == k for r in rows) for k in EXEC_RESULTS}
    cnt["没填"] = sum(not r.get("结果", "").strip() for r in rows)
    probs = []
    if cnt["没填"]:
        probs.append(f"{cnt['没填']} 条还没填结果")
    bad = [r["编号"] for r in rows if r.get("结果") == "失败" and not r.get("缺陷编号", "").strip()]
    if bad:
        probs.append(f"失败的要填缺陷编号：{', '.join(bad[:10])}")
    odd = [r["编号"] for r in rows if r.get("结果", "").strip() and r["结果"] not in EXEC_RESULTS]
    if odd:
        probs.append(f"结果只能填 {' / '.join(EXEC_RESULTS)}：{', '.join(odd[:10])}")
    return rows, cnt, probs


def cmd_regress(args) -> int:
    ws = ws_from_args(args)
    rounds = ws.state.setdefault("regress", {})
    if args.action == "list" or not args.round:
        if not rounds:
            out(f"还没有版本回归。拼一份：{qa_cmd()} regress new v1.3 [--modules 模块] [--level 高,中] [--cases 旧用例.xlsx]")
        for rnd in rounds:
            if regress_file(ws, rnd).exists():
                _, cnt, probs = regress_status(ws, rnd)
                out(f"  {rnd}：" + "，".join(f"{k} {v}" for k, v in cnt.items() if v) + ("" if not probs else f"（{probs[0]}）"))
        return 0
    rnd, f = args.round, regress_file(ws, args.round)
    if args.action == "new":
        if f.exists() and not args.force:
            out(f"{f} 已经有了：直接填；要重新拼加 --force（会清掉已填的结果）。")
            return 1
        mods = []
        if args.modules:
            norm, hints, unknown = normalize_modules(ws, [m.strip() for m in args.modules.replace("，", ",").split(",") if m.strip()])
            mods = norm + unknown + [k for _, ks in hints for k in ks]
        levels = [x.strip() for x in (args.level or "").replace("，", ",").split(",") if x.strip()]
        reqs = [x.strip().upper() for x in (args.reqs or "").split(",") if x.strip()]
        tags, rows = test_tags(ws), []
        for d in ws.req_dirs():
            rid = d.name.split("-")[0]
            rmods = ws.req_meta(rid).get("modules") or []
            if not (d / "最终测试点.md").exists() or (reqs and rid not in reqs) or (mods and not set(rmods) & set(mods)):
                continue
            for p in parse_points(d / "最终测试点.md"):
                if levels and not level_hit(p["risk"], levels):
                    continue
                tag = f"{rid}-{p['no']}"
                rows.append({"来源": tag, "模块": "、".join(rmods), "测试点": p["point"], "风险等级": p["risk"], "自动化用例": "、".join(tags.get(tag, []))})
        for cf in args.cases or []:
            for i, c in enumerate(read_cases(Path(cf)), 1):
                norm = normalize_modules(ws, [c["module"]])[0] if c["module"] else []
                mod = norm[0] if norm else c["module"]
                if (mods and mod not in mods) or (levels and c["level"] and not level_hit(c["level"], levels)):
                    continue
                rows.append({"来源": f"旧用例 {Path(cf).name}#{c['id'] or i}", "模块": mod, "风险等级": c["level"], "自动化用例": "",
                             "测试点": c["title"] + (f"（预期：{c['expect']}）" if c["expect"] else "")})
        if not rows:
            out("按这些条件一条也没选到：检查 --modules / --level / --reqs，或需求还没定稿。")
            return 1
        for i, r in enumerate(rows, 1):
            r["编号"] = f"G{i:03d}"
        filt = "；".join(x for x in (f"模块 {'、'.join(mods)}" if mods else "", f"风险等级 {'、'.join(levels)}" if levels else "",
                                     f"需求 {'、'.join(reqs)}" if reqs else "", f"旧用例 {len(args.cases or [])} 个文件" if args.cases else "") if x)
        write_regress(f, rnd, rows, f"拼出时间：{now_iso()}　范围：{filt or '全部定稿需求'}")
        auto = sum(bool(r["自动化用例"]) for r in rows)
        rounds[rnd] = {"created": now_iso(), "rows": len(rows), "auto": auto, "filters": filt}
        ws.save()
        git_commit(ws, f"版本回归 {rnd}：回归清单 {len(rows)} 条")
        out(f"已拼出 {f}：{len(rows)} 条，其中 {auto} 条有对应的自动化用例。")
        out(f"下一步：{qa_cmd()} regress run {rnd} --start 跑自动化的那部分并自动填结果；其余照清单手工执行、填结果；"
            f"全部填完运行 {qa_cmd()} regress check {rnd} 出回归报告。")
        return 0
    if not f.exists():
        out(f"还没有这一轮的回归清单：{qa_cmd()} regress new {rnd}")
        return 1
    if args.action == "run":
        _, rows = read_table(f)
        names = sorted({n for r in rows for n in split_names(r.get("自动化用例", ""))})
        if not names:
            out("清单里没有对应自动化用例的条目。用例的文档字符串里写上最终测试点编号（如 R002-T03），重新 regress new --force 才对得上。")
            return 1
        kexpr = " or ".join(sorted({n.split("::")[-1] for n in names}))
        cmd_autotest(argparse.Namespace(ws=args.ws, action="run", target=args.target, start=args.start, k=kexpr))
        ws = Ws(ws.root)
        lr = last_run(ws)
        junit = ws.root / lr["dir"] / "junit.xml" if lr else None
        if not junit or not junit.exists():
            out("这次没跑出结果（看上面的输出），清单没改。")
            return 1
        res = {}
        for tc in ET.parse(junit).getroot().iter("testcase"):
            key = f"{(tc.get('classname') or '').split('.')[-1]}::{tc.get('name')}"
            res[key] = ("失败" if tc.find("failure") is not None or tc.find("error") is not None else
                        "跳过" if tc.find("skipped") is not None else "通过")
        filled = 0
        for r in rows:
            got = [res.get(n) for n in split_names(r.get("自动化用例", ""))]
            if not got or None in got:
                continue
            r["结果"] = "失败" if "失败" in got else "未测" if all(g == "跳过" for g in got) else "通过"
            r["备注"] = f"自动化 {lr['dir']}" + ("（有失败，先归因再登记缺陷）" if r["结果"] == "失败" else "（用例被跳过，可能在等接口）" if r["结果"] == "未测" else "")
            filled += 1
        head_note = next((l for l in read(f).splitlines() if l.startswith("拼出时间")), "")
        write_regress(f, rnd, rows, head_note)
        out(f"已按自动化结果填了 {filled} 条（{lr['dir']}）。失败的先归因（{qa_cmd()} prompt triage），确认是缺陷再登记、把编号填进清单。")
        return 0
    # check：检查填写情况，出回归报告
    rows, cnt, probs = regress_status(ws, rnd)
    total = len(rows)
    by_mod = {}
    for r in rows:
        b = by_mod.setdefault(r.get("模块") or "（没标）", {k: 0 for k in (*EXEC_RESULTS, "没填")})
        b[r.get("结果") if r.get("结果") in EXEC_RESULTS else "没填"] += 1
    prev = [x for x in rounds if x != rnd and regress_file(ws, x).exists() and rounds[x].get("created", "") < rounds.get(rnd, {}).get("created", "")]
    lines = [f"# 版本回归报告：{rnd}", "", f"生成时间：{now_iso()}", "",
             f"共 {total} 条：通过 {cnt['通过']}，失败 {cnt['失败']}，阻塞 {cnt['阻塞']}，未测 {cnt['未测']}，没填 {cnt['没填']}；"
             f"通过率 {pct(cnt['通过'], total)}；自动化覆盖 {sum(bool(r.get('自动化用例')) for r in rows)} 条。", ""]
    if prev:
        _, pc, _ = regress_status(ws, prev[-1])
        ptotal = sum(pc.values())
        lines += [f"上一轮 {prev[-1]}：通过率 {pct(pc['通过'], ptotal)}（{pc['通过']}/{ptotal}）。", ""]
    lines += ["## 按模块", "", "| 模块 | 通过 | 失败 | 阻塞 | 未测 | 没填 |", "|---|---|---|---|---|---|"]
    lines += [f"| {m} | {b['通过']} | {b['失败']} | {b['阻塞']} | {b['未测']} | {b['没填']} |" for m, b in sorted(by_mod.items())]
    fails = [r for r in rows if r.get("结果") in ("失败", "阻塞")]
    if fails:
        lines += ["", "## 失败和阻塞", "", "| 编号 | 来源 | 测试点 | 结果 | 缺陷编号 |", "|---|---|---|---|---|"]
        lines += [f"| {r['编号']} | {r.get('来源', '')} | {r.get('测试点', '')} | {r.get('结果', '')} | {r.get('缺陷编号', '')} |" for r in fails]
    write(f.parent / "回归报告.md", "\n".join(lines) + "\n")
    rounds.setdefault(rnd, {}).update({"checked": now_iso(), **cnt})
    ws.save()
    out(f"{rnd}：" + "，".join(f"{k} {v}" for k, v in cnt.items()) + f"。回归报告：{f.parent / '回归报告.md'}")
    if probs:
        out("还没填完：" + "；".join(probs))
        return 1
    git_commit(ws, f"版本回归 {rnd}：通过 {cnt['通过']}/{total}，失败 {cnt['失败']}")
    return 0
