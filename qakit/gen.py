"""直接调模型的公共部分：拼测试点和归因的提示词、记调用、多模型对比、用量统计、模型清单（每一步怎么调见 steps.py）。"""
from __future__ import annotations

import json
import os
import random
import shutil
from pathlib import Path

import qa_models

from .common import QaError, bigrams, money, now_iso, out, pct, qa_cmd, read, render, stamp, strip_fence, write
from .workspace import Ws, find_ws, git_commit, ws_from_args
from .corpus import retrieved_context
from .wiki import COUPLING, coupled_for, suggest_modules
from .reqs import make_review, parse_review, review_problems, review_stats
from .prompts import req_values, triage_note


def wiki_context(ws: Ws, query: str, modules: list[str], budget: int | None = None) -> tuple[str, list[str]]:
    """按预算挑知识页：目录、风险清单、标注模块的页优先，其余按和需求的相关度排。"""
    budget = budget or int(ws.cfg.get("context_chars") or 150000)
    pages = ws.wiki_pages()
    if not pages:  # 新项目第一个需求就来了：先靠检索出测试点，提示缺知识库
        return ("（知识库还是空的：这次只能靠下面检索到的原始资料出测试点。原始资料可能过时、互相矛盾，拿不准的都列进“需要和策划确认的问题”。"
                "之后要把涉及的模块整理进知识库：prompt wiki-build --module 模块名）"), []
    q = bigrams(query)

    # 和需求模块共用数值的其他模块（耦合关系表里查到的）也优先放进来
    modules = list(modules) + [o for r in coupled_for(ws, modules) for o in r["others"]]

    def rank(p: Path):
        rel = p.relative_to(ws.wiki).as_posix()
        if rel == "index.md":
            return (0, 0)
        if rel.startswith("risks/") or rel == COUPLING:
            return (1, 0)
        if any(m and m in p.stem for m in modules):
            return (2, 0)
        if rel == "log.md":
            return (9, 0)
        return (3, -len(q & bigrams(read(p))))

    parts, used, skipped = [], 0, []
    for p in sorted(pages, key=rank):
        rel = p.relative_to(ws.wiki).as_posix()
        text = read(p)
        if rel == "log.md":
            continue
        if used + len(text) > budget:
            skipped.append(rel)
            continue
        parts.append(f"===== wiki/{rel} =====\n{text.strip()}\n")
        used += len(text)
    return "\n".join(parts), skipped


def build_testpoints_prompt(ws: Ws, rid: str) -> tuple[str, str, list[str]]:
    meta = ws.req_meta(ws.req_dir(rid).name.split("-")[0])
    req_text = read(ws.req_dir(rid) / "需求.md")
    if not meta.get("modules"):  # 直接调模型时没人回答“涉及哪些模块”：用框架推荐的，并告诉用户
        sug = [s[0] for s in suggest_modules(ws, req_text)[:3]]
        if sug:
            meta["modules"] = sug
            out(f"需求没标模块，按框架推荐用：{'、'.join(sug)}（不对就 {qa_cmd()} req modules {rid} --set … 再重新生成）")
    v = {**ws.values(), **req_values(ws, rid)}
    d = ws.root / v["req_dir"]
    missing = [m for m in meta.get("modules") or [] if m not in ws.modules()]
    if missing:
        out(f"⚠️ 知识库里还没有这些模块的页：{'、'.join(missing)}，这部分只能靠检索，测试点会差一些。整理：{qa_cmd()} prompt wiki-build --module {','.join(missing)}")
    wiki, skipped = wiki_context(ws, req_text, meta.get("modules") or [])
    v.update({"requirement": req_text.strip(), "wiki": wiki, "template": ws.template("测试点模板.md").strip(),
              "retrieved": retrieved_context(ws, req_text[:3000] + " " + v["modules"], exclude=d)})
    return render(ws.prompt_text("api/system.md"), v).strip(), render(ws.prompt_text("api/testpoints.md"), v).strip(), skipped


def log_call(ws: Ws, stage: str, label: str, r: qa_models.Result, **extra) -> None:
    p = ws.root / ".qa" / "calls.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    rec = {"time": now_iso(), "stage": stage, "model": label, **r.to_dict(), **extra}
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def calls(ws: Ws) -> list[dict]:
    p = ws.root / ".qa" / "calls.jsonl"
    return [json.loads(x) for x in read(p).splitlines() if x.strip()] if p.exists() else []


