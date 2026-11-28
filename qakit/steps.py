"""每一步都能直接调模型：网页、钩子、Jenkins 触发的时候旁边没有 AI 工具，全靠框架自己干。

一步的过程：
1. 拼资料：按步骤把要用的东西放进提示词（需求、知识库相关页、检索到的原文、配置表、现有文件……）
2. 边查边写：支持工具的模型（Claude、OpenAI 兼容接口）可以调只读的查资料工具（检索、找表查表、查缺陷、读文件）
3. 收结果：单个文件的步骤直接收正文；要改多个文件的步骤按“文件块”格式收，只许写这一步该写的文件
4. 检查：测试点能不能解析、知识库格式检查有没有新问题、用例代码能不能被 pytest 收集……
   检查不过就把问题发回去让模型改（最多 repair_rounds 轮）
5. 出错重试：限流、断网这类临时故障等一会儿重试；还不行换 stage_models 里配的下一个模型

人工关口不变：结果都停在待人审的状态——知识库改动不自动提交、测试点和需求变更要人审、用例清单要人确认。
"""
from __future__ import annotations

import contextlib
import fnmatch
import io
import json
import os
import py_compile
import re
import sys
import time
from pathlib import Path

import qa_models

from .common import PART, QaError, fit_parts, now_iso, out, qa_cmd, read, render, run, stamp, strip_fence, sub_env, today, write
from .workspace import Ws, run_hook, ws_from_args
from .corpus import LINK_PAGE, all_links, in_req_dir, index_path, link_page_rows, open_index, retrieved_context
from .wiki import coupling_gaps, coupling_text, module_catalog, wiki_check
from .reqs import make_review, parse_points, pending_change
from .prompts import PROMPTS, StepSkip, prompt_values
from .gen import build_testpoints_prompt, build_triage_prompt, log_call, usage_line, wiki_context
from .ingest import redact_text

GEN_DEFAULTS = {"retries": 2, "retry_wait": 5, "repair_rounds": 2, "max_tool_turns": 10, "tool_chars": 12000, "step_timeout": 1800}
WIKI_ALLOW, WIKI_DENY = ["wiki/*.md"], ["wiki/log.md"]
STEPS = {
    # mode：single = 模型输出就是那一个文件的正文；files = 按文件块输出，可以改多个文件
    "testpoints": {"mode": "single", "title": "生成测试点"},
    "triage": {"mode": "single", "title": "失败归因"},
    "wiki-lint": {"mode": "single", "title": "知识库内容检查"},
    "wiki-build": {"mode": "files", "title": "整理知识库", "allow": WIKI_ALLOW, "deny": WIKI_DENY},
    "wiki-update": {"mode": "files", "title": "按资料变化更新知识库", "allow": WIKI_ALLOW, "deny": WIKI_DENY},
    # may_be_empty：模型可以判断“没有要改的”，只写说明不输出文件
    "wiki-merge": {"mode": "files", "title": "定稿需求的新规则并进知识库", "allow": WIKI_ALLOW, "deny": WIKI_DENY, "may_be_empty": True},
    "feedback": {"mode": "files", "title": "漏测回流进知识库", "allow": WIKI_ALLOW, "deny": WIKI_DENY},
    "req-change": {"mode": "files", "title": "需求变更：测试点变更和回归", "allow": lambda v: [f"{v['change_dir']}/测试点变更.md"]},
    "cfg-check": {"mode": "files", "title": "配置出来后校对测试点和用例", "allow": lambda v: [f"{v['req_dir']}/最终测试点.md", "autotest/*.py"],
                  "deny": ["autotest/conftest.py"]},
    "tests-plan": {"mode": "files", "title": "设计接口用例清单", "allow": ["autotest/用例清单.md", "autotest/组合因素.md"]},
    "tests-write": {"mode": "files", "title": "写接口用例代码", "allow": lambda v: [f"{v['tests_dir']}/test_*.py", "autotest/invariants.py"]},
}
assert set(STEPS) == set(PROMPTS)

