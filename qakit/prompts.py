"""交给 AI 的任务说明：按步骤填好变量打印出来；回流、归因要用的清单。"""
from __future__ import annotations

from pathlib import Path

import qa_index

from .common import out, qa_cmd, render
from .workspace import ws_from_args
from .ingest import raw_changes
from .corpus import corpus_changes, index_pending
from .wiki import coupling_text, module_catalog, normalize_modules
from .reqs import defect_info, parse_review, pending_change

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .workspace import Ws


PROMPTS = ("wiki-build", "wiki-update", "wiki-merge", "wiki-lint", "testpoints", "req-change", "cfg-check", "tests-plan", "tests-write",
           "triage", "feedback")


def req_values(ws: Ws, rid: str) -> dict:
    d = ws.req_dir(rid)
    rid = d.name.split("-")[0]
    mods = ws.req_meta(rid).get("modules") or []
    return {"req": rid, "req_dir": ws.rel(d), "modules": "、".join(mods) if mods else "（没标模块，按需求内容找相关页）",
            "coupling": coupling_text(ws, mods)}


def feedback_items(ws: Ws) -> tuple[list[str], list[str]]:
    pend = [d for d in (defect_info(p) for p in ws.defect_files()) if d["escaped"] and not d["fed_back"] and not d["problems"]]
    items, ids = [], []
    for d in pend:
        items.append(f"- 漏测缺陷 {d['id']}（{d['kind']}{'，模块 ' + d['module'] if d['module'] else ''}）：{d['title']}。现象：{d['phenomenon'][:200]}" +
                     (f"。原因：{d['cause'][:200]}" if d["cause"] else "") + (f"。以后怎么测：{d['howto'][:200]}" if d["howto"] else ""))
        ids.append(d["id"])
    for rid, m in ws.state["reqs"].items():
        if m.get("added") and not m.get("fed_back"):
            rv = ws.req_dir(rid) / "审核.md"
            if rv.exists():
                items += [f"- 需求 {rid} 审核时人工补充的测试点：{a}" for a in parse_review(rv)[1]["added"]]
                ids.append(rid)
    return items, ids


def last_run(ws: Ws) -> dict | None:
    runs = ws.state["autotest"]["runs"]
    return runs[-1] if runs else None


def untriaged_runs(ws: Ws) -> list[dict]:
    """有失败、却没写归因（运行目录里没有 归因.md）的运行，旧的在前。"""
    return [r for r in ws.state["autotest"]["runs"] if r["failed"] and not (ws.root / r["dir"] / "归因.md").exists()]


def triage_target(ws: Ws, which: str | None) -> dict | None:
    """要归因的那次运行：指定的；否则最近一次有失败、还没归因的。"""
    runs = ws.state["autotest"]["runs"]
    if which:
        return next((r for r in runs if which in r["dir"]), None)
    pend = untriaged_runs(ws)
    return pend[-1] if pend else None


def triage_note(ws: Ws, run: dict) -> str:
    later = [r for r in ws.state["autotest"]["runs"] if r["time"] > run["time"] and r.get("target") == run.get("target")]
    passed = sorted({x for r in later for x in r.get("fixed", [])} & set(run["failed"]))
    if not passed:
        return ""
    return ("注意：这些失败之后又跑通了（" + "、".join(x.split("::")[-1] for x in passed) +
            "）。要说明中间改了什么：是用例写错改对了，还是服务修好了；如果是改了断言，写清为什么原来的预期不对、依据哪条规则。")


