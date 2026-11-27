"""环境权限、手工执行记录、抓包整理成接口文档、按代码改动推荐回归范围。"""
from __future__ import annotations

import re
from pathlib import Path

from .common import ACCESS, EXEC_RESULTS, KIT, md_tables, out, qa_cmd, read, render, run, stamp, write
from .workspace import Ws, ws_from_args
from .ingest import redact_text
from .reqs import parse_points


def cmd_access(args) -> int:
    ws = ws_from_args(args)
    if args.level:
        ws.cfg["access"] = args.level
        if args.source:
            ws.cfg["source_paths"] = [str(Path(p).resolve()) for p in args.source]
        ws.save_cfg()
        for fname in ("AGENTS.md", "CLAUDE.md", "GEMINI.md"):  # 工作区规则里写着环境说明，跟着更新
            write(ws.root / fname, render(read(KIT / "templates" / "workspace_AGENTS.md"), Ws(ws.root).values()))
        if not (ws.root / "README.md").exists():  # 老工作区补上给人看的说明（已有的不覆盖，可能被改过）
            write(ws.root / "README.md", render(read(KIT / "templates" / "workspace_README.md"), Ws(ws.root).values()))
        ws = Ws(ws.root)
    name, note = ACCESS[ws.access]
    out(f"环境：{name}（{ws.access}）。{note}")
    if ws.access == "source":
        out(f"代码位置：{ws.values()['source_paths']}" + ("" if ws.cfg.get("source_paths") else "，用 access source --source <代码文件夹> 登记"))
    return 0


def exec_rows(p: Path) -> tuple[list[dict], dict]:
    text = read(p)
    meta = {m.group(1): m.group(2).strip() for m in re.finditer(r"^(执行人|版本或环境|执行耗时（分钟）)[：:][ \t]*(.*)$", text, re.M)}
    rows = []
    for table in md_tables(text):
        if "结果" not in table[0]:
            continue
        idx = {h: i for i, h in enumerate(table[0])}
        cell = lambda r, k: r[idx[k]].strip() if k in idx and idx[k] < len(r) else ""
        rows = [{"no": cell(r, "编号"), "point": cell(r, "测试点"), "result": cell(r, "结果"), "defect": cell(r, "缺陷编号")} for r in table[1:]]
        break
    return rows, meta


def exec_problems(rows: list[dict]) -> list[str]:
    probs = []
    empty = [r["no"] for r in rows if r["result"] not in EXEC_RESULTS]
    if empty:
        probs.append(f"结果没填或填错（通过 / 失败 / 阻塞 / 未测）：{', '.join(empty[:10])}")
    nodef = [r["no"] for r in rows if r["result"] == "失败" and not re.search(r"D\d+", r["defect"].upper())]
    if nodef:
        probs.append(f"失败的要填缺陷编号（先 defect new 登记）：{', '.join(nodef[:10])}")
    return probs


def cmd_exec(args) -> int:
    ws = ws_from_args(args)
    d = ws.req_dir(args.req)
    final = d / "最终测试点.md"
    if not final.exists():
        out(f"需求还没定稿，先审核并 accept。")
        return 1
    st = stamp()[:8]
    dst = d / f"执行记录_{args.round or st}.md"
    if dst.exists() and not args.force:
        rows, _ = exec_rows(dst)
        probs = exec_problems(rows)
        cnt = {k: sum(r["result"] == k for r in rows) for k in EXEC_RESULTS}
        out(f"{dst.name}：" + "，".join(f"{k} {v}" for k, v in cnt.items()) + ("" if not probs else "；还没填完：" + "；".join(probs)))
        return 0 if not probs else 1
    pts = parse_points(final)
    lines = [f"# 执行记录：{d.name}（{args.round or st}）", "", "执行人：", "版本或环境：", "执行耗时（分钟）：", "",
             "结果填 通过 / 失败 / 阻塞 / 未测；失败的先 qa.py defect new 登记缺陷，把编号（D001）填进来。", "",
             "| 编号 | 测试点 | 结果 | 缺陷编号 | 备注 |", "|---|---|---|---|---|"]
    lines += [f"| {p['no']} | {p['point']} |  |  |  |" for p in pts]
    write(dst, "\n".join(lines) + "\n")
    out(f"执行清单已生成：{dst}（{len(pts)} 条）。逐条执行后填结果；填完再运行一次 {qa_cmd()} exec {d.name.split('-')[0]} 检查。")
    return 0