def usage_line(r: qa_models.Result) -> str:
    cache = f"，缓存读 {r.cache_read_tokens}" if r.cache_read_tokens else ""
    return f"输入 {r.input_tokens} token，输出 {r.output_tokens} token{cache}，{r.seconds:g} 秒，费用 {money(r.cost_usd)}"


def build_triage_prompt(ws: Ws, lr: dict) -> tuple[str, str]:
    """失败归因的提示词：失败的用例、pytest 输出末尾、用例代码、接口文档、风险清单。"""
    run_dir = ws.root / lr["dir"]
    tests_src = []
    for f in sorted({x.split("::")[0] for x in lr["failed"]}):
        cand = [p for p in ws.auto.rglob("*.py") if p.stem == f.split(".")[-1]]
        tests_src += [f"===== {ws.rel(p)} =====\n{read(p)[:20000]}" for p in cand[:1]]
    risks = ws.wiki / "risks" / "风险清单.md"
    v = {**ws.values(), "failed": "\n".join(f"- {x}" for x in lr["failed"]),
         "pytest_output": read(run_dir / "pytest输出.txt")[-30000:] if (run_dir / "pytest输出.txt").exists() else "",
         "tests_src": "\n".join(tests_src), "risks": read(risks)[:20000] if risks.exists() else "（没有风险清单）", "note": triage_note(ws, lr),
         "api_doc": "\n".join(read(p)[:30000] for p in ws.api_files(lr.get("target")))}
    return render(ws.prompt_text("api/system.md"), v).strip(), render(ws.prompt_text("api/triage.md"), v).strip()


def bench_dir_meta(ws: Ws) -> Path:
    return ws.root / ".qa" / "bench"


def bench_runs(ws: Ws, rid: str) -> list[dict]:
    d = bench_dir_meta(ws)
    metas = [json.loads(read(p)) for p in sorted(d.glob("*.json"))] if d.exists() else []
    return [x for x in metas if x["req"] == rid]