TOOLS = [
    {"name": "search", "description": "在资料索引里检索：策划文档、以前做过的需求和最终测试点（kind=doc，默认）、配置表（config）、历史缺陷（defect）。"
                                      "返回最相关的段落；标着“已过时”的句子不要用。",
     "schema": {"type": "object", "properties": {"query": {"type": "string", "description": "关键词或一句话"},
                                                 "kind": {"type": "string", "enum": ["doc", "config", "defect", "all"]}}, "required": ["query"]}},
    {"name": "cfg_find", "description": "按意思找配置表：返回表名、字段和表的注释。",
     "schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"name": "cfg_show", "description": "精确查配置表的行。where 每条写成 字段=值；表大时一定要加条件。"
                                        "结尾会列出框架按取值自动认出的关联（这张表哪个字段对应哪张表的哪个字段，高 = 值全对上、名字也对得上，中 = 要核对）；"
                                        "expand=1 把这些行引用到的其他表的行一起带出来（只顺着“高”的），2 再往下一层。",
     "schema": {"type": "object", "properties": {"table": {"type": "string", "description": "表名，用 cfg_find 查到的"},
                                                 "where": {"type": "array", "items": {"type": "string"}},
                                                 "cols": {"type": "string", "description": "只要这些字段，逗号分隔"},
                                                 "limit": {"type": "integer"},
                                                 "expand": {"type": "integer", "description": "连带查关联表的层数，0～2"}}, "required": ["table"]}},
    {"name": "cfg_links", "description": "列出一张配置表和别的表的关联（引用了谁、被谁引用，带可信度和依据）。整理“配置在哪些表、表之间怎么关联”时先查这个。",
     "schema": {"type": "object", "properties": {"table": {"type": "string"}}, "required": ["table"]}},
    {"name": "history_find", "description": "查历史缺陷（缺陷系统导出的和本工作区登记的）。",
     "schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"name": "read_doc", "description": "按文件名读资料索引里某份文档的全文（Word、PDF、网页都已经转成文字）。",
     "schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    {"name": "read_file", "description": "读工作区里的文件：知识库页（wiki/…）、需求和测试点（reqs/…）、用例代码和接口文档（autotest/…）。路径相对工作区。",
     "schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
]
# 完整代码权限（access source）时才给：只读，限在 qa.config.json 的 source_paths 里
SOURCE_TOOLS = [
    {"name": "source_grep", "description": "在被测项目的代码里搜关键词（不分大小写），返回“文件:行号: 那一行”。"
                                           "核对规则、找某个配置字段在代码里怎么用（比如搜字段名 unlockMain、表名 legend）时用。",
     "schema": {"type": "object", "properties": {"query": {"type": "string"},
                                                 "glob": {"type": "string", "description": "只搜这些文件，比如 *.ts、cloud/**/*.ts"}},
                "required": ["query"]}},
    {"name": "source_read", "description": "读被测项目的代码文件（路径照 source_grep 返回的写），可以只读一段行。",
     "schema": {"type": "object", "properties": {"path": {"type": "string"}, "start": {"type": "integer", "description": "从第几行开始，默认 1"},
                                                 "lines": {"type": "integer", "description": "读多少行，默认 200，最多 400"}},
                "required": ["path"]}},
]
CODE_EXT = {".ts", ".tsx", ".js", ".mjs", ".cjs", ".py", ".cs", ".lua", ".java", ".kt", ".go", ".cpp", ".cc", ".c", ".h", ".hpp",
            ".php", ".rb", ".rs", ".swift", ".m", ".gd", ".json", ".yaml", ".yml", ".xml", ".proto", ".sql", ".md"}
CODE_SKIP = {".git", ".svn", "node_modules", "__pycache__", ".qa", "dist", "build", "library", "temp", "out", ".next", "coverage",
             "venv", ".venv", "bin", "obj", "Library", "Temp", "Logs"}
# 代码内容会发给模型：隐藏文件（.env 等）和像密钥的文件不读，读出来的内容再脱敏一遍
SECRET_NAME = re.compile(r"(^|[._-])(secrets?|credentials?|passwords?|passwd|private[_-]?key|tokens?)([._-]|$)"
                         r"|\.(pem|key|p12|pfx|keystore|jks)$|^id_rsa", re.I)
BLOCK_RE = re.compile(r"^=====\s*文件[：:]\s*(.+?)\s*=====[ \t]*\n(.*?)\n=====\s*文件结束\s*=====[ \t]*$", re.M | re.S)


def gen_cfg(ws: Ws) -> dict:
    return {**GEN_DEFAULTS, **(ws.cfg.get("gen") or {})}


def clip(text: str, n: int) -> str:
    text = text.strip()
    return text if len(text) <= n else text[:n] + f"\n…（后面还有 {len(text) - n} 字没放进来，需要时用 read_file / read_doc 读全文）"


def model_limits(models: list[tuple[str, dict]], g: dict) -> dict:
    """模型清单里可以按模型写 max_input_chars（提示词最多多少字）、tool_chars、max_tool_turns：本地小模型上下文短，要比云端模型给得少。"""
    lim = {"max_input_chars": min((int(m["max_input_chars"]) for _, m in models if m.get("max_input_chars")), default=0)}
    for k in ("tool_chars", "max_tool_turns"):
        lim[k] = min([int(g[k])] + [int(m[k]) for _, m in models if m.get(k)])
    return lim


def file_section(ws: Ws, paths: list[Path], budget: int, each: int = 30000) -> str:
    parts, used = [], 0
    for p in paths:
        if not p.exists() or not p.is_file():
            continue
        block = f"===== {ws.rel(p)} =====\n{clip(read(p), each)}\n"
        if used + len(block) > budget:
            parts.append(f"（还有 {ws.rel(p)} 等文件没放进来，需要时用 read_file 读）")
            break
        parts.append(block)
        used += len(block)
    return "\n".join(parts) or "（没有）"


# ───────────────────────── 选模型 ─────────────────────────

def pick_models(ws: Ws, arg: str | None, step: str) -> list[tuple[str, dict]]:
    """--model 或 stage_models.<步骤>（没有就用 stage_models.default）；可以写多个，前一个失败就用下一个。"""
    reg = qa_models.load_registry(ws.cfg)
    sm = ws.cfg.get("stage_models") or {}
    names = arg or sm.get(step) or sm.get("default")
    if isinstance(names, str):
        names = [x.strip() for x in names.replace("，", ",").split(",") if x.strip()]
    if not names:
        raise QaError(f"没指定模型：加 --model <名字>，或在 qa.config.json 的 stage_models.{step} 里配"
                      f"（可以写成列表，前一个失败就用下一个）。可选：{', '.join(reg)}")
    missing = [n for n in names if n not in reg]
    if missing:
        raise QaError(f"模型清单里没有：{', '.join(missing)}。可选：{', '.join(reg)}（清单在 models.local.json，格式见 models.example.json）")
    return [(n, reg[n]) for n in names]


# ───────────────────────── 查资料工具 ─────────────────────────

def source_roots(ws: Ws) -> list[Path]:
    if ws.access != "source":
        return []
    return [Path(p).resolve() for p in ws.cfg.get("source_paths") or [] if Path(p).is_dir()]


def tool_list(ws: Ws) -> list[dict]:
    tools = TOOLS if index_path(ws).exists() else [t for t in TOOLS if t["name"] == "read_file"]
    return tools + (SOURCE_TOOLS if source_roots(ws) else [])


def code_files(roots: list[Path], glob: str = ""):
    """被测代码里要搜的文件：常见代码和配置后缀，跳过依赖、构建产物和 512KB 以上的大文件。"""
    for root in roots:
        # os.walk 默认不进链接文件夹：代码目录里有指回上层的链接（或 Windows 目录联接）时不会绕圈
        for d, subdirs, files in os.walk(root):
            subdirs[:] = sorted(s for s in subdirs if s not in CODE_SKIP and not s.startswith("."))
            for f in sorted(files):
                p = Path(d) / f
                if p.suffix.lower() not in CODE_EXT or f.startswith(".") or SECRET_NAME.search(f):
                    continue
                rel = f"{root.name}/{p.relative_to(root).as_posix()}"
                if glob and not (fnmatch.fnmatch(f, glob) or fnmatch.fnmatch(rel, glob) or fnmatch.fnmatch(rel, f"{root.name}/{glob}")):
                    continue
                try:
                    if p.stat().st_size <= 512 * 1024:
                        yield rel, p
                except OSError:
                    continue


def source_grep(roots: list[Path], query: str, glob: str = "", max_hits: int = 60) -> str:
    q = query.strip().lower()
    if not q:
        return "要写关键词。"
    hits, files, scanned = [], set(), 0
    for rel, p in code_files(roots, glob):
        scanned += 1
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if q not in text.lower():
            continue
        for no, line in enumerate(text.splitlines(), 1):
            if q in line.lower():
                hits.append(f"{rel}:{no}: {line.strip()[:200]}")
                files.add(rel)
                if len(hits) >= max_hits:
                    break
        if len(hits) >= max_hits:
            hits.append(f"……只列前 {max_hits} 处；加 glob 缩小范围（比如 *.ts）")
            break
    if not hits:
        return f"代码里没找到“{query}”（搜了 {scanned} 个文件）。换个写法，或者搜字段名、函数名。"
    return f"在 {len(files)} 个文件里找到：\n" + "\n".join(hits)


def source_read(roots: list[Path], path: str, start: int = 1, lines: int = 200) -> str:
    rel = path.replace("\\", "/").strip().lstrip("/")
    p = None
    for root in roots:  # 路径可以带代码文件夹名（source_grep 返回的写法），也可以不带
        for cand in (root.parent / rel, root / rel):
            c = cand.resolve()
            if (root == c or root in c.parents) and c.is_file():
                p = c
                break
        if p:
            break
    if p is None or ".." in Path(rel).parts:
        return f"代码里没有这个文件：{path}（只能读 source_paths 里的文件，路径照 source_grep 返回的写）"
    if any(x.startswith(".") for x in Path(rel).parts) or SECRET_NAME.search(p.name):
        return f"{path} 是隐藏文件或可能放密钥的文件，不给读。"
    all_lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    start, lines = max(1, int(start or 1)), max(1, min(int(lines or 200), 400))
    part = all_lines[start - 1:start - 1 + lines]
    head = f"{rel}（共 {len(all_lines)} 行，显示第 {start}～{start + len(part) - 1} 行）"
    return head + "\n" + "\n".join(f"{start + i:>5}  {x}" for i, x in enumerate(part))


def tool_runner(ws: Ws, limit: int, hide: str | None = None):
    """工具都是只读的：直接复用 search / cfg / history / index show 命令，输出给模型看。
    hide：出测试点时这个需求自己的文件夹名。它以前的测试点、定稿和审核表不给模型看，免得照抄旧答案（重新生成、bench 对比时尤其要紧）。"""
    from .cli import main  # noqa: E402  上层模块，在函数里导入免得循环

    roots = source_roots(ws)
    asked: dict[str, int] = {}
    hidden = f"这次就是在给 {hide} 出测试点：它以前的测试点和审核结果不给看（免得照抄），需求原文已经在提示词里了。"

    def cli(*argv: str) -> str:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            try:
                main(["--ws", str(ws.root), *argv])
            except SystemExit:  # 参数不对时 argparse 会退出
                pass
        return buf.getvalue()

    def run_tool(name: str, a: dict) -> str:
        key = name + json.dumps(a, ensure_ascii=False, sort_keys=True)
        asked[key] = asked.get(key, 0) + 1
        if asked[key] > 1:  # 同一个查询反复调：不再给一遍结果，提醒它换个查法或直接写，免得把查资料的轮数耗在原地
            return f"这个查询前面已经查过（第 {asked[key]} 次），结果见前面。换个关键词、换张表，或者用已经查到的资料直接写结果。"
        if name == "search":
            text = cli("search", str(a.get("query", "")), "--kind", a.get("kind") or "doc", "-n", "6", "--chars", "900",
                       *(["--exclude", hide] if hide else []))
        elif name == "cfg_find":
            text = cli("cfg", "find", str(a.get("query", "")), "-n", "5")
        elif name == "cfg_show":
            argv = ["cfg", "show", str(a.get("table", ""))]
            for w in a.get("where") or []:
                argv += ["--where", str(w)]
            if a.get("cols"):
                argv += ["--cols", str(a["cols"])]
            if a.get("expand"):
                argv += ["--expand", str(max(1, min(int(a["expand"]), 2)))]
            text = cli(*argv, "--limit", str(max(1, min(int(a.get("limit") or 30), 200))))
        elif name == "cfg_links":
            text = cli("cfg", "links", str(a.get("table", "")))
        elif name == "source_grep" and roots:
            text = redact_text(source_grep(roots, str(a.get("query", "")), str(a.get("glob") or "")))
        elif name == "source_read" and roots:
            text = redact_text(source_read(roots, str(a.get("path", "")), a.get("start") or 1, a.get("lines") or 200))
        elif name == "history_find":
            text = cli("history", "find", str(a.get("query", "")), "-n", "6")
        elif name == "read_doc":
            text = cli("index", "show", str(a.get("name", "")))
            m = re.match(r"(?:.*\n)?===== (.+?) =====", text)
            if hide and m and in_req_dir(m.group(1), hide):
                return hidden
        elif name == "read_file":
            rel = str(a.get("path", "")).replace("\\", "/").lstrip("/")
            p = (ws.root / rel).resolve()
            inside = p == ws.root or ws.root in p.parents
            if not inside or rel.startswith((".qa/", ".git/")) or ".." in Path(rel).parts:
                return "只能读工作区里的文件（不含 .qa/、.git/）。"
            if hide and hide in Path(rel).parts and p.name != "需求.md":
                return hidden
            if not p.exists():
                return f"没有这个文件：{rel}"
            if p.is_dir():
                return "这是文件夹，里面有：" + "、".join(sorted(x.name for x in p.iterdir())[:100])
            text = read(p)
        else:
            return f"没有这个工具：{name}"
        return clip(text or "（没有结果）", limit)
    return run_tool


# ───────────────────────── 每一步的资料 ─────────────────────────

def corpus_overview(ws: Ws, chars: int = 30000) -> str:
    """资料概况：有哪些文档（带一二级标题）、哪些配置表、缺陷系统里有哪些模块名。建模块清单时要用。"""
    idx = open_index(ws, must=False)
    if idx is None:
        return "（没有建资料索引）"
    try:
        heads: dict[str, list[str]] = {}
        for path, title in idx.con.execute("SELECT path, title FROM chunks WHERE kind='doc' ORDER BY id"):
            h = " > ".join(title.split(" > ")[1:3])
            lst = heads.setdefault(Path(path).name, [])
            if h and h not in lst and len(lst) < 12:
                lst.append(h)
        tables = [r[0] for r in idx.con.execute("SELECT name FROM tables ORDER BY name")]
        mods = idx.defect_stats()["module"] if idx.stats()["defects"] else []
    finally:
        idx.close()
    lines = ["### 文档（文件名：主要章节）"] + [f"- {n}：{'；'.join(h)}" for n, h in heads.items()]
    if tables:
        lines += ["", f"### 配置表（{len(tables)} 张）", "、".join(tables)]
    if mods:
        lines += ["", "### 缺陷系统里的模块名（缺陷条数）", "、".join(f"{k}（{n}）" for k, n in mods)]
    return clip("\n".join(lines), chars)


def config_links_context(ws: Ws, query: str, n: int = 6) -> str:
    """按模块名找最相关的几张配置表，列出字段和自动认出的关联（引用了谁、被谁引用）。具体数值让模型用 cfg_show 查。"""
    idx = open_index(ws, must=False)
    if idx is None:
        return "（没有建资料索引）"
    try:
        hits = idx.search(query, ("config",), k=n, emb=ws.cfg.get("embedding"))
        if not hits:
            return "（没找到相关的配置表：用 cfg_find 换几种说法再找）"
        links = all_links(ws, idx)
        lines = []
        for h in hits:
            t = idx.table(h["title"])
            if not t:
                continue
            lines.append(f"- {t['name']}（{t['nrows']} 行）字段：{'、'.join(t['columns'][:25])}{'…' if len(t['columns']) > 25 else ''}")
            for l in [l for l in links if l["from"] == t["name"]][:8]:
                kind = {"键值行": "（参数）", "字段名": "（字段名里的编号）"}.get(l.get("kind", ""), "")
                lines.append(f"  - 引用 [{l['level']}] {l['col']}{kind} → {l['to']}.{l['key']}：{l['why']}")
            for l in [l for l in links if l["to"] == t["name"]][:8]:
                lines.append(f"  - 被引用 [{l['level']}] {l['from']}.{l['col']} → {l['key']}")
        lines.append("（关联是框架按取值算的：A 表这一列的值全都能在 B 表的编号列里找到。高 = 名字也对得上；中 = 要核对。"
                     "编号写成表名、用行号当编号、跨系统的引用（存档字段、埋点）认不出来，要靠文档和代码补）")
        return "\n".join(lines)
    finally:
        idx.close()


def test_names(ws: Ws) -> str:
    names = []
    for p in sorted(ws.auto.rglob("test_*.py")):
        names += [f"{ws.rel(p)}::{m}" for m in re.findall(r"^\s*def (test_\w+)", read(p), re.M)]
    return "\n".join(f"- {n}" for n in names[:300]) or "（还没有用例）"


def build_context(ws: Ws, step: str, v: dict) -> str:
    """把这一步要用的资料放进提示词。模型能调工具时还可以自己再查。"""
    mods = [m for m in re.split(r"[、,，]", v.get("modules", "")) if m and "没标模块" not in m]
    if step in ("wiki-build", "wiki-update", "wiki-merge", "feedback", "wiki-lint"):
        if step == "wiki-build":
            query = " ".join(v.get("scope_modules") or []) or "模块"
            parts = [f"## 资料概况\n\n{corpus_overview(ws)}"]
            for m in (v.get("scope_modules") or [])[:5]:
                alias = next((x["aliases"] for x in module_catalog(ws) if x["name"] == m), [])
                parts.append(f"## 检索到的和“{m}”相关的资料\n\n{retrieved_context(ws, ' '.join([m] + alias), budget=25000)}")
                parts.append(f"## 可能和“{m}”有关的配置表，以及框架自动认出的表间关联\n\n{config_links_context(ws, ' '.join([m] + alias))}")
            raws = ws.raw_files()
            if raws and index_path(ws).exists():  # 建了索引的：原文靠上面的检索和工具查，这里只列文件名
                parts.append("## raw/ 里的原始资料（要看全文用 read_file）\n\n" + "\n".join(f"- {ws.rel(p)}" for p in raws))
            elif raws:
                parts.append(f"## raw/ 里的原始资料\n\n{file_section(ws, raws, 80000)}")
            wiki, _ = wiki_context(ws, query, v.get("scope_modules") or [], budget=60000)
            parts.append(f"## 现在的知识库（和这次范围相关的页）\n\n{wiki}")
            return PART.join(parts)
        if step == "wiki-update":
            parts = [f"## 变化的资料\n\n{v['changes']}"]
            raws = [ws.root / x for x in v.get("changed_raw") or []]
            if raws:
                parts.append(f"## raw/ 里新增和改动的资料全文\n\n{file_section(ws, raws, 60000)}")
            docs = v.get("changed_corpus") or []
            if docs:
                idx = open_index(ws, must=False)
                texts = []
                if idx is not None:
                    try:
                        for d in docs[:10]:
                            rows = idx.con.execute("SELECT title, text FROM chunks WHERE path=? ORDER BY id", (d,)).fetchall()
                            texts.append(f"===== {Path(d).name} =====\n" + clip("\n\n".join(f"【{t}】\n{x}" for t, x in rows), 20000))
                    finally:
                        idx.close()
                parts.append("## 资料文件夹里新增和改动的文档（索引里转好的文字）\n\n" + ("\n".join(texts) or "（读不到，用 read_doc 按文件名读）"))
            wiki, _ = wiki_context(ws, v["changes"], [])
            parts.append(f"## 现在的知识库\n\n{wiki}")
            return PART.join(parts)
        if step == "wiki-merge":
            d = ws.root / v["req_dir"]
            req_text = read(d / "需求.md")
            q = read(d / "测试点.md") if (d / "测试点.md").exists() else ""
            ask = q[q.find("需要和策划确认的问题"):] if "需要和策划确认的问题" in q else "（没有）"
            wiki, _ = wiki_context(ws, req_text, mods)
            return PART.join([f"## 需求原文\n\n{clip(req_text, 30000)}", f"## 最终测试点\n\n{clip(read(d / '最终测试点.md'), 40000)}",
                                f"## 出测试点时列的“需要和策划确认的问题”\n\n{clip(ask, 8000)}",
                                f"## 资料里可能被这个需求替代的旧规则（检索）\n\n{retrieved_context(ws, req_text[:3000], budget=20000, exclude=d)}",
                                f"## 现在的知识库\n\n{wiki}"])
        if step == "feedback":
            wiki, _ = wiki_context(ws, v["items"], [])
            return f"## 现在的知识库\n\n{wiki}"
        wiki, _ = wiki_context(ws, "", [])
        return f"## 现在的知识库\n\n{wiki}" + PART + "## raw/ 里的资料\n\n" + ("\n".join(f"- {ws.rel(p)}" for p in ws.raw_files()) or "（没有）")
    if step == "req-change":
        d, cdir = ws.root / v["req_dir"], ws.root / v["change_dir"]
        change = read(cdir / "需求改动.md")
        wiki, _ = wiki_context(ws, change, mods)
        return PART.join([f"## 需求改动（新旧对比）\n\n{clip(change, 30000)}", f"## 新的需求原文\n\n{clip(read(d / '需求.md'), 30000)}",
                            f"## 本需求的最终测试点\n\n{clip(read(d / '最终测试点.md'), 40000)}",
                            f"## 其他需求的最终测试点、相关资料、历史缺陷（检索）\n\n{retrieved_context(ws, change[:3000], budget=25000, exclude=d)}",
                            f"## 已有的自动化用例\n\n{test_names(ws)}", f"## 现在的知识库\n\n{wiki}"])
    if step == "cfg-check":
        d = ws.root / v["req_dir"]
        rid = v["req"]
        refs = list((ws.req_meta(rid).get("cfg_refs") or {}).keys())
        tables = []
        if refs:
            run_tool = tool_runner(ws, 15000)
            tables = [f"===== {t} =====\n{run_tool('cfg_show', {'table': t, 'limit': 60})}" for t in refs[:8]]
        pyfiles = sorted(p for p in ws.auto.rglob("*.py") if p.name != "conftest.py" and "runs" not in p.parts)
        return PART.join([f"## 最终测试点\n\n{clip(read(d / '最终测试点.md'), 40000)}",
                            "## 测试点引用的配置表（现在的数值）\n\n" + ("\n".join(tables) or "（测试点里没写表名：用 cfg_find 找）"),
                            f"## 现有的用例代码和不变量\n\n{file_section(ws, pyfiles, 50000, 15000)}"])
    if step in ("tests-plan", "tests-write"):
        api = file_section(ws, ws.api_files(v["target"]), 50000)
        tdir = ws.root / v["tests_dir"]
        if step == "tests-plan":
            final = ""
            if v.get("req_dir") and (ws.root / v["req_dir"] / "最终测试点.md").exists():
                final = f"## 最终测试点\n\n{clip(read(ws.root / v['req_dir'] / '最终测试点.md'), 40000)}" + PART
            wiki, _ = wiki_context(ws, final or "接口", mods)
            return final + PART.join([f"## 接口文档\n\n{api}", f"## 已有的用例\n\n{test_names(ws)}",
                                      f"## 和其他模块共用的数值\n\n{coupling_text(ws, [m for m in ws.modules()])}", f"## 知识库\n\n{wiki}"])
        existing = sorted(tdir.glob("test_*.py")) if tdir.exists() else []
        return PART.join([f"## 确认后的用例清单\n\n{clip(read(ws.auto / '用例清单.md'), 40000) if (ws.auto / '用例清单.md').exists() else '（没有 autotest/用例清单.md：先做 tests-plan）'}",
                            f"## 接口文档\n\n{api}", f"## autotest/conftest.py（框架提供的夹具）\n\n{clip(read(ws.auto / 'conftest.py'), 8000) if (ws.auto / 'conftest.py').exists() else '（没有）'}",
                            f"## 现有的用例和不变量\n\n{file_section(ws, existing + [ws.auto / 'invariants.py'], 40000, 12000)}"])
    return ""


# ───────────────────────── 收结果、检查 ─────────────────────────

def allowed(ws: Ws, spec: dict, v: dict) -> tuple[list[str], list[str]]:
    a = spec.get("allow") or []
    return (a(v) if callable(a) else list(a)), list(spec.get("deny") or [])


def parse_blocks(ws: Ws, text: str, allow: list[str], deny: list[str]) -> tuple[list[tuple[str, str]], list[str], str]:
    """从模型输出里拆出文件块。返回 (要写的文件, 问题, 文件块之外的说明)。"""
    blocks, problems = [], []
    for m in BLOCK_RE.finditer(text):
        rel = m.group(1).strip().strip("`").replace("\\", "/")
        while rel.startswith("./"):
            rel = rel[2:]
        rel = rel.lstrip("/")
        p = (ws.root / rel).resolve()
        if ".." in Path(rel).parts or not (ws.root in p.parents):
            problems.append(f"不能写工作区以外的文件：{rel}")
        elif any(fnmatch.fnmatch(rel, d) for d in deny) or not any(fnmatch.fnmatch(rel, a) for a in allow):
            problems.append(f"这一步不能写 {rel}（只能写：{'、'.join(allow)}" + (f"；不能写：{'、'.join(deny)}" if deny else "") + "）")
        else:
            blocks.append((rel, m.group(2).rstrip() + "\n"))
    if not blocks and not problems:
        problems.append("没找到文件块：每个文件要用“===== 文件：路径 =====”开头、“===== 文件结束 =====”结尾，中间写完整内容")
    note = re.sub(r"^#+\s*给人看的说明\s*\n?", "", BLOCK_RE.sub("", text).strip()).strip()
    return blocks, problems, note


def check_step(ws: Ws, step: str, v: dict, written: list[Path], before: dict) -> list[str]:
    """写完以后的检查；返回的问题会发回给模型改。"""
    probs = []
    if step == "testpoints":
        if not parse_points(written[0]):
            probs.append("没解析出测试点表格：表头要照模板（编号 | 测试点 | 风险等级 | 类型 | 依据 | 怎么验证），每行一条")
    elif step in ("wiki-build", "wiki-update", "wiki-merge", "feedback"):
        now, _ = wiki_check(ws)
        probs += [f"知识库格式检查：{p}" for p in now if p not in before.get("wiki", [])]
        old = before.get("links", {})
        for r in link_page_rows(ws):  # 关联的结论只有人能下：AI 新写或改动的行只能是“待确认”
            k = (r["from"], r["col"], r["to"])
            if r["verdict"] in ("确认", "否决") and old.get(k) != r["verdict"]:
                probs.append(f"wiki/{LINK_PAGE}：{r['from']}.{r['col']} → {r['to']} 的结论写成了“{r['verdict']}”；"
                             f"确认 / 否决只能由人审时改，你只能写“待确认”（人已经下过结论的行保持原样）")
    elif step == "req-change":
        rows = parse_points(written[0]) if written else []
        if not rows:
            probs.append("没解析出测试点变更表格：表头照抄任务说明里的那一行")
        for r in rows:
            if r["change"] not in ("新增", "修改", "作废", "回归"):
                probs.append(f"第 {r['no']} 条的“变更”只能是 新增 / 修改 / 作废 / 回归，现在是“{r['change']}”")
            elif r["change"] in ("修改", "作废") and not r["orig"]:
                probs.append(f"第 {r['no']} 条是{r['change']}，要写原编号（如 T03）")
    elif step == "cfg-check":
        final = ws.root / v["req_dir"] / "最终测试点.md"
        n = len(parse_points(final))
        if n < before.get("final_points", 0):
            probs.append(f"最终测试点从 {before['final_points']} 条变成了 {n} 条：校对只改数值，不能删测试点、也不能改坏表格")
    elif step == "tests-plan":
        if not (ws.auto / "用例清单.md").exists():
            probs.append("没写 autotest/用例清单.md")
    if step in ("cfg-check", "tests-write"):
        for p in written:
            if p.suffix == ".py":
                try:
                    py_compile.compile(str(p), doraise=True, cfile=str(ws.root / ".qa" / "pyc_check.pyc"))
                except py_compile.PyCompileError as e:
                    probs.append(f"{ws.rel(p)} 有语法错误：{str(e.msg)[-600:]}")
        if step == "tests-write" and not probs:
            r = run([sys.executable, "-m", "pytest", v["tests_dir"].split("/", 1)[1], "--collect-only", "-q", "-p", "no:cacheprovider"],
                    cwd=ws.auto, env=sub_env({"PYTHONDONTWRITEBYTECODE": "1", "QA_TARGET_URL": "http://127.0.0.1:9"}), timeout=300)
            if r.returncode not in (0, 5):  # 5 = 一个用例都没有
                probs.append("pytest 收集用例失败（导入错误、夹具名不对等）：\n" + (r.stdout + r.stderr).strip()[-3000:])
        (ws.root / ".qa" / "pyc_check.pyc").unlink(missing_ok=True)
    return probs


def output_rules(allow: list[str], deny: list[str]) -> str:
    return ("## 输出格式（框架按这个格式写文件，必须照做）\n\n"
            "要新建或修改的每个文件写一段，写**完整的文件内容**（不是只写改动的地方），没改的文件不要输出：\n\n"
            "===== 文件：wiki/modules/示例.md =====\n（这个文件的完整内容）\n===== 文件结束 =====\n\n"
            f"只能写这些文件：{'、'.join(allow)}" + (f"；不能写：{'、'.join(deny)}（框架自己记）" if deny else "") + "。\n"
            "全部文件写完后，可以再写一节“## 给人看的说明”：这次改了什么、拿不准的地方、需要人确认的问题。")


def build_prompt(ws: Ws, step: str, v: dict, tools_ok: bool, limit: int = 0) -> tuple[str, str]:
    """limit：这个模型提示词最多多少字（模型清单的 max_input_chars，本地小模型用）。超过时只压资料，任务说明、需求、输出要求不动。"""
    if step == "testpoints":
        system, user, skipped = build_testpoints_prompt(ws, v["req"], limit=max(0, limit - 400))
        if skipped:
            out(f"（知识库太大，没放进去：{'、'.join(skipped[:5])}）")
    elif step == "triage":
        system, user = build_triage_prompt(ws, v["run"])
    else:
        parts = build_context(ws, step, v).split(PART)
        vals = {**v, "context": ""}
        vals["wiki_rules"] = render(ws.prompt_text("api/wiki-rules.md"), vals).strip()
        system = render(ws.prompt_text("api/system.md"), vals).strip()
        spec = STEPS[step]
        rules = "\n\n" + output_rules(*allowed(ws, spec, v)) if spec["mode"] == "files" else ""
        if limit:
            fixed = len(system) + len(render(ws.prompt_text(f"api/{step}.md"), vals)) + len(rules) + 400
            raw = sum(len(p) for p in parts)
            parts = fit_parts(parts, limit - fixed)
            if sum(len(p) for p in parts) < raw:
                out(f"  资料 {raw} 字，这个模型装不下（max_input_chars {limit}），按部分压短到 {sum(len(p) for p in parts)} 字。")
        vals["context"] = "\n\n".join(parts)
        user = render(ws.prompt_text(f"api/{step}.md"), vals).strip() + rules
    system += f"\n\n（任务代号：{step}）"
    if tools_ok:
        system += ("\n\n资料不够时可以调用工具自己查（检索文档、找表查表、查历史缺陷、读工作区文件），查清楚再写；不要凭印象写数值和规则。"
                   "查完给出最终结果时，只输出要求的内容。")
    return system, user


# ───────────────────────── 调模型（重试、换模型、检查不过让模型改） ─────────────────────────

def call_models(ws: Ws, step: str, models: list[tuple[str, dict]], system: str, user: str, extra: dict,
                hide: str | None = None, deadline: float | None = None) -> tuple[str, qa_models.Result]:
    g = {**gen_cfg(ws), **{k: v for k, v in model_limits(models, gen_cfg(ws)).items() if k != "max_input_chars"}}
    tools = tool_list(ws)
    runner = tool_runner(ws, int(g["tool_chars"]), hide)
    last = (models[0][0], qa_models.Result(ok=False, error="没调用"))
    for i, (label, m) in enumerate(models):
        for attempt in range(int(g["retries"]) + 1):
            r = qa_models.call(m, system, user, cwd=ws.root, tools=tools, run_tool=runner, max_turns=int(g["max_tool_turns"]), deadline=deadline)
            log_call(ws, step, label, r, attempt=attempt + 1, **extra)
            last = (label, r)
            if r.ok:
                return last
            if not r.extra.get("transient") or attempt == int(g["retries"]) or (deadline and time.monotonic() >= deadline):
                break
            wait = float(g["retry_wait"]) * (2 ** attempt)
            out(f"  {label} 临时故障：{r.error[:200]}。{wait:g} 秒后重试（第 {attempt + 1} 次）……")
            time.sleep(wait)
        if i + 1 < len(models):
            out(f"  {label} 失败：{r.error[:300]}。换下一个模型 {models[i + 1][0]}。")
    return last


def write_single(ws: Ws, step: str, v: dict, label: str, r: qa_models.Result) -> Path:
    body = strip_fence(r.text).strip() + "\n"
    if step == "testpoints":
        p = ws.root / v["req_dir"] / "测试点.md"
        write(p, f"<!-- 生成：{label}（{r.model_id}），{now_iso()}，{usage_line(r)} -->\n\n" + body)
        ws.req_meta(v["req"])["model"] = label
        ws.save()
    elif step == "triage":
        p = ws.root / v["run_dir"] / "归因.md"
        write(p, f"<!-- 生成：{label}（{r.model_id}），{now_iso()}，{usage_line(r)}。要人确认 -->\n\n" + body)
    else:  # wiki-lint
        p = ws.root / "metrics" / f"知识库检查_{stamp()}.md"
        write(p, f"<!-- 生成：{label}（{r.model_id}），{now_iso()}。只列清单，改哪些由人定 -->\n\n" + body)
    return p


def run_step(ws: Ws, step: str, v: dict, models: list[tuple[str, dict]], args) -> tuple[bool, list[Path], list[str]]:
    """跑一步：返回 (检查是否通过, 写了哪些文件, 剩下的问题)。"""
    g = gen_cfg(ws)
    spec = STEPS[step]
    tools_ok = bool(tool_list(ws)) and any(m.get("provider") in qa_models.TOOL_PROVIDERS and not m.get("no_tools") for _, m in models) \
        and not getattr(args, "no_tools", False)
    if getattr(args, "no_tools", False):
        models = [(n, {**m, "no_tools": True}) for n, m in models]
    limit = model_limits(models, g)["max_input_chars"]
    system, user = build_prompt(ws, step, v, tools_ok, limit)
    rec = ws.root / ".qa" / "gen" / f"{stamp()}-{step}"
    gi = ws.root / ".gitignore"
    if ".qa/gen/" not in (read(gi) if gi.exists() else ""):  # 每次的提示词和输出留着查问题，但很大，不进 git
        write(gi, (read(gi).rstrip("\n") + "\n" if gi.exists() else "") + ".qa/gen/\n")
    write(rec / "提示词.md", f"# system\n\n{system}\n\n# user\n\n{user}\n")
    if getattr(args, "dry_run", False):
        n = len(system) + len(user)
        out(f"只拼提示词、没调模型：{n} 字（中文大约 {n * 2 // 3}～{n} token），" + ("模型还可以自己查资料。" if tools_ok else "不带查资料工具。") +
            f"\n提示词存在 {ws.rel(rec / '提示词.md')}")
        return True, [], []
    before = {"wiki": wiki_check(ws)[0] if step.startswith("wiki-") or step == "feedback" else [],
              "links": {(r["from"], r["col"], r["to"]): r["verdict"] for r in link_page_rows(ws)}}
    if step == "cfg-check":
        before["final_points"] = len(parse_points(ws.root / v["req_dir"] / "最终测试点.md"))
    extra = {"req": v.get("req")} if v.get("req") else ({"run": v["run_dir"]} if v.get("run_dir") else {})
    written: list[Path] = []
    probs: list[str] = []
    prompt = user
    hide = Path(v["req_dir"]).name if step == "testpoints" else None  # 出测试点时不给看这个需求以前的测试点和审核结果
    deadline = time.monotonic() + float(g["step_timeout"])  # 一步的总时限：查资料、返工、重试都算在里面
    for rnd in range(int(g["repair_rounds"]) + 1):
        if rnd and time.monotonic() >= deadline:
            out(f"  这一步已经用了 {int(float(g['step_timeout']))} 秒（gen.step_timeout），不再返工，剩下的问题留给人看。")
            break
        label, r = call_models(ws, step, models, system, prompt, {**extra, "round": rnd + 1}, hide, deadline)
        write(rec / f"输出_{rnd + 1}.md", r.text or f"（失败：{r.error}）")
        calls = r.extra.get("tool_calls") or []
        out(f"{'调用' if rnd == 0 else '让模型修改，调用'} {label}：" + (usage_line(r) if r.ok else f"失败：{r.error[:300]}") +
            (f"；查了 {len(calls)} 次资料（{'、'.join(sorted({c['name'] for c in calls}))}）" if calls else ""))
        if not r.ok:
            return False, written, [f"模型调用失败：{r.error}"]
        if r.extra.get("truncated"):
            out("  ⚠️ 输出被截断了：调大这个模型的 max_tokens，或把这一步拆小（比如 wiki-build --module 少几个模块）")
        if spec["mode"] == "single":
            written = [write_single(ws, step, v, label, r)]
            probs = check_step(ws, step, v, written, before)
            fix_text = r.text
        else:
            a, d = allowed(ws, spec, v)
            blocks, probs, note = parse_blocks(ws, r.text, a, d)
            if note:
                write(rec / "给人看的说明.md", note + "\n")
                out("模型的说明：" + clip(note, 1500).replace("\n", "\n  "))
            if not blocks and spec.get("may_be_empty") and note and all(p.startswith("没找到文件块") for p in probs):
                return True, written, []  # 模型判断没有要改的
            if blocks:
                for rel, body in blocks:
                    p = ws.root / rel
                    write(p, body)
                    if p not in written:
                        written.append(p)
                probs += check_step(ws, step, v, [ws.root / rel for rel, _ in blocks], before)
            fix_text = r.text
        if not probs:
            return True, written, []
        if rnd < int(g["repair_rounds"]):
            out(f"  检查发现 {len(probs)} 个问题，发回去让模型改：" + "；".join(x.splitlines()[0][:120] for x in probs[:3]))
            fix = ("\n\n## 你上一次的输出\n\n" + clip(fix_text, max(3000, limit // 3) if limit else 60000) + "\n\n## 框架检查发现的问题（改好后重新输出"
                   + ("完整的测试点" if spec["mode"] == "single" else "需要改的文件，每个都写完整内容") + "）\n\n" +
                   "\n".join(f"- {x}" for x in probs))
            base = user
            if limit and len(system) + len(user) + len(fix) > limit:  # 小模型装不下：资料再压短一些，给上一次的输出和问题腾地方
                base = build_prompt(ws, step, v, tools_ok, max(4000, limit - len(fix)))[1]
            prompt = base + fix
    return False, written, probs


# ───────────────────────── 每一步做完以后 ─────────────────────────

def after_step(ws: Ws, step: str, v: dict, ok: bool, written: list[Path], args) -> None:
    rels = [ws.rel(p) for p in written]
    if step == "testpoints" and written:
        pts = parse_points(written[0])
        out(f"已生成 {rels[0]}（{len(pts)} 条）。")
        for g in coupling_gaps(ws, v["req"], pts):
            out(f"  ⚠️ 耦合提醒：{g}")
        if ok and getattr(args, "review", False):
            d = ws.root / v["req_dir"]
            n = make_review(ws, written[0], d / "审核.md", d.name)
            out(f"审核表已生成：{ws.rel(d / '审核.md')}（{n} 条），等人审。")
            run_hook(ws, "after_review_ready", {"QA_REQ": v["req"], "QA_REVIEW": str(d / "审核.md")})
        else:
            out(f"下一步：{qa_cmd()} review {v['req']}，请人审核。")
    elif step == "triage" and written:
        out(f"已写 {rels[0]}。请人确认，真缺陷用 {qa_cmd()} defect new 登记。")
    elif step == "wiki-lint" and written:
        out(f"检查清单：{rels[0]}。改哪些由人定。")
    elif step in ("wiki-build", "wiki-update", "wiki-merge", "feedback") and written:
        log = ws.wiki / "log.md"
        line = f"- {today()} 直接调模型{STEPS[step]['title']}" + (f"（需求 {v['req']}）" if v.get("req") else "") + \
               f"：改了 {'、'.join(r.replace('wiki/', '') for r in rels)}（待人审）"
        write(log, (read(log).rstrip("\n") + "\n" if log.exists() else "# 知识库变更记录\n\n") + line + "\n")
        out(f"改了 {len(rels)} 页：{'、'.join(rels)}。知识库改动没有提交，请人审（打开 wiki/ 看，或 git diff）。")
        commit = {"wiki-merge": f' --merged {v.get("req")}', "feedback": f' --feedback {v.get("ids")}'}.get(step, "")
        out(f"人审通过后：{qa_cmd()} wiki commit -m \"{STEPS[step]['title']}：……\"{commit}")
    elif step == "wiki-merge" and ok and not written:
        out(f"模型判断需求 {v['req']} 没有要写进知识库的新规则（理由见上面的说明）。人同意后运行 {qa_cmd()} req merged {v['req']}")
    elif step == "req-change" and written and ok:
        ch = pending_change(ws, v["req"])
        cdir = ws.root / v["change_dir"]
        n = make_review(ws, written[0], cdir / "审核.md", f"{ws.req_dir(v['req']).name}（需求变更 {cdir.name.replace('变更_', '')}）")
        if ch:
            ch["status"] = "待审核"
            ws.save()
        out(f"测试点变更：{rels[0]}；变更审核表已生成：{ws.rel(cdir / '审核.md')}（{n} 条，只有变动和回归项）。人填完运行 {qa_cmd()} accept {v['req']}")
        run_hook(ws, "after_review_ready", {"QA_REQ": v["req"], "QA_REVIEW": str(cdir / "审核.md")})
    elif step == "cfg-check" and written:
        out(f"改了：{'、'.join(rels)}。人确认后运行 {qa_cmd()} req cfg-checked {v['req']}")
    elif step == "tests-plan" and written:
        fac = ws.auto / "组合因素.md"
        if fac.exists() and fac in written:
            from .wiki import cmd_combos  # noqa: E402
            with contextlib.redirect_stdout(io.StringIO()):
                cmd_combos(type("A", (), {"file": str(fac), "ws": str(ws.root)})())
            out(f"已按 {ws.rel(fac)} 生成两两组合：autotest/组合因素_两两组合.md")
        out(f"用例清单：{'、'.join(rels)}。请人审清单，确认后运行 {qa_cmd()} gen tests-write{v.get('target_opt', '')}")
    elif step == "tests-write" and written:
        out(f"写了：{'、'.join(rels)}。请人看断言，然后 {qa_cmd()} autotest run{v.get('target_opt', '')}（服务没在跑加 --start）")


def cmd_gen(args) -> int:
    ws = ws_from_args(args)
    step = args.what
    if step not in STEPS:
        out("没有这一步。可选：" + "、".join(STEPS))
        return 1
    try:
        v = prompt_values(ws, step, args)
    except StepSkip as e:
        out(str(e))
        return e.code
    models = pick_models(ws, getattr(args, "model", None), step)
    out(f"直接调模型：{STEPS[step]['title']}" + (f"（{v['req']}）" if v.get("req") else "") + f"，模型 {'、'.join(n for n, _ in models)}……")
    ok, written, probs = run_step(ws, step, v, models, args)
    if getattr(args, "dry_run", False):
        return 0
    after_step(ws, step, v, ok, written, args)
    if probs:
        out(f"⚠️ 还有 {len(probs)} 个问题没解决，要人看：")
        for p in probs:
            out("  · " + p.replace("\n", "\n    ")[:1500])
    run_hook(ws, "after_gen", {"QA_STEP": step, "QA_OK": int(ok), "QA_REQ": v.get("req") or "",
                               "QA_FILES": json.dumps([ws.rel(p) for p in written], ensure_ascii=False)})
    return 0 if ok else 1
