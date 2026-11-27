"""知识库：耦合关系表、两两组合、模块清单（统一模块名、推荐涉及模块）、过时资料、待确认、格式检查、提交。"""
from __future__ import annotations

import re
from pathlib import Path

import qa_index

from .common import QaError, bigrams, md_tables, now_iso, out, qa_cmd, read, set_field, sha256, split_names, today, write
from .workspace import find_ws, git_commit, run_hook, ws_from_args
from .corpus import corpus_doc_sigs, index_path, open_index

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .workspace import Ws


COUPLING = "耦合关系.md"


def coupling_rows(ws: Ws) -> list[dict]:
    """读 wiki/耦合关系.md：每行是一个被多个模块共用的数值或状态（价格、金币、次数、库存……）。"""
    p = ws.wiki / COUPLING
    if not p.exists():
        return []
    rows = []
    for table in md_tables(read(p)):
        head = table[0]
        if not any("共用" in h or "共享" in h for h in head):
            continue
        idx = {h: i for i, h in enumerate(head)}
        col = lambda r, key: next((r[i] for h, i in idx.items() if key in h and i < len(r)), "")
        for r in table[1:]:
            name = col(r, "共用") or col(r, "共享")
            if name:
                rows.append({"value": name, "writers": split_names(col(r, "改")), "readers": split_names(col(r, "读")),
                             "constraint": col(r, "约束"), "source": col(r, "来源"), "keywords": split_names(col(r, "关键词"))})
    return rows


def mentions(text: str, row: dict, others: list[str]) -> bool:
    """测试点有没有提到这个共用数值：名字（含括号里的别名）、关键词、相关模块名，或名字的字词大部分出现。"""
    parts = [p for p in re.split(r"[（）()、/\s]+", row["value"]) if len(p) >= 2]
    if any(p in text for p in parts + row.get("keywords", []) + others):
        return True
    grams = bigrams(re.sub(r"[（）()、/\s]", "", row["value"]))
    return bool(grams) and len(grams & bigrams(text)) / len(grams) >= 0.8


def coupled_for(ws: Ws, modules: list[str]) -> list[dict]:
    res = []
    for row in coupling_rows(ws):
        involved = set(row["writers"]) | set(row["readers"])
        if set(modules) & involved and len(involved) > 1:
            res.append({**row, "others": sorted(involved - set(modules))})
    return res


def coupling_text(ws: Ws, modules: list[str]) -> str:
    if not modules:
        return "（这个需求没标模块，框架查不了耦合关系表。请先按需求内容判断它涉及哪些模块，再读 wiki/耦合关系.md 找共用的数值。）"
    rows = coupled_for(ws, modules)
    if not rows:
        return (f"（耦合关系表里没有和 {'、'.join(modules)} 共用的数值。如果需求涉及金币、价格、次数、库存等会被别的模块改动的东西，"
                "在“需要和策划确认的问题”里提出来，并提醒用户补耦合关系表。）")
    return "\n".join(f"- 「{r['value']}」：会改它的模块 {'、'.join(r['writers']) or '?'}；会读它的模块 {'、'.join(r['readers']) or '?'}"
                     + (f"；约束：{r['constraint']}" if r["constraint"] else "") for r in rows)


def coupling_gaps(ws: Ws, rid: str, points: list[dict]) -> list[str]:
    """需求涉及的模块和别的模块共用某个数值（多个模块会改它、或有约束），测试点里却一条都没提到 → 提醒。"""
    mods = ws.req_meta(rid).get("modules") or []
    if not mods or not points:
        return []
    # 只有类型标成“耦合”的测试点（或审核时人工补充、没填类型的）才算考虑过叠加：普通测试点的“依据”也常引用别的模块页，算进来会漏报。
    # “怎么验证”一列常写“对账、日志”，会和模块名撞上，也不算
    eligible = [p for p in points if "耦合" in p.get("type", "") or not p.get("type", "").strip()]
    text = " ".join(" ".join(p.get(k, "") for k in ("point", "basis")) for p in eligible)
    gaps = []
    for r in coupled_for(ws, mods):
        if len(r["writers"]) < 2 and not r["constraint"]:
            continue
        if not mentions(text, r, r["others"]):
            gaps.append(f"和 {'、'.join(r['others'])} 共用「{r['value']}」" + (f"（约束：{r['constraint']}）" if r["constraint"] else "") + "，测试点里没提到")
    return gaps


