"""大批资料：登记资料目录、建索引、检索、查配置表、查历史缺陷、向量模型；生成时取检索内容。"""
from __future__ import annotations

import json
import time
from pathlib import Path

import qa_index

from .common import QaError, now_iso, out, qa_cmd, read, write
from .workspace import ws_from_args

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .workspace import Ws


KIND_NAME = {"doc": "文档", "config": "配置表", "defect": "历史缺陷"}


def index_path(ws: Ws) -> Path:
    return ws.root / ".qa" / "index.sqlite"


def ensure_index_ignored(ws: Ws) -> None:
    gi = ws.root / ".gitignore"
    text = read(gi) if gi.exists() else ""
    if ".qa/index.sqlite" not in text:
        write(gi, text.rstrip("\n") + "\n.qa/index.sqlite*\n")  # 索引可以随时重建，又大，不进 git


def open_index(ws: Ws, must: bool = True) -> qa_index.Index | None:
    if not index_path(ws).exists():
        if must:
            raise QaError("还没建资料索引：先 qa.py corpus add <文件夹> --kind doc|config|defect 登记资料，再 qa.py index")
        return None
    return qa_index.Index(index_path(ws))


BUILTIN_CORPORA = ("本工作区需求", "本工作区缺陷")


def builtin_corpora(ws: Ws) -> list[dict]:
    """工作区自己产生的资料也进索引：做过的需求（需求原文和最终测试点）、登记过的缺陷。
    这样 search 能查到以前需求里定下的新规则，history find 能查到本工作区发现的缺陷，不用等缺陷系统下次导出。"""
    return [{"name": BUILTIN_CORPORA[0], "kind": "doc", "path": str(ws.reqs.resolve()), "only": ["需求.md", "最终测试点.md"]},
            {"name": BUILTIN_CORPORA[1], "kind": "defect", "path": str(ws.defects.resolve()), "glob": "D*.md"}]


def all_corpora(ws: Ws) -> list[dict]:
    return (ws.cfg.get("corpora") or []) + builtin_corpora(ws)


def index_pending(ws: Ws) -> dict | None:
    if not index_path(ws).exists():  # 没建过索引：登记了资料才提醒（小项目不用索引，不打扰）
        return {"new": ["?"], "changed": [], "deleted": [], "never": True} if ws.cfg.get("corpora") else None
    idx = open_index(ws)
    try:
        return qa_index.pending(idx, all_corpora(ws))
    finally:
        idx.close()


def corpus_doc_sigs(ws: Ws) -> dict:
    """登记的文档类资料现在的状态（文件大小 + 修改时间），知识库提交时记下来，之后比对有没有变化。"""
    return {str(p): qa_index.file_sig(p) for c in ws.cfg.get("corpora") or [] if c["kind"] == "doc" for p in qa_index.corpus_files(c)}


def corpus_changes(ws: Ws) -> dict | None:
    """自上次提交知识库以来，登记的资料文件夹里哪些文档新增、改动、删除了（没有对照基线时返回 None）。"""
    commits = ws.state["wiki"]["commits"]
    if not commits or "corpus" not in commits[-1]:
        return None
    last, now = commits[-1]["corpus"], corpus_doc_sigs(ws)
    return {"new": sorted(k for k in now if k not in last), "changed": sorted(k for k in now if k in last and last[k] != now[k]),
            "deleted": sorted(k for k in last if k not in now)}


