"""需求：新建、导入、测试点解析、审核、定稿、需求变更、配置晚到、缺陷记录。"""
from __future__ import annotations

import re
from pathlib import Path

import qa_index

from .common import QaError, REJECT_REASONS, SEVERITIES, VERDICTS, fields, md_tables, next_id, now_iso, out, pct, qa_cmd, read, read_table, render, safe_name, section, sha256, stamp, write
from .workspace import git_commit, run_hook, ws_from_args
from .corpus import open_index
from .wiki import CATALOG, coupling_gaps, mark_feedback, mark_merged, normalize_modules, print_module_suggestions

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .workspace import Ws


FINAL_HEAD = ["编号", "测试点", "风险等级", "类型", "依据", "怎么验证", "来源", "AI 原编号"]


def write_final(d: Path, rows: list[dict], note: str) -> None:
    lines = [f"# 最终测试点：{d.name}", "", note, "", "| " + " | ".join(FINAL_HEAD) + " |", "|" + "---|" * len(FINAL_HEAD)]
    lines += ["| " + " | ".join(str(r.get(h, "")).replace("|", "/") for h in FINAL_HEAD) + " |" for r in rows]
    write(d / "最终测试点.md", "\n".join(lines) + "\n")


def pending_change(ws: Ws, rid: str) -> dict | None:
    """这个需求还没处理完的变更（原文件改了、重新导入后，测试点变更还没定稿）。"""
    ch = (ws.req_meta(rid).get("changes") or [None])[-1]
    return ch if ch and ch.get("status") in ("待处理", "待审核") else None


def config_snapshot(ws: Ws) -> dict:
    return {str(p): qa_index.file_sig(p) for c in ws.cfg.get("corpora") or [] if c["kind"] == "config" for p in qa_index.corpus_files(c)}


def record_cfg(ws: Ws, rid: str) -> None:
    """定稿时记下测试点引用了哪些配置表、配置文件当时的样子、还有几条“待配表”：之后配置出来或改了就提醒校对。"""
    meta, final = ws.req_meta(rid), ws.req_dir(rid) / "最终测试点.md"
    text = read(final) if final.exists() else ""
    refs = {}
    idx = open_index(ws, must=False)
    if idx is not None:
        try:
            tables = idx.con.execute("SELECT name, path FROM tables").fetchall()
        finally:
            idx.close()
        for name, path in tables:  # 表名要整个出现（economy 不算 economy/gear.rod 里的那一截）
            if Path(path).exists() and re.search(rf"(?<![\w/.]){re.escape(name)}(?![\w/.])", text):
                refs[name] = [path, qa_index.file_sig(Path(path))]
    meta.update({"cfg_refs": refs, "cfg_snapshot": config_snapshot(ws), "cfg_pending": text.count("待配表")})


def cfg_reminders(ws: Ws) -> list[str]:
    todo, snap = [], None
    for d in ws.req_dirs():
        rid, m = d.name.split("-")[0], ws.req_meta(d.name.split("-")[0])
        if not (d / "最终测试点.md").exists():
            continue
        changed = [n for n, (path, sig) in (m.get("cfg_refs") or {}).items() if Path(path).exists() and qa_index.file_sig(Path(path)) != sig]
        if changed:
            todo.append(f"需求 {rid} 引用的配置表 {'、'.join(changed[:5])} 改过了，测试点和用例里的数值要校对 → {qa_cmd()} prompt cfg-check {rid}")
        elif m.get("cfg_pending"):
            snap = config_snapshot(ws) if snap is None else snap
            if snap != m.get("cfg_snapshot"):
                todo.append(f"需求 {rid} 有 {m['cfg_pending']} 处“待配表”，配置表有更新了 → {qa_cmd()} prompt cfg-check {rid}")
    return todo