def cmd_coupling(args) -> int:
    from .reqs import parse_points  # noqa: E402  上层模块，在函数里导入免得循环
    ws = ws_from_args(args)
    rows = coupling_rows(ws)
    if not rows:
        out(f"知识库里还没有耦合关系表（wiki/{COUPLING}）。生成或更新知识库时会让 AI 整理；格式见 {qa_cmd()} prompt wiki-build。")
        return 0
    if args.req:
        d = ws.req_dir(args.req)
        rid = d.name.split("-")[0]
        mods = ws.req_meta(rid).get("modules") or []
        out(f"需求 {d.name}（模块：{'、'.join(mods) or '没标'}）涉及的共用数值：")
        out(coupling_text(ws, mods))
        f = d / "最终测试点.md" if (d / "最终测试点.md").exists() else d / "测试点.md"
        if f.exists():
            gaps = coupling_gaps(ws, rid, parse_points(f))
            out("")
            out(f"对照 {f.name}：" + ("每个共用数值都有测试点提到。" if not gaps else ""))
            for g in gaps:
                out("  ⚠️ " + g)
        return 0
    out(f"耦合关系表（wiki/{COUPLING}），{len(rows)} 个共用数值：")
    for r in rows:
        out(f"  「{r['value']}」：改 {'、'.join(r['writers']) or '?'}；读 {'、'.join(r['readers']) or '?'}" + (f"；约束 {r['constraint']}" if r["constraint"] else ""))
    multi = [r["value"] for r in rows if len(r["writers"]) >= 2]
    if multi:
        out(f"\n被两个以上模块同时改的（最容易出叠加问题）：{'、'.join(multi)}")
    return 0


def pairwise(factors: list[tuple[str, list[str]]]) -> list[list[str]]:
    """两两组合（贪心）：任意两个因素的任意两个取值都至少同时出现一次，行数远少于全组合。"""
    n = len(factors)
    vals = [v for _, v in factors]
    if n == 1:
        return [[x] for x in vals[0]]
    uncovered = {(i, a, j, b) for i in range(n) for j in range(i + 1, n) for a in range(len(vals[i])) for b in range(len(vals[j]))}
    rows = []
    while uncovered:
        i, a, j, b = min(uncovered)
        row = [None] * n
        row[i], row[j] = a, b
        for k in range(n):
            if row[k] is not None:
                continue
            best, score = 0, -1
            for v in range(len(vals[k])):
                s = sum(((min(k, m), v if k < m else row[m], max(k, m), row[m] if k < m else v) in uncovered)
                        for m in range(n) if row[m] is not None)
                if s > score:
                    best, score = v, s
            row[k] = best
        for x in range(n):
            for y in range(x + 1, n):
                uncovered.discard((x, row[x], y, row[y]))
        rows.append([vals[k][row[k]] for k in range(n)])
    return rows