def cmd_corpus(args) -> int:
    ws = ws_from_args(args)
    corpora = ws.cfg.setdefault("corpora", [])
    if args.action == "list":
        if not corpora:
            out("还没登记资料目录。qa.py corpus add <文件夹> --kind doc|config|defect")
        for c in corpora:
            out(f"  {c['name']}（{KIND_NAME[c['kind']]}）：{c['path']}，{len(qa_index.corpus_files(c))} 个文件")
        for c in builtin_corpora(ws):
            out(f"  {c['name']}（{KIND_NAME[c['kind']]}，工作区自带，不用登记）：{len(qa_index.corpus_files(c))} 个文件")
        return 0
    if (args.target if args.action == "remove" else args.name) in BUILTIN_CORPORA:
        out(f"“{'、'.join(BUILTIN_CORPORA)}”是工作区自带的资料名，换个名字。")
        return 1
    if args.action == "remove":
        ws.cfg["corpora"] = [c for c in corpora if c["name"] != args.target]
        ws.save_cfg()
        out(f"已移除 {args.target}，下次 qa.py index 会把它的索引删掉。")
        return 0
    if not args.target or not args.kind:
        out("用法：qa.py corpus add <文件夹或文件> --kind doc|config|defect [--name 名字]\n"
            "  doc = 策划案等文档（Word / Excel / 网页 / PDF / Markdown / 文本），config = 配置表（Excel / CSV / JSON），defect = 缺陷系统导出的 CSV / Excel")
        return 1
    p = Path(args.target).resolve()
    if not p.exists():
        out(f"找不到 {p}")
        return 1
    root = ws.root.resolve()
    if p == root or root in p.parents or p in root.parents:
        # 工作区里的知识库、需求会被当成“资料”：知识库一改资料就变，又提醒更新知识库，自动流转时会转不停
        out(f"{p} 和工作区 {root} 重叠了：工作区自己的需求和缺陷框架会自动收进索引，不用登记；"
            "资料目录要放在工作区外面（比如 SVN 检出的策划文档文件夹）。")
        return 1
    name = args.name or p.stem
    if name in BUILTIN_CORPORA:
        out(f"“{name}”是工作区自带的资料名，用 --name 换个名字。")
        return 1
    entry = {"name": name, "kind": args.kind, "path": str(p)}
    ws.cfg["corpora"] = [c for c in corpora if c["name"] != name] + [entry]
    ws.save_cfg()
    n = len(qa_index.corpus_files(entry))
    out(f"已登记资料“{name}”（{KIND_NAME[args.kind]}）：{p}，找到 {n} 个文件。资料留在原处，不复制进工作区。接着运行 {qa_cmd()} index 建索引。")
    return 0


def cmd_index(args) -> int:
    from .wiki import annotate_obsolete  # noqa: E402  上层模块，在函数里导入免得循环
    ws = ws_from_args(args)
    corpora = all_corpora(ws)
    if args.action == "show":
        if not args.file:
            out(f"要写文件名：{qa_cmd()} index show <文件名或路径>（Word、PDF 也能直接看，读的是索引里转好的文字）")
            return 1
        idx = open_index(ws)
        try:
            paths, rows = qa_index.file_chunks(idx, args.file)
        finally:
            idx.close()
        if not paths:
            out(f"索引里没有 {args.file}。{qa_cmd()} index stats 看建了哪些；文件是新加的就先 {qa_cmd()} index")
            return 1
        if len(paths) > 1:
            out(f"匹配到 {len(paths)} 个文件，显示第一个；要看别的就写完整一点：" + "；".join(paths[:10]))
        out(f"===== {paths[0]} =====")
        last = None
        rows = [(h["title"], h["text"]) for h in annotate_obsolete(ws, [{"path": paths[0], "title": t, "text": x} for t, x in rows], reorder=False)]
        for title, text in rows:
            if title != last:
                out(f"\n【{title}】")
                last = title
            out(text.strip())
        return 0
    if args.action == "stats":
        idx = open_index(ws)
        try:
            s = idx.stats()
            todo = qa_index.pending(idx, corpora)
        finally:
            idx.close()
        out(f"资料索引：{s['files']} 个文件；文档 {s['docs']} 份切成 {s['doc_chunks']} 段；配置表 {s['tables']} 张、{s['table_rows']} 行；"
            f"历史缺陷 {s['defects']} 条；向量 {s['vectors']} 条")
        out(f"待更新：新增 {len(todo['new'])}、改动 {len(todo['changed'])}、删除 {len(todo['deleted'])} 个文件")
        emb = ws.cfg.get("embedding")
        out("向量模型：" + (f"{emb.get('provider')} / {emb.get('model', '')}" if emb else "没配（只用关键词检索；qa.py embed set ollama --model bge-m3 接本地模型）"))
        return 0
    if not ws.cfg.get("corpora") and not any(qa_index.corpus_files(c) for c in corpora):
        out(f"还没登记资料目录：{qa_cmd()} corpus add <文件夹> --kind doc|config|defect")
        return 1
    if args.rebuild and index_path(ws).exists():
        index_path(ws).unlink()
    ensure_index_ignored(ws)
    idx = qa_index.Index(index_path(ws))
    try:
        res = qa_index.build(idx, corpora, ws.cfg.get("defect_columns"),
                             progress=lambda i, n: out(f"  已处理 {i}/{n} 个文件……"))
        out(f"建索引完成：处理 {res['indexed']} 个文件（新增 {res['new']}、改动 {res['changed']}），删除 {res['deleted']} 个，用时 {res['seconds']} 秒。")
        for note in res["notes"][:10]:
            out(f"  · {note}")
        if res["skipped"]:
            out(f"跳过 {len(res['skipped'])} 个：" + "；".join(res["skipped"][:5]) + ("…" if len(res["skipped"]) > 5 else ""))
        emb = ws.cfg.get("embedding")
        if emb and not args.no_embed:
            try:
                t0 = time.monotonic()
                n = idx.embed_missing(emb, progress=lambda d, t: out(f"  向量 {d}/{t}……") if d % 320 == 0 else None)
                out(f"向量：新算了 {n} 段，用时 {time.monotonic() - t0:.1f} 秒。")
            except qa_index.IndexError_ as e:
                out(f"向量没算成：{e}。关键词检索照常能用；向量模型准备好后再运行一次 {qa_cmd()} index。")
        s = idx.stats()
        if s["tables"]:
            links = idx.links()  # 顺便算好表间关联，后面查表时直接用
            hi = sum(l["level"] == "高" for l in links)
            out(f"配置表关联：自动认出 {len(links)} 条（可信度高 {hi} 条、要确认 {len(links) - hi} 条），{qa_cmd()} cfg links 查看。")
    finally:
        idx.close()
    ws.state["index"] = {"time": now_iso(), **s}
    commits = ws.state["wiki"]["commits"]
    if commits and "corpus" not in commits[-1] and ws.cfg.get("corpora"):
        # 知识库是在登记资料之前提交的：把现在的资料状态记为对照基线，以后文档有变化 status 才能提醒更新知识库
        commits[-1]["corpus"] = corpus_doc_sigs(ws)
        out(f"已把现在的 {len(commits[-1]['corpus'])} 份文档记为知识库的对照基线：以后这些文档有新增或改动，{qa_cmd()} status 会提醒更新知识库。")
    ws.save()
    return 0