def change_reminders(ws: Ws) -> list[str]:
    todo = []
    for d in ws.req_dirs():
        rid, m = d.name.split("-")[0], ws.req_meta(d.name.split("-")[0])
        src = Path(m["from"]) if m.get("from") else None
        if src and src.is_file() and m.get("from_sha") and sha256(src) != m["from_sha"]:
            todo.append(f"需求 {rid} 的原文件改过了（{src.name}）→ {qa_cmd()} req refresh {rid}（新导出的需求单是另一个文件就加 --from 新文件）")
        ch = pending_change(ws, rid)
        if ch and ch["status"] == "待处理":
            todo.append(f"需求 {rid} 改了，要出测试点变更和回归影响 → {qa_cmd()} prompt req-change {rid}")
        elif ch:
            todo.append(f"需求 {rid} 的变更审核表待填：{ch['dir']}/审核.md，填完 {qa_cmd()} accept {rid}")
    return todo


def refresh_req(ws: Ws, rid: str, src_arg: str | None) -> int:
    d, meta = ws.req_dir(rid), ws.req_meta(rid)
    src = Path(src_arg) if src_arg else (Path(meta["from"]) if meta.get("from") else None)
    if not src or not src.is_file():
        out(f"不知道需求原文件在哪：用 {qa_cmd()} req refresh {rid} --from <新的需求文件>")
        return 1
    new, old = req_text_from(src, meta.get("title") or d.name), read(d / "需求.md")
    meta.update({"from": str(src.resolve()), "from_sha": sha256(src)})
    if re.sub(r"\s+", "", new) == re.sub(r"\s+", "", old):
        ws.save()
        out(f"需求 {rid} 的内容没有变化（只是文件被保存过），不用改测试点。")
        return 0
    if pending_change(ws, rid):
        out(f"需求 {rid} 上一次变更还没处理完（{pending_change(ws, rid)['dir']}），先处理完再导入新的改动。")
        return 1
    st = stamp()
    cdir = d / f"变更_{st}"
    import difflib
    diff = "\n".join(difflib.unified_diff(old.splitlines(), new.splitlines(), "旧需求", "新需求", lineterm="", n=2))
    write(cdir / "旧需求.md", old)
    write(cdir / "需求改动.md", f"# 需求改动：{d.name}（{st}）\n\n来源：{src.name}\n\n“-”开头是删掉的，“+”开头是新加的。\n\n```diff\n{diff}\n```\n")
    write(d / "需求.md", new)
    finalized = (d / "最终测试点.md").exists()
    meta.setdefault("changes", []).append({"time": now_iso(), "dir": ws.rel(cdir), "status": "待处理" if finalized else "未定稿时改的"})
    ws.save()
    git_commit(ws, f"需求 {d.name}：原文有改动（{st}）")
    out(f"已更新 {d / '需求.md'}，新旧对比在 {cdir / '需求改动.md'}。")
    if finalized:
        out(f"下一步：{qa_cmd()} prompt req-change {rid}：AI 出“要改的测试点”和“受影响要回归的原有功能”，再 review、accept。")
    else:
        out(f"测试点还没定稿：直接重新生成 {qa_cmd()} prompt testpoints {rid} --force（审核表也 review --force 重建）。")
    return 0