def exec_status(ws: Ws) -> list[tuple[str, Path, list[str], dict]]:
    res = []
    for d in ws.req_dirs():
        for p in sorted(d.glob("执行记录_*.md")):
            rows, _ = exec_rows(p)
            res.append((d.name.split("-")[0], p, exec_problems(rows), {k: sum(r["result"] == k for r in rows) for k in EXEC_RESULTS}))
    return res


def har_to_markdown(har: dict, name: str) -> str:
    """把抓包导出的 HAR 整理成接口文档：每个“方法 + 路径”一节，列请求参数和一个响应样例。"""
    from urllib.parse import urlparse
    groups = {}
    for e in har.get("log", {}).get("entries", []):
        req, resp = e.get("request", {}), e.get("response", {})
        u = urlparse(req.get("url", ""))
        key = (req.get("method", "GET"), u.path or "/")
        g = groups.setdefault(key, {"hosts": set(), "count": 0, "query": set(), "body": None, "resp": None, "status": set()})
        g["hosts"].add(u.netloc)
        g["count"] += 1
        g["query"].update(q.get("name", "") for q in req.get("queryString", []))
        g["status"].add(resp.get("status"))
        if g["body"] is None and (req.get("postData") or {}).get("text"):
            g["body"] = req["postData"]["text"][:1500]
        if g["resp"] is None and (resp.get("content") or {}).get("text"):
            g["resp"] = resp["content"]["text"][:1500]
    lines = [f"# 接口文档（从抓包整理）：{name}", "", "由 qa.py ingest --har 自动整理，只反映抓到的请求；字段含义、取值范围要人补充。", ""]
    for (method, path), g in sorted(groups.items(), key=lambda x: x[0][1]):
        lines += [f"## {method} {path}", "", f"- 主机：{'、'.join(sorted(h for h in g['hosts'] if h))}；抓到 {g['count']} 次；状态码 {'、'.join(str(s) for s in sorted(x for x in g['status'] if x is not None))}"]
        if g["query"]:
            lines.append(f"- 查询参数：{'、'.join(sorted(q for q in g['query'] if q))}")
        if g["body"]:
            lines += ["- 请求体样例：", "", "```", g["body"], "```"]
        if g["resp"]:
            lines += ["- 响应样例：", "", "```", g["resp"], "```"]
        lines.append("")
    return redact_text("\n".join(lines))


def cmd_impact(args) -> int:
    """完整代码权限时：取代码改动清单，交给 AI 对照知识库和耦合关系推荐回归范围。"""
    ws = ws_from_args(args)
    if ws.access != "source":
        out("按代码改动推荐回归范围需要完整代码权限：qa.py access source --source <代码文件夹>")
        return 1
    srcs = [Path(p) for p in ws.cfg.get("source_paths") or []]
    if not srcs:
        out("还没登记代码位置：qa.py access source --source <代码文件夹>")
        return 1
    rng = args.range or "HEAD~1..HEAD"
    parts = []
    for s in srcs:
        r = run(["git", "-c", "core.quotepath=false", "diff", "--stat", rng], cwd=s)
        names = run(["git", "-c", "core.quotepath=false", "diff", "--name-only", rng], cwd=s)
        if r.returncode != 0:
            out(f"{s}：git diff {rng} 失败：{(r.stderr or '').strip()[:200]}")
            return 1
        parts.append(f"### {s}（{rng}）\n\n改动的文件：\n" + "\n".join(f"- {x}" for x in names.stdout.split()) + "\n\n```\n" + r.stdout.strip()[-3000:] + "\n```")
    v = {**ws.values(), "range": rng, "diff": "\n\n".join(parts)}
    out("【以下是这一步的任务说明：在工作区里工作的 AI 照着做】")
    out("-" * 60)
    out(render(ws.prompt_text("impact.md"), v).strip())
    out("-" * 60)
    return 0