def show_hits(hits: list[dict], chars: int, query: str = "") -> None:
    for i, h in enumerate(hits, 1):
        out(f"[{i}] {KIND_NAME.get(h['kind'], h['kind'])} · {h['corpus']} · {Path(h['path']).name} · {h['title']}")
        body = qa_index.snippet(h["text"], query, chars) if query else h["text"].strip()[:chars]
        out("    " + body.replace("\n", "\n    ") + ("…" if len(h["text"].strip()) > len(body) else ""))
    if hits:
        out(f"（检索方式：{hits[0]['mode']}）")


def cmd_search(args) -> int:
    from .wiki import annotate_obsolete  # noqa: E402  上层模块，在函数里导入免得循环
    ws = ws_from_args(args)
    idx = open_index(ws)
    try:
        kinds = ("doc", "config", "defect") if args.kind == "all" else (args.kind,)
        hide = getattr(args, "exclude", None)
        hits = idx.search(" ".join(args.query), kinds, k=args.n + (6 if hide else 0), emb=ws.cfg.get("embedding"))
        hits = [h for h in hits if not in_req_dir(h["path"], hide)][:args.n]
    finally:
        idx.close()
    if not hits:
        out("没找到相关内容。换几个关键词试试（中文按两个字一组匹配）。")
        return 1
    show_hits(annotate_obsolete(ws, hits), args.chars, " ".join(args.query))
    return 0