def accept_change(ws: Ws, d: Path, rid: str, ch: dict) -> int:
    """需求变更的审核填完了：新增、修改、作废合进最终测试点；回归项写成这次变更的回归清单。"""
    cdir = ws.root / ch["dir"]
    rows, meta = parse_review(cdir / "审核.md")
    probs = review_problems(rows, meta)
    if probs:
        out("变更审核表还没填完：")
        for p in probs:
            out("  · " + p)
        return 1
    pts = {p["no"]: p for p in parse_points(cdir / "测试点变更.md")}
    _, final = read_table(d / "最终测试点.md")
    pos = lambda t: next((i for i, r in enumerate(final) if r.get("编号") == t), None)
    nxt = max([int(n) for r in final if (n := re.sub(r"\D", "", r.get("编号", "")))] or [0]) + 1
    st = cdir.name.replace("变更_", "")
    cnt = {"新增": 0, "修改": 0, "作废": 0, "回归": 0}
    regress = []
    for r in rows:
        if r["verdict"] == "否决":
            continue
        p = pts.get(r["no"], {})
        text = r["edit"] if r["verdict"] == "改后采纳" else (p.get("point") or r["point"])
        kind, orig = p.get("change", ""), p.get("orig", "").strip()
        fields_ = {"风险等级": p.get("risk", ""), "类型": p.get("type", ""), "依据": p.get("basis", ""), "怎么验证": p.get("verify", "")}
        if "回归" in kind:
            regress.append({"原编号": orig, "测试点": text, **fields_})
            cnt["回归"] += 1
        elif "作废" in kind and pos(orig) is not None:
            final.pop(pos(orig))
            cnt["作废"] += 1
        elif "修改" in kind and pos(orig) is not None:
            final[pos(orig)].update({"测试点": text, **{k: v for k, v in fields_.items() if v}, "来源": f"需求变更 {st} 修改"})
            cnt["修改"] += 1
        else:
            final.append({"编号": f"T{nxt:02d}", "测试点": text, **fields_, "来源": f"需求变更 {st} 新增", "AI 原编号": r["no"]})
            nxt += 1
            cnt["新增"] += 1
    write_final(d, final, f"最近一次变更：{st}（{ch['dir']}）　定稿时间：{now_iso()}")
    head = ["编号", "原编号", "测试点", "风险等级", "类型", "依据", "怎么验证", "结果", "缺陷编号"]
    lines = [f"# 回归清单：{d.name} 需求变更 {st}", "", "需求改动可能影响到的原有功能。结果填 通过 / 失败 / 阻塞 / 未测。", "",
             "| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    lines += [f"| G{i:02d} | {g['原编号']} | {g['测试点']} | {g['风险等级']} | {g['类型']} | {g['依据']} | {g['怎么验证']} |  |  |"
              for i, g in enumerate(regress, 1)]
    write(cdir / "回归清单.md", "\n".join(lines) + "\n")
    ch.update({"status": "已定稿", "accepted": now_iso(), **cnt, "minutes": meta.get("审核耗时（分钟）", "")})
    ws.req_meta(rid).pop("merged", None)  # 规则变了，要重新并进知识库
    record_cfg(ws, rid)
    ws.save()
    git_commit(ws, f"需求 {d.name}：变更定稿（{st}）")
    out(f"变更已合进 {d / '最终测试点.md'}：新增 {cnt['新增']}、修改 {cnt['修改']}、作废 {cnt['作废']}；回归 {cnt['回归']} 项写在 {cdir / '回归清单.md'}。")
    out(f"下一步：按回归清单回归（自动化的照“要重跑的自动化用例”跑），再把改了的规则并进知识库：{qa_cmd()} prompt wiki-merge {rid}")
    return 0


def req_text_from(src: Path, title: str) -> str:
    """需求文件转成 需求.md 的内容：Word、Excel、网页、PDF 先转成文字（禅道、飞书等导出的需求单也行），Markdown 和文本原样用。"""
    if not src.is_file():
        raise QaError(f"找不到需求文件 {src}")
    try:
        body = qa_index.doc_text(src)
    except qa_index.IndexError_ as e:
        raise QaError(str(e)) from e
    if src.suffix.lower() not in (".md", ".txt"):
        body = f"# 需求：{title}\n\n> 来源：{src.name}（从原文件转换导入；图片和流程图没有转进来，以原文件为准）\n\n{body.strip()}\n"
    return body