def cmd_combos(args) -> int:
    ws_root = find_ws(args)
    p = Path(args.file)
    if not p.is_absolute() and ws_root and not p.exists():
        p = ws_root / p
    if not p.exists():
        out(f"找不到 {args.file}。格式：一张两列的表 | 因素 | 取值 |，取值用顿号隔开，例如 | 券类型 | 无券、满减券、折扣券、叠加两张 |")
        return 1
    factors = []
    for table in md_tables(read(p)):
        for r in table[1:]:
            if len(r) >= 2 and r[0] and split_names(r[1]):
                factors.append((r[0], split_names(r[1])))
        if factors:
            break
    if len(factors) < 2:
        out("至少要两个因素。")
        return 1
    rows = pairwise(factors)
    full = 1
    for _, v in factors:
        full *= len(v)
    lines = [f"# 两两组合：{p.name}", "", f"{len(factors)} 个因素，全组合 {full} 种，两两组合 {len(rows)} 种（任意两个因素的任意两个取值都至少同时出现一次）。", "",
             "| 编号 | " + " | ".join(n for n, _ in factors) + " |", "|---|" + "---|" * len(factors)]
    lines += [f"| C{i:02d} | " + " | ".join(r) + " |" for i, r in enumerate(rows, 1)]
    text = "\n".join(lines) + "\n"
    dst = p.with_name(p.stem + "_两两组合.md")
    write(dst, text)
    out(text)
    out(f"已保存：{dst}")
    return 0


CATALOG = "模块清单.md"


def module_catalog(ws: Ws) -> list[dict]:
    """wiki/模块清单.md：| 模块 | 上级模块 | 别名 | 说明 |。
    “模块”是全项目统一的叫法（和 wiki/modules/ 的页名一致）；“上级模块”是缺陷系统里那种大模块；
    “别名”是各种叫法（缺陷系统里的模块名、口头叫法、代码目录名），顿号隔开。还没进清单的模块页也算进来。"""
    rows, seen = [], set()
    p = ws.wiki / CATALOG
    if p.exists():
        for table in md_tables(read(p)):
            head = [h.replace("*", "").strip() for h in table[0]]
            if "模块" not in head:
                continue
            i_name = head.index("模块")
            find = lambda key: next((i for i, h in enumerate(head) if key in h), None)
            i_parent, i_alias, i_desc = find("上级"), find("别名"), find("说明")
            cell = lambda r, i: r[i].strip() if i is not None and i < len(r) else ""
            for r in table[1:]:
                name = cell(r, i_name)
                if name and name not in seen:
                    seen.add(name)
                    rows.append({"name": name, "parent": cell(r, i_parent), "aliases": split_names(cell(r, i_alias)),
                                 "desc": cell(r, i_desc), "listed": True})
            break
    for m in ws.modules():
        if m not in seen:
            rows.append({"name": m, "parent": "", "aliases": [], "desc": "", "listed": False})
    return rows


def _key(s: str) -> str:
    return re.sub(r"[\s/_\-·]", "", s).lower()


def normalize_modules(ws: Ws, names: list[str]) -> tuple[list[str], list[tuple[str, list[str]]], list[str]]:
    """把各人写的模块名统一成清单里的标准名。返回 (标准名, [(大模块, 它下面的模块)], 对不上的名字)。
    写的是缺陷系统里的大模块时，不自动展开成全部子模块，而是交给推荐去挑具体是哪几个。"""
    cat = module_catalog(ws)
    lookup = {}
    for m in cat:
        for n in [m["name"]] + m["aliases"]:
            lookup.setdefault(_key(n), m["name"])
    parents = {}
    for m in cat:
        if m["parent"]:
            parents.setdefault(_key(m["parent"]), (m["parent"], []))[1].append(m["name"])
    mods, hints, unknown = [], [], []
    for n in names:
        k = _key(n)
        if k in lookup:
            if lookup[k] not in mods:
                mods.append(lookup[k])
        elif k in parents:
            hints.append(parents[k])
        else:
            unknown.append(n)
    return mods, hints, unknown