def cmd_cfg(args) -> int:
    ws = ws_from_args(args)
    idx = open_index(ws)
    try:
        if args.action == "find":  # 找表也用向量：JSON 配置的表名多是英文（gear.rod），问“鱼竿”时靠意思对上
            hits = idx.search(" ".join(args.words), ("config",), k=args.n, emb=ws.cfg.get("embedding"))
            if not hits:
                out("没找到相关的配置表。")
                return 1
            for i, h in enumerate(hits, 1):
                t = idx.table(h["title"])
                out(f"[{i}] {t['name']}（{t['nrows']} 行，{Path(t['path']).name}）")
                out(f"    字段：{'、'.join(t['columns'][:30])}{'…' if len(t['columns']) > 30 else ''}")
                for n in t["notes"][:2]:
                    out(f"    注释：{' '.join(c for c in n if c)[:200]}")
            out(f"查具体数值：{qa_cmd()} cfg show <表名> [--where 字段=值] [--cols 字段1,字段2]")
            return 0
        if args.action == "links":
            return cmd_cfg_links(ws, idx, " ".join(args.words))
        if args.action == "list":
            rows = idx.con.execute("SELECT name, nrows FROM tables WHERE name LIKE ? ORDER BY name", (f"%{' '.join(args.words)}%",)).fetchall()
            out(f"{len(rows)} 张表：" + "；".join(f"{n}（{r} 行）" for n, r in rows[:200]) + ("…" if len(rows) > 200 else ""))
            return 0
        # show
        if not args.words:
            out("要写表名：qa.py cfg show <表名> [--where 字段=值] [--cols 字段1,字段2] [--limit 20]")
            return 1
        name = " ".join(args.words)
        t = idx.table(name)
        if not t:
            out(f"没有表 {name}。先用 qa.py cfg find <关键词> 找表名。")
            return 1
        where = []
        for w in args.where or []:
            if "=" not in w:
                out(f"--where 要写成 字段=值：{w}")
                return 1
            k, v = w.split("=", 1)
            where.append((k.strip(), v.strip()))
        try:
            hits, total = idx.table_rows(t["name"], where, args.limit)
        except qa_index.IndexError_ as e:
            out(str(e))
            return 1
        cols = t["columns"]
        pick = [c.strip() for c in args.cols.split(",")] if args.cols else cols
        bad = [c for c in pick if c not in cols]
        if bad:
            out(f"表 {t['name']} 没有字段：{'、'.join(bad)}。字段：{'、'.join(cols)}")
            return 1
        ids = [cols.index(c) for c in pick]
        out(f"表 {t['name']}（{Path(t['path']).name}，共 {t['nrows']} 行，符合条件 {total} 行，显示 {len(hits)} 行）")
        print_rows(pick, ids, hits)
        links = all_links(ws, idx)
        if args.expand:
            expand_links(idx, links, t["name"], cols, hits, args.expand, {t["name"]})
        mine = [l for l in links if l["from"] == t["name"]]
        if mine:
            out("关联（框架按取值自动算的，高 = 值全对上、名字也对得上，中 = 要确认）：" + "；".join(
                f"{link_label(l)}（{l['level']}）" for l in mine[:10]) + ("…" if len(mine) > 10 else "")
                + ("" if args.expand else "。要把关联表的行一起带出来，加 --expand"))
        used = sorted({l["from"] for l in links if l["to"] == t["name"] and l["level"] in ("高", "确认")})
        if used:
            out("被这些表引用：" + "、".join(used[:10]) + ("…" if len(used) > 10 else ""))
        return 0
    finally:
        idx.close()


LINK_PAGE = "配置关联.md"
LINK_VERDICTS = ("确认", "否决", "待确认")


def link_page_rows(ws: Ws) -> list[dict]:
    """wiki/配置关联.md：| 表 | 字段 | 关联表 | 关联字段 | 结论 | 依据 |。
    框架靠取值认不出的关联（编号写成表名、名字对不上的小数字……）在这里补；认错的在这里否决。
    结论：AI 只写“待确认”；人审时改成“确认”或“否决”。"""
    from .common import md_tables  # noqa: E402
    p = ws.wiki / LINK_PAGE
    if not p.exists():
        return []
    for table in md_tables(read(p)):
        head = [h.replace("*", "").strip() for h in table[0]]
        if "关联表" not in head or "表" not in head:
            continue
        find = lambda key: next((i for i, h in enumerate(head) if h == key), None) or next((i for i, h in enumerate(head) if key in h), None)
        cols = {"from": head.index("表"), "col": find("字段"), "to": head.index("关联表"), "key": find("关联字段"),
                "verdict": find("结论"), "why": find("依据")}
        rows = []
        for r in table[1:]:
            c = {k: (r[i].strip().strip("`") if i is not None and i < len(r) else "") for k, i in cols.items()}
            if c["from"] and c["to"] and c["col"]:
                c["verdict"] = next((v for v in LINK_VERDICTS if v == c["verdict"]), c["verdict"] or "待确认")
                rows.append(c)
        return rows
    return []


