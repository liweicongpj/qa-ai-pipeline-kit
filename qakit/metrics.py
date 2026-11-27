"""指标：采纳率、编造、补充、漏测率、模型用量；模块清单一览。"""
from __future__ import annotations


from .common import now_iso, out, pct, qa_cmd, write
from .workspace import git_commit, ws_from_args
from .ingest import raw_changes
from .wiki import CATALOG, module_catalog, wiki_check
from .reqs import defect_info
from .prompts import last_run
from .gen import calls, usage_table
from .access import exec_status


def cmd_metrics(args) -> int:
    ws = ws_from_args(args)
    defects = [defect_info(p) for p in ws.defect_files()]
    rows, by_mod = [], {}
    tot = {"generated": 0, "kept": 0, "fabricated": 0, "added": 0, "minutes": 0.0}
    for d in ws.req_dirs():
        rid = d.name.split("-")[0]
        m = ws.req_meta(rid)
        ds = [x for x in defects if x["req"] == rid]
        esc = sum(x["escaped"] for x in ds)
        mods = m.get("modules") or ["（未标模块）"]
        for mod in mods:
            b = by_mod.setdefault(mod, {"reqs": 0, "generated": 0, "kept": 0, "defects": 0, "escaped": 0})
            b["reqs"] += 1
            b["defects"] += len(ds)
            b["escaped"] += esc
        if "generated" in m:
            kept = m["accepted"] + m["modified"]
            for k, val in (("generated", m["generated"]), ("kept", kept), ("fabricated", m["fabricated"]), ("added", m["added"]), ("minutes", m["minutes"])):
                tot[k] += val
            for mod in mods:
                by_mod[mod]["generated"] += m["generated"]
                by_mod[mod]["kept"] += kept
            rows.append(f"| {d.name} | {'、'.join(m.get('modules') or [])} | {m.get('model', '')} | {m['generated']} | {pct(kept, m['generated'])} | "
                        f"{m['fabricated']} | {m['added']} | {m['minutes']:g} | {len(ds)} | {esc} | {pct(esc, len(ds)) if ds else '—'} |")
        else:
            rows.append(f"| {d.name} | {'、'.join(m.get('modules') or [])} | | 未定稿 |  |  |  |  | {len(ds)} | {esc} |  |")
    mod_rows = [f"| {k} | {b['reqs']} | {b['generated']} | {pct(b['kept'], b['generated'])} | {b['defects']} | {b['escaped']} |" for k, b in sorted(by_mod.items())]
    lr = last_run(ws)
    _, wstats = wiki_check(ws)
    ch = raw_changes(ws)
    cs = calls(ws)
    text = f"""# 指标：{ws.cfg.get('project', '')}

生成时间：{now_iso()}

## 测试点（每个需求定稿时记录）

| 需求 | 模块 | 生成模型 | AI 生成 | 采纳率 | 编造 | 人工补充 | 审核分钟 | 缺陷 | 其中漏测 | 漏测率 |
|---|---|---|---|---|---|---|---|---|---|---|
{chr(10).join(rows) or '| （还没有需求） | | | | | | | | | | |'}

合计：AI 生成 {tot['generated']} 条，采纳率 {pct(tot['kept'], tot['generated'])}，编造 {tot['fabricated']} 条，人工补充 {tot['added']} 条，审核共 {tot['minutes']:g} 分钟。

- 采纳率 =（采纳 + 改后采纳）÷ AI 生成；编造 = 否决原因是“不符合规则”的条数（AI 编了资料里没有的规则）
- 漏测 = 缺陷记录里“被最终测试点覆盖：否”；漏测率 = 漏测 ÷ 该需求的缺陷数

## 按功能模块

| 模块 | 需求数 | AI 生成 | 采纳率 | 缺陷 | 漏测 |
|---|---|---|---|---|---|
{chr(10).join(mod_rows) or '| （还没有） | | | | | |'}

## 缺陷

共 {len(defects)} 条，漏测 {sum(x['escaped'] for x in defects)} 条，已回流 {sum(x['escaped'] and x['fed_back'] for x in defects)} 条，自动化发现 {sum('自动化' in x['how'] for x in defects)} 条。

## 手工执行

{chr(10).join(f"- {p.name}（{rid}）：" + "，".join(f"{k} {v}" for k, v in c.items()) + ("（没填完）" if pr else "") for rid, p, pr, c in exec_status(ws)) or '还没有执行记录。'}

## 接口自动化

运行 {len(ws.state['autotest']['runs'])} 次。{f"最近一次 {lr['time']}（{lr.get('target', 'default')}）：用例 {lr['total']} 个，失败 {len(lr['failed'])} 个。" if lr else ''}

## 模型用量（gen、bench 自动记录，usage add 手动补记）

{usage_table(cs) if cs else '还没有记录。'}

## 知识库

{wstats['pages']} 页，“待确认” {wstats['todo']} 处，提交 {len(ws.state['wiki']['commits'])} 次。资料自上次提交后：新增 {len(ch['new'])}、改动 {len(ch['changed'])}、删除 {len(ch['deleted'])}。
"""
    write(ws.root / "metrics" / "指标.md", text)
    out(text)
    out(f"已保存：{ws.root / 'metrics' / '指标.md'}")
    git_commit(ws, "指标：更新")
    return 0


def cmd_modules(args) -> int:
    ws = ws_from_args(args)
    cat = module_catalog(ws)
    if not cat:
        out(f"还没有模块清单，也没有模块页。建知识库时第一步会建模块清单：{qa_cmd()} prompt wiki-build")
        return 0
    defects = [defect_info(p) for p in ws.defect_files()]
    built = set(ws.modules())
    out(f"模块清单（wiki/{CATALOG}）：{len(cat)} 个模块，已整理 {len(built & {m['name'] for m in cat})} 个")
    for m in cat:
        reqs = [rid for rid, x in ws.state["reqs"].items() if m["name"] in (x.get("modules") or [])]
        ds = [d for d in defects if m["name"] in d["module"]]
        out(f"  {'✅' if m['name'] in built else '⬜'} {m['name']}" + (f"（上级：{m['parent']}）" if m["parent"] else "") +
            (f"　别名：{'、'.join(m['aliases'])}" if m["aliases"] else "") +
            f"　需求 {len(reqs)} 个，缺陷 {len(ds)} 条（漏测 {sum(d['escaped'] for d in ds)}）")
    return 0