def suggest_modules(ws: Ws, text: str, top: int = 6) -> list[tuple[str, float, list[str]]]:
    """按需求内容推荐涉及哪些模块：需求里直接写到的模块名或别名、知识库模块页和需求的相似度、
    需求提到的共用数值（耦合表里改它、读它的模块）、检索到的同类历史缺陷所属的模块。"""
    cat = module_catalog(ws)
    names = {m["name"] for m in cat}
    score, why, wrote, read_ = {}, {}, {}, {}

    def add(mod: str, s: float, reason: str | None = None) -> None:
        score[mod] = score.get(mod, 0) + s
        if reason and reason not in why.setdefault(mod, []):
            why[mod].append(reason)

    for m in cat:
        hit = [n for n in [m["name"]] + m["aliases"] if len(n) >= 2 and n in text]
        if hit:
            add(m["name"], 3, f"需求里写到“{hit[0]}”")
    pages = {p.stem: bigrams(read(p)) for p in (ws.wiki / "modules").glob("*.md")} if (ws.wiki / "modules").exists() else {}
    if pages:
        q = bigrams(text)
        df = {}
        for g in pages.values():
            for b in g & q:
                df[b] = df.get(b, 0) + 1
        sims = {m: sum(1 / df[b] for b in g & q) for m, g in pages.items()}  # 越少模块用到的词越说明问题
        best = max(sims.values()) if sims else 0
        for m, s in sims.items():
            if best and s >= 0.6 * best:
                add(m, 2 * s / best, "和这个模块的知识库页内容最相近" if s == best else "和这个模块的知识库页内容相近")
    for row in coupling_rows(ws):
        words = [w for w in re.split(r"[（）()、/\s]+", row["value"]) + row["keywords"] if len(w) >= 2]
        hit = next((w for w in words if w in text), None)
        if hit:  # 需求提到某个共用数值：主要说明“改它的模块”涉及；一行涉及的模块越多，每个分得越少
            for mod in row["writers"]:
                add(mod, 1.2 / len(row["writers"]))
                wrote.setdefault(mod, []).append(hit)
            for mod in row["readers"]:
                add(mod, 0.3 / len(row["readers"]))
                read_.setdefault(mod, []).append(hit)
    for mod, ws_ in wrote.items():
        why.setdefault(mod, []).append(f"需求提到“{'”“'.join(dict.fromkeys(ws_))}”，耦合表里会改它")
    for mod, rs in read_.items():
        if mod not in wrote:
            why.setdefault(mod, []).append(f"需求提到“{'”“'.join(dict.fromkeys(rs))}”，耦合表里会读它")
    idx = open_index(ws, must=False)
    if idx is not None:
        try:
            hits = idx.search(text[:2000], ("defect",), k=20, emb=ws.cfg.get("embedding"))
        except Exception:  # noqa: BLE001  检索出问题不影响推荐其他来源
            hits = []
        finally:
            idx.close()
        counts = {}
        for h in hits:
            raw = h["text"].split("\n")[0].replace("模块：", "").split("　")[0].strip().strip("/")
            if raw:
                mods, hints, _ = normalize_modules(ws, [raw.split("/")[-1]])
                for mod in mods or ([raw] if not names else []):
                    counts[mod] = counts.get(mod, 0) + 1
        for mod, n in counts.items():
            add(mod, min(2.0, 0.4 * n), f"同类历史缺陷 {n} 条在这个模块")
    ranked = sorted(score.items(), key=lambda x: -x[1])
    return [(m, round(s, 2), why.get(m, [])) for m, s in ranked[:top] if s >= 1]


def print_module_suggestions(ws: Ws, rid: str) -> None:
    meta = ws.req_meta(rid)
    d = ws.req_dir(rid)
    sug = suggest_modules(ws, read(d / "需求.md"))
    hints = meta.get("module_hints") or []
    if hints:  # 写的是大模块：只在它下面的模块里挑
        kids = {k for _, ks in hints for k in ks}
        picked = [s for s in sug if s[0] in kids]
        sug = picked or [(k, 0, [f"属于“{p}”"]) for p, ks in hints for k in ks]
    meta["suggested_modules"] = [s[0] for s in sug]
    if not sug:
        out("  框架推荐不出来（知识库和模块清单还空、资料索引里也没检索到）：请按需求内容判断，或先建模块清单（prompt wiki-build）。")
        return
    out("  框架推荐的涉及模块（按把握从高到低）：")
    for m, s, why in sug:
        out(f"    · {m}：" + "；".join(why[:3]))
    out(f"  确认后：{qa_cmd()} req modules {rid} --set {','.join(s[0] for s in sug[:3])}（按实际改）")