def new_req(ws: Ws, title: str, src: Path | None, modules: list[str], quiet: bool = False) -> Path:
    rid = next_id([p.name for p in ws.req_dirs()], "R")
    d = ws.reqs / f"{rid}-{safe_name(title)}"
    write(d / "需求.md", req_text_from(src, title) if src else render(ws.template("需求模板.md"), {"title": title}))
    meta = ws.req_meta(rid)
    meta.update({"title": title, "modules": modules, "created": now_iso()})
    if src:  # 记住原文件和它当时的样子：原文件后来改了，status 会提醒 req refresh
        meta.update({"from": str(src.resolve()), "from_sha": sha256(src)})
    if not quiet:
        out(f"已建需求 {rid}：{d / '需求.md'}" + ("（照模板写好需求再生成测试点）" if not src else "") +
            (f"，模块：{'、'.join(modules)}" if modules else ""))
    return d


def cmd_req(args) -> int:
    ws = ws_from_args(args)
    if args.action == "new":
        if not args.title:
            out("要给需求起个标题：python qa.py req new \"广告宝箱\" [--from 需求文件] [--module 模块1,模块2]")
            return 1
        raw = [m.strip() for m in (args.module or "").replace("，", ",").split(",") if m.strip()]
        mods, hints, unknown = normalize_modules(ws, raw)
        d = new_req(ws, args.title, Path(args.src) if args.src else None, mods + unknown)
        rid = d.name.split("-")[0]
        meta = ws.req_meta(rid)
        meta["module_hints"] = [p for p, _ in hints]
        meta["unknown_modules"] = unknown
        renamed = [f"{a}→{n[0]}" for a in raw if (n := normalize_modules(ws, [a])[0]) and n[0] != a]
        if renamed:
            out(f"  模块名已按模块清单统一：{'、'.join(renamed)}")
        if unknown:
            out(f"  ⚠️ 模块清单里没有：{'、'.join(unknown)}。是哪个模块的别名？补进 wiki/{CATALOG} 的“别名”列，或用 {qa_cmd()} req modules {rid} --set 改成清单里的名字")
        if hints or not raw:
            print_module_suggestions(ws, rid)
        ws.save()
        out(f"下一步：{qa_cmd()} prompt testpoints {rid}（让工作区里的 AI 生成），或 {qa_cmd()} gen testpoints {rid} --model <模型>（直接调模型接口）")
        return 0
    if args.action == "modules":
        if not args.title:
            out(f"要写需求编号：{qa_cmd()} req modules R002 [--set 模块1,模块2]")
            return 1
        rid = ws.req_dir(args.title).name.split("-")[0]
        meta = ws.req_meta(rid)
        if args.set:
            mods, hints, unknown = normalize_modules(ws, [m.strip() for m in args.set.replace("，", ",").split(",") if m.strip()])
            if hints:
                out(f"“{'、'.join(p for p, _ in hints)}”是大模块，要写具体是下面哪几个：" + "；".join(f"{p}：{'、'.join(ks)}" for p, ks in hints))
                return 1
            meta.update({"modules": mods + unknown, "unknown_modules": unknown, "module_hints": []})
            ws.save()
            out(f"需求 {rid} 的模块：{'、'.join(mods + unknown)}" + (f"。⚠️ 模块清单里没有：{'、'.join(unknown)}" if unknown else "") +
                f"。改了模块要重新运行 {qa_cmd()} prompt testpoints {rid}，拿到最新的耦合提示。")
            return 0
        out(f"需求 {rid} 现在标的模块：{'、'.join(meta.get('modules') or []) or '（没标）'}")
        print_module_suggestions(ws, rid)
        ws.save()
        return 0
    if args.action in ("refresh", "cfg-checked") and not args.title:
        out(f"要写需求编号：{qa_cmd()} req {args.action} R002")
        return 1
    if args.action == "refresh":
        return refresh_req(ws, ws.req_dir(args.title).name.split("-")[0], args.src)
    if args.action == "cfg-checked":
        rid = ws.req_dir(args.title).name.split("-")[0]
        record_cfg(ws, rid)
        ws.save()
        git_commit(ws, f"需求 {ws.req_dir(rid).name}：配置数值校对")
        m = ws.req_meta(rid)
        out(f"已记下需求 {rid} 现在的配置状态（引用了 {len(m['cfg_refs'])} 张表" +
            (f"，还有 {m['cfg_pending']} 处待配表" if m["cfg_pending"] else "") + "）；之后配置再变会再提醒。")
        return 0
    if args.action == "merged":
        if not args.title:
            out(f"要写需求编号：{qa_cmd()} req merged R001（这个需求没有要写进知识库的新规则时用；有的话用 prompt wiki-merge）")
            return 1
        marked = mark_merged(ws, args.title)
        ws.save()
        out(f"已标记：{', '.join(marked)} 不用再并入知识库。")
        return 0
    rows = req_rows(ws)
    if not rows:
        out("还没有需求。python qa.py req new \"标题\" --from 需求文件 --module 模块")
    for r in rows:
        mods = ws.req_meta(r["id"]).get("modules") or []
        out(f"  {r['id']}  {r['name']}" + (f"〔{'、'.join(mods)}〕" if mods else "") + f"：{r['stage']}")
    return 0