def all_links(ws: Ws, idx) -> list[dict]:
    """框架自动认出的关联 + wiki/配置关联.md 里人确认、补充、否决的。
    确认的和“高”一样用（连带查也走它）；否决的去掉；待确认的（AI 从文档、代码里看出来的）当“中”。"""
    links = [dict(l) for l in idx.links()]
    for r in link_page_rows(ws):
        same = [l for l in links if l["from"] == r["from"] and l["col"] == r["col"] and l["to"] == r["to"]]
        if r["verdict"] == "否决":
            links = [l for l in links if l not in same]
            continue
        level = "确认" if r["verdict"] == "确认" else "中"
        note = f"wiki/{LINK_PAGE}：{'人已确认' if level == '确认' else '待确认'}" + (f"（{r['why']}）" if r["why"] else "")
        if same:
            for l in same:
                l["level"] = level if level == "确认" else l["level"]
                l["why"] += "；" + note
        else:
            t = idx.table(r["from"])
            kind = "键值行" if t and qa_index.is_kv_table(t["columns"]) and r["col"] not in t["columns"] else "列"
            links.append({"from": r["from"], "col": r["col"], "to": r["to"], "key": r["key"] or "", "level": level,
                          "why": "框架没认出，" + note, "n": 0, "missing": [], "multi": False, "kind": kind, "target": "编号"})
    links.sort(key=lambda x: (x["from"], x["level"] not in ("确认", "高"), x["col"]))
    return links


def link_page_problems(ws: Ws) -> list[str]:
    """配置关联页里写的表、字段、关联字段在资料索引里找不到（写错了，或者表改名了）就报出来。"""
    rows = link_page_rows(ws)
    if not rows or not index_path(ws).exists():
        return []
    idx = open_index(ws)
    probs = []
    try:
        for r in rows:
            if r["verdict"] not in LINK_VERDICTS:
                probs.append(f"wiki/{LINK_PAGE}：{r['from']}.{r['col']} 的结论只能写 确认 / 否决 / 待确认，现在是“{r['verdict']}”")
            for name, col in ((r["from"], r["col"]), (r["to"], r["key"])):
                t = idx.con.execute("SELECT columns FROM tables WHERE name=?", (name,)).fetchone()
                if not t:
                    probs.append(f"wiki/{LINK_PAGE}：资料索引里没有表“{name}”（表名要和 cfg list 里的一致）")
                    continue
                cols = json.loads(t[0])
                kv = qa_index.is_kv_table(cols)
                if col and col not in cols and not (kv and name == r["from"]) and not col.endswith(".*"):
                    probs.append(f"wiki/{LINK_PAGE}：表 {name} 没有字段“{col}”。字段：{'、'.join(cols[:30])}")
    finally:
        idx.close()
    return probs


def print_rows(pick: list[str], ids: list[int], hits: list, cell: int = 0) -> None:
    clip = (lambda s: s if len(s) <= cell else s[:cell] + "…") if cell else (lambda s: s)
    out("| 行号 | " + " | ".join(pick) + " |")
    out("|---|" + "---|" * len(pick))
    for rn, row in hits:
        out(f"| {rn} | " + " | ".join(clip((row[i] if i < len(row) else "").replace("|", "/").replace("\n", " ")) for i in ids) + " |")


def link_label(l: dict) -> str:
    kind = {"键值行": "（参数）", "字段名": "（字段名里的编号）"}.get(l.get("kind", ""), "")
    return f"{l['col']}{kind} → {l['to']}.{l['key']}"