OBSOLETE = "过时资料.md"


def obsolete_rows(ws: Ws) -> list[dict]:
    """wiki/过时资料.md：| 资料 | 章节 | 过时的原文 | 被什么替代 | 现行规则在哪 | 日期 |。
    “过时的原文”是那一句的原文片段：同一节里别的规则还在用，只标这一句；空着表示整节（章节也空 = 整份文档）作废。"""
    p = ws.wiki / OBSOLETE
    if not p.exists():
        return []
    for table in md_tables(read(p)):
        head = [h.replace("*", "").strip() for h in table[0]]
        if "资料" not in head:
            continue
        find = lambda key: next((i for i, h in enumerate(head) if key in h), None)
        cols = {"doc": head.index("资料"), "section": find("章节"), "text": find("原文"), "by": find("替代"), "where": find("现行")}
        rows = []
        for r in table[1:]:
            c = {k: (r[i].strip() if i is not None and i < len(r) else "") for k, i in cols.items()}
            c["doc"], c["text"] = c["doc"].strip("`"), c["text"].strip("“”\"'「」`")
            if c["doc"]:
                rows.append(c)
        return rows
    return []


def _doc_match(doc: str, path: str) -> bool:
    name = Path(path).name
    return name == doc or Path(path).stem == doc or path.replace("\\", "/").endswith(doc.replace("\\", "/"))


def _snippet_re(snippet: str) -> re.Pattern:
    chars = [re.escape(ch) for ch in re.sub(r"\s+", "", snippet)]
    return re.compile(r"\s*".join(chars))


def annotate_obsolete(ws: Ws, hits: list[dict], reorder: bool = True) -> list[dict]:
    """检索结果里，紧跟在登记的过时原文后面标“已过时”（同一段、同一行里别的规则照常显示、不降排名）；整节作废的标在开头并排到最后
    （reorder=False 时不挪位置，看整份文档时用）。"""
    rows = obsolete_rows(ws)
    if not rows:
        return hits
    keep, tail = [], []
    for h in hits:
        h, whole = dict(h), None
        for r in rows:
            if not _doc_match(r["doc"], h["path"]) or (r["section"] and r["section"] not in h["title"]):
                continue
            if not r["text"]:
                whole = r
                continue
            mark = f"【⚠️已过时：被 {r['by'] or '新规则'} 替代，现行规则见 {r['where'] or '知识库'}】"
            text = h["text"]
            for m in reversed(list(_snippet_re(r["text"]).finditer(text))):
                # 紧跟在登记的那段原文后面标：同一句、同一行表格里别的规则不受牵连
                text = text[:m.end()] + mark + text[m.end():]
            h["text"] = text
        if whole:
            h["text"] = (f"【⚠️这一{'节' if whole['section'] else '份资料'}已整体过时：被 {whole['by'] or '新规则'} 替代，"
                         f"现行规则见 {whole['where'] or '知识库'}；下面只作参考】\n" + h["text"])
            (tail if reorder else keep).append(h)
        else:
            keep.append(h)
    return keep + tail


def obsolete_problems(ws: Ws) -> list[str]:
    """过时登记对不上原文（文档后来改了、文件名写错）就报出来，免得标错地方或漏标。"""
    rows = obsolete_rows(ws)
    if not rows or not index_path(ws).exists():
        return []
    idx = open_index(ws)
    probs = []
    try:
        for r in rows:
            paths, _ = qa_index.file_chunks(idx, r["doc"])
            if not paths:
                if not (ws.raw / r["doc"]).exists():
                    probs.append(f"wiki/{OBSOLETE}：资料索引里没有“{r['doc']}”（文件名写错了，或资料被删了）")
                continue
            if not r["text"]:
                continue
            chunks = idx.con.execute("SELECT title, text FROM chunks WHERE path=?", (paths[0],)).fetchall()
            pat = _snippet_re(r["text"])
            if not any(pat.search(t) for title, t in chunks if not r["section"] or r["section"] in title):
                probs.append(f"wiki/{OBSOLETE}：在“{r['doc']}”{('的“' + r['section'] + '”') if r['section'] else ''}里找不到“{r['text'][:30]}”（文档可能改过了，更新登记）")
    finally:
        idx.close()
    return probs