def parse_points(path: Path) -> list[dict]:
    pts = []
    for table in md_tables(read(path)):
        head = table[0]
        col = next((i for i, h in enumerate(head) if "测试点" in h), None)
        if col is None:
            continue
        idx = {h: i for i, h in enumerate(head)}
        get = lambda r, key: next((r[i] for h, i in idx.items() if key in h and i < len(r)), "")
        no_col = idx.get("编号", next((i for h, i in idx.items() if "编号" in h and "原" not in h), None))
        for r in table[1:]:
            if col < len(r) and r[col]:
                no = r[no_col] if no_col is not None and no_col < len(r) else ""
                pts.append({"no": no or str(len(pts) + 1), "point": r[col].replace("|", "/"),
                            "risk": get(r, "风险"), "type": get(r, "类型"), "basis": get(r, "依据"), "verify": get(r, "验证"),
                            "change": get(r, "变更"), "orig": get(r, "原编号")})
        break
    return pts


def parse_review(path: Path) -> tuple[list[dict], dict]:
    text = read(path)
    meta = {}
    for key in ("审核耗时（分钟）", "模型名称"):
        m = re.search(rf"^{re.escape(key)}[：:][ \t]*(.*)$", text, re.M)
        meta[key] = m.group(1).strip() if m else ""
    meta["added"] = [x.strip()[2:].strip() for x in section(text, "审核时补充的测试点").splitlines()
                     if x.strip().startswith("- ") and x.strip()[2:].strip()]
    rows = []
    for table in md_tables(text):
        head = table[0]
        if "结论" not in head:
            continue
        idx = {h: i for i, h in enumerate(head)}
        cell = lambda r, name: r[idx[name]].strip() if name in idx and idx[name] < len(r) else ""
        for r in table[1:]:
            rows.append({"no": cell(r, "编号"), "point": cell(r, "测试点"), "verdict": cell(r, "结论"),
                         "reason": cell(r, "否决原因"), "edit": cell(r, "修改后的内容")})
        break
    return rows, meta


def review_problems(rows: list[dict], meta: dict) -> list[str]:
    probs = []
    if not rows:
        probs.append("审核表里没有测试点")
    for label, bad in (
        ("没填结论", [r["no"] for r in rows if not r["verdict"]]),
        ("结论只能填 采纳 / 改后采纳 / 否决", [r["no"] for r in rows if r["verdict"] and r["verdict"] not in VERDICTS]),
        ("否决要填原因（重复 / 太笼统 / 不符合规则 / 无关）", [r["no"] for r in rows if r["verdict"] == "否决" and r["reason"] not in REJECT_REASONS]),
        ("改后采纳要填“修改后的内容”", [r["no"] for r in rows if r["verdict"] == "改后采纳" and not r["edit"]]),
    ):
        if bad:
            probs.append(f"{label}：编号 {', '.join(bad[:10])}{'…' if len(bad) > 10 else ''}（共 {len(bad)} 条）")
    if not re.fullmatch(r"\d+(\.\d+)?", meta.get("审核耗时（分钟）", "")):
        probs.append("顶部“审核耗时（分钟）”没填数字")
    return probs