def cmd_cfg_links(ws: Ws, idx, name: str) -> int:
    links = all_links(ws, idx)
    if name:
        t = idx.table(name)
        if not t:
            out(f"没有表 {name}。先用 qa.py cfg find <关键词> 找表名。")
            return 1
        mine = [l for l in links if l["from"] == t["name"]]
        used = [l for l in links if l["to"] == t["name"]]
        out(f"表 {t['name']} 的关联（框架按取值自动算的：高 = 值全对上、名字也对得上；中 = 要人确认）")
        out("引用别的表：" + ("" if mine else "没认出"))
        for l in mine:
            out(f"  - [{l['level']}] {link_label(l)}：{l['why']}")
        out("被别的表引用：" + ("" if used else "没认出"))
        for l in used:
            out(f"  - [{l['level']}] {l['from']}.{l['col']} → {l['key']}：{l['why']}")
        return 0
    if not links:
        out("配置表之间没认出关联（或者还没登记配置表）。")
        return 0
    hi = sum(l["level"] in ("高", "确认") for l in links)
    out(f"配置表之间认出 {len(links)} 条关联：高或人已确认 {hi} 条，中 {len(links) - hi} 条（要人确认：在 wiki/{LINK_PAGE} 里写结论）。")
    out("认法：A 表某一列的值，全都能在 B 表的编号列（每行都有、不重复）里找到；再看字段名和表名对不对得上。")
    cur = None
    for l in links[:400]:
        if l["from"] != cur:
            cur = l["from"]
            out(f"{cur}")
        out(f"  - [{l['level']}] {link_label(l)}：{l['why']}")
    if len(links) > 400:
        out(f"……还有 {len(links) - 400} 条：qa.py cfg links <表名> 看一张表的")
    return 0


EXPAND_MAX_DEPTH, EXPAND_MAX_TABLES = 3, 12


def expand_links(idx, links: list[dict], tname: str, cols: list[str], rows: list, depth: int, seen: set, limit: int = 30) -> None:
    """顺着可信度高（或人已确认）的关联，把这些行引用到的其他表的行带出来（--expand）。
    seen 是整次展开共用的：每张表最多展开一次，最多 3 层、12 张表——表之间互相引用（A→B→A）也不会绕圈。"""
    depth = min(depth, EXPAND_MAX_DEPTH)
    for l in links:
        if l["from"] != tname or l["level"] not in ("高", "确认") or l["to"] in seen:
            continue
        if len(seen) > EXPAND_MAX_TABLES:
            out(f"\n（已经带出 {EXPAND_MAX_TABLES} 张表，后面的不再展开：要看就单独 cfg show）")
            return
        vals = set()
        for _, r in rows:
            if l.get("kind") == "键值行":
                if r and r[0] == l["col"]:
                    vals.update(qa_index.ref_values(r[1] if len(r) > 1 else ""))
            elif l.get("kind") == "字段名":
                pre = l["col"][:-2]
                vals.update(c.rsplit(".", 1)[1] for i, c in enumerate(cols) if c.startswith(pre + ".") and i < len(r) and r[i])
            elif l["col"] in cols:
                i = cols.index(l["col"])
                vals.update(qa_index.ref_values(r[i] if i < len(r) else ""))
        if not vals:
            continue
        t2 = idx.table(l["to"])
        if not t2 or l["key"] not in t2["columns"]:
            continue
        seen.add(t2["name"])
        k = t2["columns"].index(l["key"])
        all_rows, _ = idx.table_rows(t2["name"], [], qa_index.MAX_ROWS)
        hits = [(rn, r) for rn, r in all_rows if k < len(r) and r[k] in vals][:limit]
        cols2 = t2["columns"]
        out(f"\n↳ {link_label(l)}：表 {t2['name']} 里对上 {len(hits)} 行"
            + (f"（只列前 15 个字段，全部字段用 cfg show {t2['name']} 看）" if len(cols2) > 15 else ""))
        print_rows(cols2[:15], list(range(min(15, len(cols2)))), hits, cell=60)
        if depth > 1:
            expand_links(idx, links, t2["name"], t2["columns"], hits, depth - 1, seen, limit)


def cmd_history(args) -> int:
    ws = ws_from_args(args)
    idx = open_index(ws)
    try:
        if args.action == "stats":
            s = idx.defect_stats()
            total = idx.stats()["defects"]
            out(f"历史缺陷共 {total} 条。")
            for key, title in (("module", "按模块"), ("severity", "按严重程度"), ("status", "按状态")):
                out(f"{title}：" + "；".join(f"{k} {n}" for k, n in s[key]))
            return 0
        hits = idx.search(" ".join(args.words), ("defect",), k=args.n * (25 if args.module else 1), emb=ws.cfg.get("embedding"))
    finally:
        idx.close()
    if args.module:
        hits = [h for h in hits if args.module in h["text"].split("\n")[0]]
    hits = hits[:args.n]
    if not hits:
        out("没找到相关的历史缺陷。")
        return 1
    show_hits(hits, args.chars, " ".join(args.words))
    return 0