def cmd_prompt(args) -> int:
    ws = ws_from_args(args)
    name = args.name
    if name not in PROMPTS:
        out("没有这个提示词。可选：" + "、".join(PROMPTS))
        return 1
    v = ws.values()
    if name == "testpoints" or (name == "tests-plan" and args.req):
        if not args.req:
            out(f"要指定需求编号：python qa.py prompt {name} R001")
            return 1
        v.update(req_values(ws, args.req))
        if name == "testpoints" and (ws.root / v["req_dir"] / "测试点.md").exists() and not args.force:
            out(f"{v['req_dir']}/测试点.md 已经有了。要重新生成就加 --force（之后审核表也要 --force 重建）。")
            return 1
    if name in ("req-change", "cfg-check"):
        if not args.req:
            out(f"要指定需求编号：{qa_cmd()} prompt {name} R002")
            return 1
        v.update(req_values(ws, args.req))
        rid = v["req"]
        if name == "req-change":
            ch = pending_change(ws, rid)
            if not ch:
                out(f"需求 {rid} 没有待处理的变更。原文件改了先运行 {qa_cmd()} req refresh {rid}")
                return 1
            v["change_dir"] = ch["dir"]
        else:
            m = ws.req_meta(rid)
            changed = [n for n, (path, sig) in (m.get("cfg_refs") or {}).items() if Path(path).exists() and qa_index.file_sig(Path(path)) != sig]
            v["cfg_note"] = ((f"改过的配置表：{'、'.join(changed)}。" if changed else "") +
                             (f"最终测试点里还有 {m.get('cfg_pending')} 处“待配表”。" if m.get("cfg_pending") else "")) or "（框架没发现变化，按需要核对）"
    if name in ("tests-plan", "tests-write"):
        tname, t = ws.target(args.target)
        v.update({"target": tname, "api_dir": f"autotest/{t['api']}", "tests_dir": f"autotest/{t['tests']}",
                  "target_opt": "" if tname == "default" else f" --target {tname}",
                  "focus": (f"重点覆盖 {v['req_dir']}/最终测试点.md 里能用接口验证的测试点；" if args.req else ""),
                  "api_note": ""})
        if not ws.api_files(tname):  # 接口还没出：先出清单和用例骨架，不用等开发做完
            v["api_note"] = (f"注意：{v['api_dir']}/ 里还没有接口文档（开发还没做完）。先按需求和最终测试点设计：接口相关的步骤写“等接口”，"
                             "写代码时给这些用例加 @pytest.mark.skip(reason=\"等接口\")，只写好数据准备和断言思路；"
                             f"接口文档到了（{qa_cmd()} ingest <接口文档> --api{v['target_opt']}）status 会提醒补全。")
    if name == "wiki-update":
        ch = raw_changes(ws)
        if not ch["built"]:
            out("还没生成过知识库，用 wiki-build。")
            return 1
        lines = [f"- 新增：{x}" for x in ch["new"]] + [f"- 有改动：{x}" for x in ch["changed"]] + [f"- 已删除：{x}" for x in ch["deleted"]]
        cc = corpus_changes(ws) or {"new": [], "changed": [], "deleted": []}
        lines += ([f"- 资料文件夹新增：{x}" for x in cc["new"]] + [f"- 资料文件夹有改动：{x}" for x in cc["changed"]] +
                  [f"- 资料文件夹已删除：{x}" for x in cc["deleted"]])
        if not lines:
            out("资料自上次提交知识库以来没有变化，不用更新。")
            return 0
        if cc["new"] or cc["changed"]:
            pend = index_pending(ws)
            if pend and (pend["new"] or pend["changed"]):
                out(f"资料索引还没更新，先运行 {qa_cmd()} index（任务说明里让 AI 用 index show 读新文档，读的是索引里的内容）。")
                return 1
        v["changes"] = "\n".join(lines)
    if name == "wiki-build":
        asked = [m.strip() for m in (getattr(args, "module", None) or "").replace("，", ",").split(",") if m.strip()]
        cat, built = module_catalog(ws), set(ws.modules())
        if asked:
            norm, hints, unknown = normalize_modules(ws, asked)
            names = norm + unknown + [k for _, ks in hints for k in ks]
            v["scope"] = f"这次只整理这些模块：{'、'.join(names)}（已有页的就核对、补全）。其他模块不动。"
        elif not any(m["listed"] for m in cat):
            v["scope"] = "还没有模块清单：先做第 1 步，建好清单就停下来请用户审；用户确认后，再挑优先级最高的一批（不超过 5 个）整理"
        else:
            todo = [m["name"] for m in cat if m["name"] not in built]
            v["scope"] = (f"模块清单里还没整理的有 {len(todo)} 个：{'、'.join(todo[:40])}{'…' if len(todo) > 40 else ''}。"
                          "这次挑优先级最高的一批（不超过 5 个）整理，其余下次再做" if todo else
                          "模块清单里的模块都整理过了：检查有没有新出现的模块要加进清单，再核对已有页")
    if name == "wiki-merge":
        if not args.req:
            out(f"要指定需求编号：{qa_cmd()} prompt wiki-merge R002")
            return 1
        v.update(req_values(ws, args.req))
        d = ws.root / v["req_dir"]
        if not (d / "最终测试点.md").exists():
            out(f"需求 {v['req']} 还没定稿：先 review、accept，再把新规则并进知识库。")
            return 1
        rows, _ = parse_review(d / "审核.md") if (d / "审核.md").exists() else ([], {})
        wrong = [f"- {r['no']}：{r['point']}" for r in rows if r["verdict"] == "否决" and r["reason"] == "不符合规则"]
        v["wrong"] = "\n".join(wrong) or "（没有）"
    if name == "triage":
        lr = triage_target(ws, args.run)
        if not lr or not lr["failed"]:
            out("没有要归因的失败（每次有失败的运行都写过 归因.md 了）。" if not args.run else f"找不到有失败的运行：{args.run}")
            return 0
        v.update({"run_dir": lr["dir"], "failed": "\n".join(f"- {x}" for x in lr["failed"]), "target": lr.get("target", "default"),
                  "note": triage_note(ws, lr)})
    if name == "feedback":
        items, ids = feedback_items(ws)
        if not items:
            out("没有待回流的内容（漏测缺陷都回流过了，审核补充的点也回流过了）。")
            return 0
        v.update({"items": "\n".join(items), "ids": ",".join(ids)})
    out("【以下是这一步的任务说明：在工作区里工作的 AI 照着做；也可以整段复制给别的 AI 工具】")
    out("-" * 60)
    out(render(ws.prompt_text(f"{name}.md"), v).strip())
    out("-" * 60)
    return 0