def review_stats(rows: list[dict], meta: dict) -> dict:
    return {"generated": len(rows), "accepted": sum(r["verdict"] == "采纳" for r in rows),
            "modified": sum(r["verdict"] == "改后采纳" for r in rows), "rejected": sum(r["verdict"] == "否决" for r in rows),
            "fabricated": sum(r["verdict"] == "否决" and r["reason"] == "不符合规则" for r in rows),
            "added": len(meta["added"]), "minutes": float(meta["审核耗时（分钟）"] or 0)}


def generated_by(src: Path) -> str:
    """读测试点文件开头的“<!-- 生成：工具或模型名，时间 -->”，取出是谁生成的（括号里的逗号不算分隔）。"""
    m = re.search(r"<!--\s*生成[：:]\s*(.*?)-->", "\n".join(read(src).splitlines()[:5]))
    if not m:
        return ""
    depth, name = 0, ""
    for ch in m.group(1):
        depth += ch in "（("
        depth -= ch in "）)"
        if depth <= 0 and ch in "，,":
            break
        name += ch
    return name.strip()


def make_review(ws: Ws, src: Path, dst: Path, title: str, blind: bool = False) -> int:
    pts = parse_points(src)
    label = lambda p: (f"【{p['change']}{' ' + p['orig'] if p['orig'] else ''}】" if p.get("change") else "") + p["point"]  # 需求变更时标出是新增、修改、作废还是回归
    rows = "\n".join(f"| {p['no']} | {label(p)} |  |  |  |" for p in pts) or "| 1 | （没解析出测试点，把 AI 的测试点手动抄进来） |  |  |  |"
    text = render(ws.template("审核表.md"), {"req": title, "rows": rows})
    model = "" if blind else generated_by(src)  # 模型对比要盲审，不预填
    if model:
        text = re.sub(r"^(模型名称[：:])[ \t]*$", lambda m: m.group(1) + model, text, count=1, flags=re.M)
    write(dst, text)
    return len(pts)


def cmd_review(args) -> int:
    ws = ws_from_args(args)
    d = ws.req_dir(args.req)
    rid = d.name.split("-")[0]
    ch = pending_change(ws, rid)
    if ch:  # 需求变更：只审变动和回归项
        cdir = ws.root / ch["dir"]
        if not (cdir / "测试点变更.md").exists():
            out(f"这次需求变更还没出测试点变更：{qa_cmd()} prompt req-change {rid}")
            return 1
        if (cdir / "审核.md").exists() and not args.force:
            out(f"变更审核表已经有了：{cdir / '审核.md'}。直接填；要重新生成加 --force。")
            return 1
        n = make_review(ws, cdir / "测试点变更.md", cdir / "审核.md", f"{d.name}（需求变更 {cdir.name.replace('变更_', '')}）")
        ch["status"] = "待审核"
        ws.save()
        out(f"变更审核表已生成：{cdir / '审核.md'}（{n} 条，只有变动和回归项）。请人逐条填结论；填完运行 {qa_cmd()} accept {rid}")
        return 0
    src = d / "测试点.md"
    if not src.exists():
        out(f"还没有 {src}。先生成：{qa_cmd()} prompt testpoints {args.req.upper()}，或 {qa_cmd()} gen testpoints {args.req.upper()}")
        return 1
    dst = d / "审核.md"
    if dst.exists() and not args.force:
        out(f"审核表已经有了：{dst}。直接填；要按新的测试点重新生成就加 --force（会清掉已填的内容）。")
        return 1
    n = make_review(ws, src, dst, d.name)
    out(f"审核表已生成：{dst}（{n} 条）。请人逐条填结论；填完运行 {qa_cmd()} accept {args.req.upper()}")
    for g in coupling_gaps(ws, d.name.split("-")[0], parse_points(src)):
        out(f"  ⚠️ 耦合提醒：{g}。审核时可以在“审核时补充的测试点”里补上。")
    return 0