def cmd_embed(args) -> int:
    ws = ws_from_args(args)
    if args.action == "off":
        ws.cfg.pop("embedding", None)
        ws.save_cfg()
        out("已关闭向量检索，只用关键词检索。")
        return 0
    if args.action == "set":
        cfg = {"provider": args.provider, "model": args.model or ("bge-m3" if args.provider == "ollama" else "")}
        if args.url:
            cfg["base_url"] = args.url
        if args.key_env:
            cfg["api_key_env"] = args.key_env
        if args.provider == "openai" and not args.url:
            out("OpenAI 兼容的向量接口要写 --url（例如 https://api.openai.com/v1）")
            return 1
        ws.cfg["embedding"] = cfg
        ws.save_cfg()
        out(f"向量模型已设为 {cfg['provider']} / {cfg['model']}。试一下：{qa_cmd()} embed test；然后 {qa_cmd()} index 补算向量。")
        return 0
    emb = ws.cfg.get("embedding")
    if not emb:
        out("没配向量模型。本地：qa.py embed set ollama --model bge-m3（先装 Ollama 并运行 ollama pull bge-m3）")
        return 1 if args.action == "test" else 0
    if args.action == "show":
        out(f"向量模型：{json.dumps(emb, ensure_ascii=False)}")
        return 0
    t0 = time.monotonic()
    try:
        v = qa_index.embed(emb, ["营业款翻倍封顶是多少"])[0]
    except qa_index.IndexError_ as e:
        out(f"试调失败：{e}")
        return 1
    out(f"向量模型可用：{emb['provider']} / {emb.get('model')}，维度 {len(v)}，用时 {time.monotonic() - t0:.2f} 秒。")
    return 0


def in_req_dir(path: str, req_dir: Path | str | None) -> bool:
    """资料索引里的这个文件是不是某个需求文件夹里的。按文件夹名比（R001-营业款……各不相同），
    不按完整路径：工作区搬家或复制后，索引里记的还是老路径，按完整路径会漏掉（真跑时在副本里漏过，模型看到了这个需求的旧答案）。"""
    if not req_dir:
        return False
    # 反斜杠和斜杠都当分隔：索引是在 Windows 上建的、换到 Linux 服务器上跑（或反过来）也认得出
    name = str(req_dir).replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    parts = path.replace("\\", "/").split("/")
    return len(parts) >= 2 and parts[-2] == name


def retrieved_context(ws: Ws, query: str, budget: int = 40000, exclude: Path | None = None) -> str:
    """直接调模型生成测试点时，从资料索引里找相关的策划段落（含以前做过的需求）、历史缺陷和配置表，放进提示词。
    exclude：这次需求自己的文件夹，它的需求原文已经在提示词里了，不重复放。"""
    from .wiki import annotate_obsolete  # noqa: E402  上层模块，在函数里导入免得循环
    idx = open_index(ws, must=False)
    if idx is None:
        return "（这个工作区没有建资料索引）"
    emb = ws.cfg.get("embedding")
    try:
        docs = annotate_obsolete(ws, [h for h in idx.search(query, ("doc",), k=16, emb=emb) if not in_req_dir(h["path"], exclude)][:12])
        bugs = idx.search(query, ("defect",), k=8, emb=emb)
        cfgs = idx.search(query, ("config",), k=5, emb=emb)
        tables = [idx.table(h["title"]) for h in cfgs]
    finally:
        idx.close()
    parts, used = [], 0
    for title, hits in (("相关策划段落", docs), ("同类历史缺陷", bugs)):
        if not hits:
            continue
        parts.append(f"### {title}")
        for h in hits:
            block = f"- 【{Path(h['path']).name} · {h['title']}】\n{h['text'].strip()[:1500]}"
            if used + len(block) > budget:
                break
            parts.append(block)
            used += len(block)
    if tables:
        parts.append("### 可能相关的配置表（具体数值以表为准）")
        parts += [f"- {t['name']}（{t['nrows']} 行）字段：{'、'.join(t['columns'][:25])}" for t in tables if t]
    return "\n".join(parts) or "（资料索引里没找到相关内容）"