def todo_items(ws: Ws) -> list[tuple[str, str]]:
    """知识库里真正待确认的问题：不算目录、变更记录和标题；同一个问题抄在几页里只算一次。"""
    seen, items = set(), []
    for p in ws.wiki_pages():
        rel = p.relative_to(ws.wiki).as_posix()
        if rel in ("index.md", "log.md"):
            continue
        for line in read(p).splitlines():
            s = line.strip()
            if "待确认" not in s or s.startswith("#"):
                continue
            key = re.sub(r"（[^）]*raw/[^）]*）|\([^)]*raw/[^)]*\)", "", s)  # 出处括号不同不算不同问题
            key = re.sub(r"待确认|[\s*`|>\-→:：。，,；;、（）()\[\]【】“”\"']", "", key)
            if len(key) < 4 or key[:40] in seen:
                continue
            seen.add(key[:40])
            items.append((rel, s))
    return items


def wiki_check(ws: Ws) -> tuple[list[str], dict]:
    problems = []
    index = ws.wiki / "index.md"
    pages = ws.wiki_pages()
    if not index.exists():
        problems.append("没有 wiki/index.md")
    index_text = read(index) if index.exists() else ""
    for p in pages:
        rel = p.relative_to(ws.wiki).as_posix()
        text = read(p)
        if rel in ("index.md", "log.md"):
            continue
        head = "\n".join(text.splitlines()[:15])
        if not re.search(r"^\s*[>*\-]*\s*\**来源\**[：:]", head, re.M):
            problems.append(f"wiki/{rel} 开头没写“来源：”")
        # 中文行文里资料名后面常跟（）、：、“” 等标点，这些都算分隔
        for ref in set(re.findall(r"raw/[^\s()（）\[\]【】、，,；;。:：`'\"“”‘’「」《》<>|]+", text)):
            ref = ref.rstrip(".")
            if not (ws.root / ref).exists():
                problems.append(f"wiki/{rel} 引用的 {ref} 不存在")
        if p.name not in index_text and rel not in index_text:
            problems.append(f"wiki/index.md 没列出 {rel}")
    if pages and not (ws.wiki / "log.md").exists():
        problems.append("没有 wiki/log.md（变更记录）")
    if len(ws.modules()) >= 2 and not coupling_rows(ws):
        problems.append(f"有 {len(ws.modules())} 个模块，但没有耦合关系表 wiki/{COUPLING}（或表头里没有“共用的数值”这一列）")
    cat = module_catalog(ws)
    if any(m["listed"] for m in cat):
        for m in cat:
            if not m["listed"]:
                problems.append(f"wiki/modules/{m['name']}.md 不在模块清单 wiki/{CATALOG} 里（模块名要和清单一致）")
        owner = {}
        for m in cat:
            for a in m["aliases"]:
                if _key(a) in owner and owner[_key(a)] != m["name"]:
                    problems.append(f"别名“{a}”同时写在 {owner[_key(a)]} 和 {m['name']} 两个模块下，框架分不清")
                owner.setdefault(_key(a), m["name"])
    problems += obsolete_problems(ws)
    return problems, {"pages": len(pages), "todo": len(todo_items(ws)), "coupling": len(coupling_rows(ws))}