def cmd_accept(args) -> int:
    ws = ws_from_args(args)
    d = ws.req_dir(args.req)
    rid = d.name.split("-")[0]
    ch = pending_change(ws, rid)
    if ch:
        if not (ws.root / ch["dir"] / "审核.md").exists():
            out(f"这次需求变更还没有审核表：{qa_cmd()} review {rid}")
            return 1
        return accept_change(ws, d, rid, ch)
    rv = d / "审核.md"
    if not rv.exists():
        out(f"还没有审核表：{qa_cmd()} review {rid}")
        return 1
    rows, meta = parse_review(rv)
    probs = review_problems(rows, meta)
    if probs:
        out("审核表还没填完：")
        for p in probs:
            out("  · " + p)
        return 1
    orig = {p["no"]: p for p in parse_points(d / "测试点.md")} if (d / "测试点.md").exists() else {}
    final = []
    for r in rows:
        if r["verdict"] == "否决":
            continue
        o = orig.get(r["no"], {})
        final.append({"point": r["edit"] if r["verdict"] == "改后采纳" else r["point"], "risk": o.get("risk", ""),
                      "type": o.get("type", ""), "basis": o.get("basis", ""), "verify": o.get("verify", ""),
                      "src": "AI" if r["verdict"] == "采纳" else "AI（人改过）", "ai_no": r["no"]})
    for a in meta["added"]:
        final.append({"point": a, "risk": "", "type": "", "basis": "", "verify": "", "src": "人工补充", "ai_no": ""})
    lines = [f"# 最终测试点：{d.name}", "", f"定稿时间：{now_iso()}　审核：{rv.name}", "",
             "| 编号 | 测试点 | 风险等级 | 类型 | 依据 | 怎么验证 | 来源 | AI 原编号 |", "|---|---|---|---|---|---|---|---|"]
    for i, f in enumerate(final, 1):
        lines.append(f"| T{i:02d} | {f['point']} | {f['risk']} | {f['type']} | {f['basis']} | {f['verify']} | {f['src']} | {f['ai_no']} |")
    write(d / "最终测试点.md", "\n".join(lines) + "\n")
    st = review_stats(rows, meta)
    gaps = coupling_gaps(ws, rid, final)
    meta_req = ws.req_meta(rid)
    meta_req.update({**st, "time": now_iso(), "model": meta["模型名称"] or meta_req.get("model", ""), "final": len(final),
                     "coupling_gaps": gaps})
    record_cfg(ws, rid)
    ws.save()
    git_commit(ws, f"需求 {d.name}：测试点定稿")
    out(f"已定稿：{d / '最终测试点.md'}（{len(final)} 条）。AI 生成 {st['generated']} 条，"
        f"采纳率 {pct(st['accepted'] + st['modified'], st['generated'])}，编造 {st['fabricated']} 条，人工补充 {st['added']} 条，审核 {meta['审核耗时（分钟）']} 分钟。")
    for g in gaps:
        out(f"  ⚠️ 耦合提醒：{g}（已定稿，可以重新审核补上，或确认确实不相关）")
    out(f"下一步：把这个需求定下的新规则、新耦合并进知识库，以后的需求才看得到 → {qa_cmd()} prompt wiki-merge {rid}"
        f"（没有新规则就 {qa_cmd()} req merged {rid}）；同时可以开始执行或写自动化用例。")
    run_hook(ws, "after_accept", {"QA_REQ": rid})
    return 0


