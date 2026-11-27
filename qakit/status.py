"""上手进度、每日待办、status / next 命令。"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from .common import KIT, out, qa_cmd, read, render, safe_name
from .workspace import Ws, find_ws
from .ingest import raw_changes
from .corpus import corpus_changes, index_pending
from .wiki import CATALOG, coupling_gaps, module_catalog, normalize_modules, unmerged_reqs, wiki_check
from .reqs import cfg_reminders, change_reminders, defect_info, parse_points, req_rows
from .prompts import feedback_items, triage_note, untriaged_runs
from .access import exec_status
from .regress import regress_file, regress_status


ONBOARD = [
    ("env", "检查环境", "01_环境.md"),
    ("init", "建工作区", "02_建工作区.md"),
    ("source", "登记代码位置", "08_环境与权限.md"),
    ("raw", "放进原始资料", "03_原始资料.md"),
    ("wiki", "生成知识库并人工审核", "04_知识库.md"),
    ("req", "跑通第一个需求（生成 → 审核 → 定稿）", "05_需求与测试点.md"),
    ("exec", "按最终测试点执行一轮并记录结果", "09_手工执行.md"),
    ("target", "接上被测服务和接口文档", "06_被测服务.md"),
    ("tests", "AI 写接口用例并跑通一次", "07_接口用例.md"),
]


# 不同环境走的上手步骤：无代码权限靠人工执行；开放接口走接口自动化；完整代码权限再多登记代码位置
ONBOARD_BY_ACCESS = {
    "blackbox": ("env", "init", "raw", "wiki", "req", "exec"),
    "interface": ("env", "init", "raw", "wiki", "req", "target", "tests"),
    "source": ("env", "init", "source", "raw", "wiki", "req", "target", "tests"),
}


def onboarding(ws: Ws | None) -> list[dict]:
    from .start import doctor_report  # noqa: E402  上层模块，在函数里导入免得循环
    env_ok, _ = doctor_report()
    steps = [s for s in ONBOARD if s[0] in ONBOARD_BY_ACCESS[ws.access if ws else "interface"]]
    if ws is None:
        return [{"id": i, "title": t, "doc": d, "done": (i == "env" and env_ok), "detail": ""} for i, t, d in steps]
    _, t = ws.target()
    tests = list((ws.auto / t["tests"]).rglob("test_*.py"))
    execs = exec_status(ws)
    checks = {
        "env": (env_ok, "运行 python qa.py doctor"),
        "init": (True, ""),
        "source": (bool(ws.cfg.get("source_paths")), "用 qa.py access source --source <代码文件夹> 登记"),
        "raw": (bool(ws.raw_files()) or bool(ws.state.get("index", {}).get("files")), "raw/ 里还没有资料，也没有建资料索引"),
        "wiki": (bool(ws.state["wiki"]["commits"]) and (ws.wiki / "index.md").exists(), "还没提交过知识库"),
        "req": (any((d / "最终测试点.md").exists() for d in ws.req_dirs()), "还没有定稿的需求"),
        "exec": (any(not p for _, _, p, _ in execs), "还没有填完的执行记录（qa.py exec R001）"),
        "target": (bool(t["base_url"]) and bool(ws.api_files()), "被测服务地址没配" if not t["base_url"] else "还没有接口文档"),
        "tests": (bool(tests) and bool(ws.state["autotest"]["runs"]), "还没有用例" if not tests else "用例还没跑过"),
    }
    return [{"id": i, "title": tt, "doc": d, "done": checks[i][0], "detail": "" if checks[i][0] else checks[i][1]} for i, tt, d in steps]


def target_tests(ws: Ws, tname: str) -> list[Path]:
    """某个被测服务的用例文件：别的服务的用例目录如果放在它下面（tests/admin 在 tests 里），不算它的。"""
    _, t = ws.target(tname)
    base = (ws.auto / t["tests"]).resolve()
    others = [(ws.auto / ws.target(n)[1]["tests"]).resolve() for n in ws.cfg["targets"] if n != tname]
    others = [o for o in others if o != base and base in o.parents]
    return [p for p in base.rglob("test_*.py") if not any(o in p.resolve().parents for o in others)] if base.exists() else []


def daily(ws: Ws) -> list[str]:
    todo = []
    ch = raw_changes(ws)
    pend_idx = index_pending(ws)
    if pend_idx and (pend_idx["new"] or pend_idx["changed"] or pend_idx["deleted"]):
        todo.append("登记的资料目录还没建索引 → " + f"{qa_cmd()} index" if pend_idx.get("never") else
                    f"资料索引要更新：新增 {len(pend_idx['new'])}、改动 {len(pend_idx['changed'])}、删除 {len(pend_idx['deleted'])} 个文件 → {qa_cmd()} index")
    if ch["built"] and (ch["new"] or ch["changed"] or ch["deleted"]):
        todo.append(f"知识库要更新：资料新增 {len(ch['new'])}、改动 {len(ch['changed'])}、删除 {len(ch['deleted'])} 份 → {qa_cmd()} prompt wiki-update")
    cc = corpus_changes(ws)
    if cc and (cc["new"] or cc["changed"] or cc["deleted"]):
        todo.append(f"资料文件夹里的文档自上次提交知识库后有变化（新增 {len(cc['new'])}、改动 {len(cc['changed'])}、删除 {len(cc['deleted'])} 份），"
                    f"知识库可能要跟着更新 → {qa_cmd()} prompt wiki-update")
    if any(m["listed"] for m in module_catalog(ws)):
        for rid, m in ws.state["reqs"].items():
            bad = normalize_modules(ws, m.get("modules") or [])[2]
            if bad:
                todo.append(f"需求 {rid} 标的模块“{'、'.join(bad)}”不在模块清单里 → 补进 wiki/{CATALOG} 的别名，"
                            f"或改成清单里的名字：{qa_cmd()} req modules {rid} --set …")
    um = unmerged_reqs(ws)
    if um:
        todo.append(f"已定稿的需求 {', '.join(um)} 定下的新规则还没并进知识库 → {qa_cmd()} prompt wiki-merge {um[0]}"
                    f"（没有新规则就 {qa_cmd()} req merged {um[0]}）")
    todo += change_reminders(ws) + cfg_reminders(ws)
    for rnd in ws.state.get("regress", {}):
        if regress_file(ws, rnd).exists():
            _, cnt, probs = regress_status(ws, rnd)
            if probs:
                todo.append(f"版本回归 {rnd}：{probs[0]} → 填 regress/{safe_name(rnd)}/回归清单.md，再 {qa_cmd()} regress check {rnd}")
    for tname in ws.cfg["targets"]:  # 接口没出时先写的用例骨架：接口文档到了就提醒补全
        waiting = [p for p in target_tests(ws, tname) if "等接口" in read(p)]
        if waiting and ws.api_files(tname):
            todo.append(f"{tname} 的接口文档到了，{len(waiting)} 个用例文件里还有标“等接口”的用例 → {qa_cmd()} prompt tests-write" +
                        ("" if tname == "default" else f" --target {tname}"))
    probs, st = wiki_check(ws)
    if ch["built"] and probs:
        todo.append(f"知识库格式有 {len(probs)} 个问题 → {qa_cmd()} wiki check")
    if st["todo"]:
        todo.append(f"知识库里有 {st['todo']} 个待确认的问题，找人确认后改掉 → {qa_cmd()} wiki todo 看清单")
    for r in req_rows(ws):
        if not r["done"]:
            todo.append(f"需求 {r['name']}：{r['stage']}")
            tp = ws.req_dir(r["id"]) / "测试点.md"
            gaps = coupling_gaps(ws, r["id"], parse_points(tp)) if tp.exists() else []
            if gaps:
                todo.append(f"需求 {r['id']} 的耦合提醒：" + "；".join(gaps) + f" → {qa_cmd()} coupling {r['id']}")
    ds = [defect_info(p) for p in ws.defect_files()]
    bad = [d["id"] for d in ds if d["problems"]]
    if bad:
        todo.append(f"缺陷记录没填完：{', '.join(bad)} → {qa_cmd()} defect list")
    items, ids = feedback_items(ws)
    if items:
        todo.append(f"待回流：{', '.join(ids)} → {qa_cmd()} prompt feedback")
    for rid, p, probs, _ in exec_status(ws):
        if probs:
            todo.append(f"执行记录 {p.name}（{rid}）没填完：{probs[0]}")
    pend = untriaged_runs(ws)
    if pend:
        later_fixed = [r for r in pend if triage_note(ws, r)]
        todo.append(f"有 {len(pend)} 次运行的失败还没归因（最近一次 {pend[-1]['dir']}，{len(pend[-1]['failed'])} 个）"
                    + (f"，其中 {len(later_fixed)} 次是后来改了用例才通过的，要补记录为什么改" if later_fixed else "")
                    + f" → {qa_cmd()} prompt triage")
    for tname in ws.cfg["targets"]:
        _, t = ws.target(tname)
        lr = next((x for x in reversed(ws.state["autotest"]["runs"]) if x.get("target", "default") == tname), None)
        tests = list((ws.auto / t["tests"]).rglob("test_*.py"))
        if lr:
            if tests and max(p.stat().st_mtime for p in tests) > datetime.fromisoformat(lr["time"]).timestamp():
                todo.append(f"{tname} 的用例在上次运行后改过，重跑 → {qa_cmd()} autotest run" + ("" if tname == "default" else f" --target {tname}"))
    return todo


def cmd_status(args) -> int:
    root = find_ws(args)
    ws = Ws(root) if root else None
    ob = onboarding(ws)
    nxt = next((s for s in ob if not s["done"]), None)
    todo = daily(ws) if ws else []
    if args.json:
        out(json.dumps({"workspace": str(root) if root else None, "project": ws.cfg.get("project") if ws else None,
                        "onboarding": ob, "onboarding_next": nxt["id"] if nxt else None, "todo": todo}, ensure_ascii=False, indent=2))
        return 0
    out(f"工作区：{root}（项目：{ws.cfg.get('project')}）" if ws else "还没有工作区。")
    if ws:
        cat = module_catalog(ws)
        if any(m["listed"] for m in cat):
            built = sum(m["name"] in ws.modules() for m in cat)
            out(f"知识库：模块清单 {len(cat)} 个模块，已整理 {built} 个" +
                (f"（接着整理：{qa_cmd()} prompt wiki-build，或 --module 指定）" if built < len(cat) else ""))
    if nxt:
        out("")
        out("上手进度：")
        for i, s in enumerate(ob, 1):
            mark = "✅" if s["done"] else ("👉" if s is nxt else "⬜")
            out(f" {mark} {i}. {s['title']}" + (f"  —— {s['detail']}" if s is nxt and s["detail"] else ""))
    out("")
    if todo:
        out("待办：")
        for item in todo:
            out("  · " + item)
    elif ws and not nxt:
        out("没有待办。来新需求时：python qa.py req new \"标题\" --from 需求文件 --module 模块")
    out("")
    if nxt:
        out(f"下一步：「{nxt['title']}」，详细做法：python qa.py next")
    return 0


def cmd_next(args) -> int:
    root = find_ws(args)
    ws = Ws(root) if root else None
    ob = onboarding(ws)
    step = next((s for s in ob if s["id"] == args.step), None) if args.step else next((s for s in ob if not s["done"]), None)
    v = ws.values() if ws else {"qa": qa_cmd(), "kit": str(KIT), "ws": "<工作区>", "project": ""}
    if step:
        if step["detail"]:
            out(f"当前情况：{step['detail']}\n")
        out(render(read(KIT / "docs" / "上手" / step["doc"]), v))
    else:
        out(render(read(KIT / "docs" / "日常.md"), v))
        out("\n## 现在的待办\n")
        out("\n".join(f"- {item}" for item in daily(ws)) or "- 没有待办")
    return 0