def cmd_wiki(args) -> int:
    ws = ws_from_args(args)
    if args.action == "todo":
        items = todo_items(ws)
        out(f"知识库里有 {len(items)} 个待确认的问题（不算标题、目录、变更记录，重复的只算一次）：" if items else "知识库里没有待确认的问题。")
        for rel, text in items:
            out(f"  · wiki/{rel}：{text[:160]}")
        if items:
            out("能回答的告诉 AI，让它改进知识页、删掉“待确认”再提交；不是问题的状态说明（如“未真机验收”）不要标待确认。")
        return 0
    problems, stats = wiki_check(ws)
    if args.action == "check":
        out(f"知识库：{stats['pages']} 页，待确认的问题 {stats['todo']} 个（清单：qa.py wiki todo）。")
        if problems:
            out(f"有 {len(problems)} 个问题：")
            for p in problems:
                out("  · " + p)
        else:
            out("格式检查通过（每页有来源、引用的资料都在、目录齐全）。内容对不对要人审。")
        return 1 if problems else 0
    if problems and not args.force:
        out("知识库格式检查没通过，先修好（python qa.py wiki check 看明细），确实要提交就加 --force。")
        return 1
    if not args.message:
        out("要写提交说明：python qa.py wiki commit -m \"改了什么\"")
        return 1
    marked = mark_feedback(ws, args.feedback) if args.feedback else []
    merged = mark_merged(ws, args.merged) if getattr(args, "merged", None) else []
    snapshot = {ws.rel(p): sha256(p) for p in ws.raw_files()}
    entry = {"time": now_iso(), "message": args.message, "sources": snapshot, "feedback": marked, "merged": merged}
    if ws.cfg.get("corpora"):
        entry["corpus"] = corpus_doc_sigs(ws)  # 登记的资料文件夹也记一份，之后文档有变化就提醒更新知识库
    ws.state["wiki"]["commits"].append(entry)
    ws.save()
    committed = git_commit(ws, f"知识库：{args.message}")
    out(f"已提交知识库（{stats['pages']} 页，基于 {len(snapshot)} 份资料" + (f"、资料文件夹里 {len(entry['corpus'])} 份文档" if "corpus" in entry else "") + "）"
        + ("" if committed else "，没有文件改动，只记录了资料快照") +
        (f"；标记为已回流：{', '.join(marked)}" if marked else "") + (f"；标记为新规则已并入知识库：{', '.join(merged)}" if merged else "") + "。")
    run_hook(ws, "after_wiki_commit", {"QA_MESSAGE": args.message})
    return 0


def mark_feedback(ws: Ws, ids: str) -> list[str]:
    marked = []
    for raw_id in [x.strip().upper() for x in ids.split(",") if x.strip()]:
        if raw_id.startswith("D"):
            f = next((p for p in ws.defect_files() if p.name.split("-")[0].split(".")[0] == raw_id), None)
            if not f:
                raise QaError(f"没有缺陷 {raw_id}")
            write(f, set_field(read(f), "已回流", f"是（{today()}）"))
        elif raw_id.startswith("R"):
            ws.req_meta(ws.req_dir(raw_id).name.split("-")[0])["fed_back"] = today()
        else:
            raise QaError(f"看不懂 {raw_id}：缺陷写 D001，需求写 R001")
        marked.append(raw_id)
    return marked


def mark_merged(ws: Ws, ids: str) -> list[str]:
    """需求定稿后，新规则、新耦合已经并进知识库（或确认没有要并的）。"""
    marked = []
    for raw_id in [x.strip().upper() for x in ids.split(",") if x.strip()]:
        rid = ws.req_dir(raw_id).name.split("-")[0]
        if not (ws.req_dir(rid) / "最终测试点.md").exists():
            raise QaError(f"需求 {rid} 还没定稿（没有 最终测试点.md），定稿后再并入知识库")
        ws.req_meta(rid)["merged"] = today()
        marked.append(rid)
    return marked


def unmerged_reqs(ws: Ws) -> list[str]:
    """已经定稿、新规则还没并进知识库的需求。"""
    return [d.name.split("-")[0] for d in ws.req_dirs()
            if (d / "最终测试点.md").exists() and not ws.req_meta(d.name.split("-")[0]).get("merged")]