def req_rows(ws: Ws) -> list[dict]:
    from .gen import bench_runs  # noqa: E402  上层模块，在函数里导入免得循环
    rows = []
    for d in ws.req_dirs():
        rid = d.name.split("-")[0]
        if (d / "最终测试点.md").exists():
            stage = "已定稿"
        elif (d / "审核.md").exists():
            probs = review_problems(*parse_review(d / "审核.md"))
            stage = f"审核中（{probs[0]}）" if probs else f"审核填完了，待定稿：{qa_cmd()} accept {rid}"
        elif (d / "测试点.md").exists():
            stage = f"待审核：{qa_cmd()} review {rid}"
        elif bench_runs(ws, rid):
            stage = f"在做模型对比：{qa_cmd()} bench report {rid}"
        else:
            stage = f"待生成测试点：{qa_cmd()} prompt testpoints {rid}（或 gen testpoints {rid}）"
        rows.append({"id": rid, "name": d.name, "stage": stage, "done": stage == "已定稿"})
    return rows


def defect_info(p: Path) -> dict:
    text = read(p)
    f = fields(text)
    cover = f.get("被最终测试点覆盖", "")
    problems = []
    if f.get("严重程度", "") not in SEVERITIES:
        problems.append("严重程度填 P0–P3")
    if not (cover.startswith("是") or cover.startswith("否")):
        problems.append("“被最终测试点覆盖”填 是（编号）或 否")
    if not f.get("归类"):
        problems.append("归类没填")
    if not section(text, "现象"):
        problems.append("现象没写")
    return {"id": p.name.split("-")[0].split(".")[0], "file": p, "title": (text.splitlines() or [""])[0].lstrip("# ").strip(),
            "req": f.get("关联需求", "").upper(), "module": f.get("模块", ""), "stage": f.get("发现阶段", ""),
            "how": f.get("发现方式", ""), "severity": f.get("严重程度", ""), "escaped": cover.startswith("否"),
            "kind": f.get("归类", ""), "fed_back": f.get("已回流", "").startswith("是"), "problems": problems,
            "phenomenon": section(text, "现象"), "cause": section(text, "原因"), "howto": section(text, "以后怎么测")}


def cmd_defect(args) -> int:
    ws = ws_from_args(args)
    if args.action == "new":
        if not args.title:
            out("要写缺陷标题：python qa.py defect new \"领奖回包丢失后重复到账\" [--req R001] [--stage 测试|线上] [--from-test 用例名]")
            return 1
        did = next_id([p.name for p in ws.defect_files()], "D")
        req = (args.req or "").upper()
        mods = "、".join(ws.req_meta(req).get("modules") or []) if req else ""
        text = render(ws.template("缺陷模板.md"), {
            "id": did, "title": args.title, "req": req, "module": mods, "stage": args.stage or "测试",
            "how": "自动化" if args.from_test else "手工", "test": args.from_test or ""})
        p = ws.defects / f"{did}-{safe_name(args.title)}.md"
        write(p, text)
        out(f"已建缺陷记录：{p}。把严重程度、是否被最终测试点覆盖、归类、现象填上。")
        return 0
    if args.action == "done":
        marked = mark_feedback(ws, ",".join(args.ids))
        ws.save()
        out(f"已标记为已回流：{', '.join(marked)}（一般用 wiki commit --feedback 一起标）")
        return 0
    infos = [defect_info(p) for p in ws.defect_files()]
    if not infos:
        out("还没有缺陷记录。")
    for d in infos:
        state = ("未填完：" + "；".join(d["problems"]) if d["problems"] else
                 ("漏测，待回流" if d["escaped"] and not d["fed_back"] else ("漏测，已回流" if d["escaped"] else "测试点覆盖到了")))
        out(f"  {d['id']} {d['title']}（{d['severity'] or '?'}，{d['stage'] or '?'}{'，' + d['module'] if d['module'] else ''}）：{state}")
    return 0