def cmd_bench(args) -> int:
    ws = ws_from_args(args)
    d = ws.req_dir(args.req)
    rid = d.name.split("-")[0]
    if args.action == "testpoints":
        if not args.models:
            out("要指定参加对比的模型：python qa.py bench testpoints R001 --models 模型1,模型2[,模型3] [--repeat 2]")
            return 1
        reg = qa_models.load_registry(ws.cfg)
        names = [x.strip() for x in args.models.replace("，", ",").split(",") if x.strip()]
        missing = [x for x in names if x not in reg]
        if missing:
            raise QaError(f"模型清单里没有：{', '.join(missing)}。可选：{', '.join(reg)}")
        system, user, skipped = build_testpoints_prompt(ws, rid)
        jobs = [(n, i + 1) for n in names for i in range(max(1, args.repeat))]
        labels = [f"方案{chr(65 + i)}" if i < 26 else f"方案{i + 1}" for i in range(len(jobs))]
        random.shuffle(labels)  # 打乱顺序，审核的人不知道哪份是哪个模型
        st = stamp()
        bdir = d / "bench" / st
        entries = []
        out(f"同一份提示词（需求 + 知识库 {len(user)} 字）交给 {len(names)} 个模型" + (f"，每个跑 {args.repeat} 次" if args.repeat > 1 else "") +
            (f"；知识库没放进去的页：{'、'.join(skipped[:5])}" if skipped else "") + "……")
        for (name, rep), label in zip(jobs, labels):
            r = qa_models.call(reg[name], system, user, cwd=ws.root)
            log_call(ws, "testpoints", name, r, req=rid, bench=st, plan=label)
            e = {"label": label, "model": name, "rep": rep, "result": r.to_dict(), "file": None, "points": 0}
            if r.ok:
                f = bdir / f"{label}.md"
                write(f, strip_fence(r.text).strip() + "\n")
                e["points"] = make_review(ws, f, bdir / f"审核_{label}.md", f"{d.name} · {label}", blind=True)
                e["file"] = ws.rel(f)
            entries.append(e)
            out(f"  {label}：{'完成，' + str(e['points']) + ' 条' if r.ok else '失败：' + r.error}")
        meta = {"req": rid, "stamp": st, "dir": ws.rel(bdir), "entries": entries, "prompt_chars": len(system) + len(user)}
        write(bench_dir_meta(ws) / f"{st}.json", json.dumps(meta, ensure_ascii=False, indent=2))
        write(bdir / "说明.md", f"# 模型对比 {st}：{d.name}\n\n每个“方案”是一个模型的输出，顺序已打乱。请先逐份填 审核_方案X.md（和平时审核一样），"
              f"全部填完再运行 {qa_cmd()} bench report {rid}，才会揭晓每个方案是哪个模型。\n")
        ws.save()
        git_commit(ws, f"需求 {d.name}：模型对比 {st}")
        out("")
        out(f"输出和审核表在 {bdir}。请人逐份审核（不要先看是哪个模型），填完运行 {qa_cmd()} bench report {rid}")
        return 0
    runs = bench_runs(ws, rid)
    if not runs:
        out(f"需求 {rid} 还没做过模型对比。")
        return 1
    meta = next((x for x in runs if x["stamp"] == args.stamp), None) if args.stamp else runs[-1]
    if meta is None:
        out(f"找不到这次对比：{args.stamp}。有：{', '.join(x['stamp'] for x in runs)}")
        return 1
    bdir = ws.root / meta["dir"]
    if args.action == "adopt":
        if not args.label:
            out("要指定采用哪个方案：python qa.py bench adopt R001 方案B")
            return 1
        e = next((x for x in meta["entries"] if x["label"] == args.label), None)
        if not e or not e["file"]:
            out(f"没有 {args.label} 或它生成失败了。")
            return 1
        if (d / "测试点.md").exists() and not args.force:
            out(f"{d / '测试点.md'} 已经有了，要用 {args.label} 覆盖就加 --force。")
            return 1
        shutil.copyfile(ws.root / e["file"], d / "测试点.md")
        rv = bdir / f"审核_{args.label}.md"
        if rv.exists():
            write(d / "审核.md", read(rv).replace(f"{d.name} · {args.label}", d.name))
        ws.req_meta(rid)["model"] = e["model"]
        ws.save()
        out(f"已采用 {args.label}（{e['model']}）作为 {rid} 的测试点" + ("，审核表一起带过来了" if rv.exists() else "") +
            f"。审核填完后运行 {qa_cmd()} accept {rid}")
        return 0
    # report
    probs, rows = [], []
    for e in meta["entries"]:
        res = e["result"]
        if not e["file"]:
            rows.append({**e, "stats": None})
            continue
        rv = bdir / f"审核_{e['label']}.md"
        rr, mm = parse_review(rv)
        p = review_problems(rr, mm)
        if p:
            probs.append(f"{e['label']}：{p[0]}")
        rows.append({**e, "stats": None if p else review_stats(rr, mm)})
    if probs and not args.force:
        out("还有审核表没填完（填完才揭晓模型；确实只想先看用量就加 --force）：")
        for p in probs:
            out("  · " + p)
        return 1
    lines = [f"# 模型对比报告：{d.name}（{meta['stamp']}）", "", f"同一份提示词约 {meta['prompt_chars']} 字。质量来自人工审核，用量来自接口返回。", "",
             "| 方案 | 模型 | 生成 | 采纳率 | 编造 | 人工补充 | 审核分钟 | 输入 token | 输出 token | 耗时（秒） | 费用 | 每条采纳的费用 |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in sorted(rows, key=lambda x: x["label"]):
        res, s = r["result"], r["stats"]
        if not r["file"]:
            lines.append(f"| {r['label']} | {r['model']} | 失败：{res['error'][:60]} | | | | | | | {res['seconds']} | | |")
            continue
        kept = (s["accepted"] + s["modified"]) if s else 0
        per = (res["cost_usd"] / kept) if s and kept and res["cost_usd"] is not None else None
        lines.append(f"| {r['label']} | {r['model']} | {r['points']} | {pct(kept, s['generated']) if s else '未审'} | "
                     f"{s['fabricated'] if s else ''} | {s['added'] if s else ''} | {s['minutes'] if s else ''} | {res['input_tokens']} | "
                     f"{res['output_tokens']} | {res['seconds']} | {money(res['cost_usd'])} | {money(per)} |")
    lines += ["", "- 采纳率 =（采纳 + 改后采纳）÷ 生成；编造 = 否决原因为“不符合规则”的条数",
              "- 人工补充多，说明这个模型漏得多；费用没配单价的显示 —（在模型清单里填 price_in / price_out，单位：美元 / 百万 token）",
              "- 一次对比样本很小，选型建议在 3 个以上需求上重复对比再下结论", "",
              f"选定后可以运行 {qa_cmd()} bench adopt {rid} <方案> 把它作为正式测试点（审核表一起带过去）。"]
    write(bdir / "对比报告.md", "\n".join(lines) + "\n")
    out("\n".join(lines))
    return 0


def cmd_usage(args) -> int:
    ws = ws_from_args(args)
    if args.action == "add":
        r = qa_models.Result(ok=True, input_tokens=args.input or 0, output_tokens=args.output or 0, seconds=args.seconds or 0.0,
                             cost_usd=args.cost, provider="手动记录", model_id=args.model or "")
        log_call(ws, args.stage or "其他", args.model or "未知", r, note=args.note or "", req=(args.req or "").upper())
        out("已记一条用量。")
        return 0
    rows = calls(ws)
    if not rows:
        out("还没有模型调用记录（gen、bench 会自动记；在 AI 工具里手动完成的步骤可以用 usage add 补记）。")
        return 0
    out(usage_table(rows))
    return 0


def usage_table(rows: list[dict]) -> str:
    def agg(key):
        g = {}
        for r in rows:
            a = g.setdefault(r.get(key) or "?", {"n": 0, "ok": 0, "in": 0, "out": 0, "cache": 0, "sec": 0.0, "cost": 0.0, "priced": 0})
            a["n"] += 1
            a["ok"] += bool(r.get("ok"))
            a["in"] += r.get("input_tokens", 0)
            a["out"] += r.get("output_tokens", 0)
            a["cache"] += r.get("cache_read_tokens", 0)
            a["sec"] += r.get("seconds", 0) or 0
            if r.get("cost_usd") is not None:
                a["cost"] += r["cost_usd"]
                a["priced"] += 1
        return g
    lines = []
    for key, title in (("model", "按模型"), ("stage", "按步骤")):
        lines += [f"### {title}", "", "| 名称 | 调用 | 成功 | 输入 token | 输出 token | 缓存读 | 总耗时（秒） | 平均耗时 | 费用 |", "|---|---|---|---|---|---|---|---|---|"]
        for name, a in sorted(agg(key).items()):
            lines.append(f"| {name} | {a['n']} | {a['ok']} | {a['in']} | {a['out']} | {a['cache']} | {a['sec']:.1f} | {a['sec'] / a['n']:.1f} | "
                         f"{money(a['cost']) if a['priced'] else '—'} |")
        lines.append("")
    return "\n".join(lines)


def cmd_models(args) -> int:
    ws_root = find_ws(args)
    cfg = Ws(ws_root).cfg if ws_root else None
    reg = qa_models.load_registry(cfg)
    out("模型清单（框架 models.local.json + 工作区 qa.config.json 的 models）：")
    for name, m in reg.items():
        key = m.get("api_key_env")
        key_state = "" if not key else ("，密钥已设" if os.environ.get(key) else f"，⚠️ 环境变量 {key} 没设")
        out(f"  {name}：{m.get('provider')} / {m.get('model') or m.get('command') or ''}{key_state}" + (f"　{m['note']}" if m.get("note") else ""))
    if cfg and cfg.get("stage_models"):
        out("各步骤默认模型：" + "，".join(f"{k} → {v}" for k, v in cfg["stage_models"].items()))
    if args.test:
        name = args.test
        if name not in reg:
            out(f"没有模型 {name}")
            return 1
        out(f"\n试调 {name}（只问一句，花费很少）……")
        r = qa_models.call(reg[name], "你是测试助手。", "只回复两个字：收到", cwd=ws_root)
        out(("成功：" + r.text.strip()[:50] + "。" + usage_line(r)) if r.ok else f"失败：{r.error}")
        if ws_root:
            log_call(Ws(ws_root), "试调", name, r)
        return 0 if r.ok else 1
    return 0
