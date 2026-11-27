"""大批资料的检索层：策划文档、配置表、历史缺陷。

真实项目的资料量很大（几百份策划案、几千张配置表、成千上万条缺陷），不能整份塞给模型，也不适合复制进工作区。
做法：资料留在原处（共享盘、SVN 目录都行），在工作区 .qa/index.sqlite 里建索引（这个文件不进 git）。

- 文档（kind=doc）：Word / Excel / Markdown / 网页 / PDF / 文本。按标题切成段落；
  关键词检索（BM25，中文按两字词组）+ 可选向量检索（本地 Ollama 或 OpenAI 兼容接口），两路分数加权合并
- 配置表（kind=config）：Excel / CSV / JSON。每张表一条“目录卡片”（表名、字段、注释、部分取值）用来找表；
  具体数值用 cfg show 精确读，不让模型去“搜”数值。JSON 按结构拆成多张表（见 json_tables）
- 历史缺陷（kind=defect）：禅道、TAPD、Jira、飞书、自建系统导出的 CSV / Excel，自动识别列名，也可以手动指定；
  工作区自己登记的缺陷记录（defects/D001-….md）也按缺陷建索引

只依赖 Python 标准库；读 Word 要 python-docx，读 Excel 要 openpyxl，读 PDF 要 pypdf（没装的格式会跳过并提示）。
"""
from __future__ import annotations

import csv
import hashlib
import html.parser
import io
import json
import math
import os
import re
import sqlite3
import struct
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from datetime import date, datetime
from heapq import nlargest
from pathlib import Path

DOC_EXT = {".docx", ".xlsx", ".xlsm", ".md", ".txt", ".html", ".htm", ".pdf", ".csv", ".json"}
CONFIG_EXT = {".xlsx", ".xlsm", ".csv", ".json"}
DEFECT_EXT = {".xlsx", ".xlsm", ".csv", ".md"}
SKIP_DIRS = {".git", ".svn", "node_modules", "__pycache__", ".qa"}
CHUNK_CHARS = 900
MAX_ROWS = 200_000

DEFECT_COLUMNS = {
    "id": ["编号", "id", "bug编号", "bugid", "缺陷编号", "缺陷id", "问题编号", "issuekey", "key", "issueid", "单号", "工作项id"],
    "title": ["标题", "bug标题", "缺陷标题", "摘要", "主题", "summary", "名称", "问题标题", "缺陷名称", "工作项标题"],
    "module": ["模块", "所属模块", "功能模块", "模块路径", "component/s", "components", "component", "分类", "所属分类", "业务模块"],
    "severity": ["严重程度", "严重等级", "severity", "缺陷等级", "等级", "优先级", "priority"],
    "status": ["状态", "status", "bug状态", "处理状态", "缺陷状态"],
    "desc": ["重现步骤", "复现步骤", "详细描述", "描述", "description", "步骤", "问题描述", "缺陷描述"],
    "created": ["创建日期", "创建时间", "提交时间", "created", "发现时间", "提出时间", "报告时间"],
    "version": ["影响版本", "版本", "affectsversion/s", "发现版本", "所属版本", "迭代"],
    "resolution": ["解决方案", "解决结果", "resolution", "处理结果"],
}
TYPE_WORDS = {"int", "string", "str", "float", "double", "bool", "boolean", "long", "list", "array", "json", "number", "text",
              "int32", "int64", "uint", "byte", "short", "map", "dict", "enum", "date", "datetime", "vector", "table"}


class IndexError_(Exception):
    pass


# ───────────────────────── 分词与工具 ─────────────────────────

_SEG = re.compile(r"[一-鿿]+|[A-Za-z][A-Za-z0-9_]*|\d+(?:\.\d+)?")


def tokens(text: str) -> list[str]:
    """中文按两字词组切，英文和数字按词，统一小写。"""
    out = []
    for seg in _SEG.findall(text or ""):
        if "一" <= seg[0] <= "鿿":
            out += [seg] if len(seg) == 1 else [seg[i:i + 2] for i in range(len(seg) - 1)]
        else:
            out.append(seg.lower())
    return out


def cell_str(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return str(v).strip()


def file_sig(p: Path) -> str:
    st = p.stat()
    return f"{st.st_size}-{st.st_mtime_ns}"


def norm_header(s: str) -> str:
    return re.sub(r"[\s_\-:：（）()\[\]【】]", "", str(s or "")).lower()


# ───────────────────────── 读各种文件 ─────────────────────────

def read_text_file(p: Path) -> str:
    raw = p.read_bytes()
    for enc in ("utf-8-sig", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def docx_to_md(p: Path) -> str:
    try:
        import docx  # python-docx
    except ImportError as e:
        raise IndexError_("读 Word 要安装 python-docx：pip install python-docx") from e
    from docx.table import Table
    from docx.text.paragraph import Paragraph
    d = docx.Document(str(p))
    lines = []
    for el in d.element.body.iterchildren():
        tag = el.tag.split("}")[-1]
        if tag == "p":
            para = Paragraph(el, d)
            t = para.text.strip()
            if not t:
                continue
            style = (para.style.name if para.style is not None else "") or ""
            m = re.search(r"(?:Heading|标题)\s*(\d)", style)
            lines.append(("#" * int(m.group(1)) + " " + t) if m else t)
        elif tag == "tbl":
            first = True
            for row in Table(el, d).rows:
                cells = []
                for c in row.cells:
                    txt = c.text.replace("\n", " ").strip()
                    if not cells or cells[-1] != txt:  # 合并单元格会重复出现
                        cells.append(txt)
                if any(cells):
                    lines.append("| " + " | ".join(cells) + " |")
                    if first:  # 第一行当表头，补上分隔行，Markdown 才认成表格
                        lines.append("|" + "---|" * len(cells))
                        first = False
    return "\n".join(lines)


def open_workbook(p: Path):
    try:
        import openpyxl
    except ImportError as e:
        raise IndexError_("读 Excel 要安装 openpyxl：pip install openpyxl") from e
    return openpyxl.load_workbook(str(p), read_only=True, data_only=True)


def sheet_rows(ws) -> list[list[str]]:
    rows = []
    for r in ws.iter_rows(values_only=True):
        cells = [cell_str(v) for v in r]
        while cells and not cells[-1]:
            cells.pop()
        rows.append(cells)
        if len(rows) >= MAX_ROWS:
            break
    while rows and not rows[-1]:
        rows.pop()
    return rows


def xlsx_to_md(p: Path) -> str:
    wb = open_workbook(p)
    out = []
    try:
        for ws in wb.worksheets:
            rows = [r for r in sheet_rows(ws) if any(r)]
            if not rows:
                continue
            out.append(f"# {ws.title}")
            out += ["| " + " | ".join(r) + " |" for r in rows]
    finally:
        wb.close()
    return "\n".join(out)


class _HtmlText(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts, self._skip, self._prefix = [], 0, ""

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif re.fullmatch(r"h[1-6]", tag):
            self._prefix = "#" * int(tag[1]) + " "
            self.parts.append("\n")
        elif tag in ("p", "div", "br", "li", "tr"):
            self.parts.append("\n")
        elif tag in ("td", "th"):
            self.parts.append(" | ")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip -= 1
        elif re.fullmatch(r"h[1-6]", tag):
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip and data.strip():
            self.parts.append(self._prefix + data.strip())
            self._prefix = ""


def html_to_md(p: Path) -> str:
    h = _HtmlText()
    h.feed(read_text_file(p))
    return re.sub(r"\n{3,}", "\n\n", "".join(h.parts))


def pdf_to_text(p: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as e:
        raise IndexError_("读 PDF 要安装 pypdf：pip install pypdf") from e
    return "\n".join((pg.extract_text() or "") for pg in PdfReader(str(p)).pages)


def doc_text(p: Path) -> str:
    ext = p.suffix.lower()
    if ext == ".docx":
        return docx_to_md(p)
    if ext in (".xlsx", ".xlsm"):
        return xlsx_to_md(p)
    if ext in (".html", ".htm"):
        return html_to_md(p)
    if ext == ".pdf":
        return pdf_to_text(p)
    return read_text_file(p)


# ───────────────────────── 切段 ─────────────────────────

def chunk_doc(text: str, title: str) -> list[tuple[str, str]]:
    """按标题切段：返回 (标题路径, 正文)。超长的段按段落再切；表格行按组切，每组带表头。"""
    chunks, path, buf, header_row = [], [], [], None

    def flush():
        body = "\n".join(buf).strip()
        if body:
            heading = " > ".join([title] + path)
            parts, cur = [], ""
            for line in body.split("\n"):
                if len(cur) + len(line) > CHUNK_CHARS and cur:
                    parts.append(cur)
                    cur = (header_row + "\n" if header_row and line.startswith("|") else "") + line
                else:
                    cur = f"{cur}\n{line}" if cur else line
            if cur:
                parts.append(cur)
            chunks.extend((heading, part) for part in parts)
        buf.clear()

    for line in text.splitlines():
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            flush()
            level = len(m.group(1))
            path[:] = path[:level - 1] + [m.group(2).strip()]
            header_row = None
            continue
        if line.startswith("|") and header_row is None:
            header_row = line
        buf.append(line)
    flush()
    return chunks


# ───────────────────────── 配置表 ─────────────────────────

def is_ident(s: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", s))


def parse_table(rows: list[list[str]]) -> tuple[list[str], list[list[str]], list[list[str]]]:
    """游戏配置表常见格式：一行字段名，可能再跟一行类型、一行中文注释。返回 (字段, 注释行, 数据行)。"""
    rows = [r for r in rows]
    width = max((len(r) for r in rows[:20]), default=0)
    h = next((i for i, r in enumerate(rows[:10]) if sum(1 for c in r if c and not re.fullmatch(r"-?\d+(\.\d+)?", c)) >= max(2, 0.5 * width)), 0)
    header = rows[h] if rows else []
    cols = [c or f"列{i + 1}" for i, c in enumerate(header + [""] * (width - len(header)))]
    notes, i = [], h + 1
    while i < len(rows) and len(notes) < 3:
        r = [c for c in rows[i] if c]
        if not r:
            i += 1
            continue
        lower = [c.lower() for c in r]
        typeish = sum(c in TYPE_WORDS for c in lower) >= 0.6 * len(r)
        chinese_comment = (sum(is_ident(c) for c in header if c) >= 0.6 * max(1, len([c for c in header if c]))
                           and sum(bool(re.search(r"[一-鿿]", c)) for c in r) >= 0.6 * len(r))
        if typeish or chinese_comment:
            notes.append(rows[i])
            i += 1
        else:
            break
    data = [r for r in rows[i:] if any(r)]
    return cols, notes, data


# 配置表里常见的英文字段词 → 中文，写进表的目录卡片，用中文找表（“价格”“解锁”）时也能命中英文字段名的表。
# 只放各类游戏通用的词；项目专有的（比如 rod = 鱼竿）由知识库记下“在哪张表”，或用 cfg list 按表名找
FIELD_WORDS = {
    "price": "价格", "cost": "花费", "unlock": "解锁", "level": "等级", "lv": "等级", "tier": "档位", "grade": "档位",
    "gold": "金币", "coin": "金币", "diamond": "钻石", "gem": "宝石", "exp": "经验", "reward": "奖励", "bonus": "加成",
    "rate": "比率", "ratio": "比率", "prob": "概率", "chance": "概率", "weight": "权重", "cap": "上限", "max": "上限",
    "min": "下限", "limit": "限制", "count": "数量", "num": "数量", "amount": "数量", "sec": "秒",
    "time": "时间", "duration": "时长", "day": "天", "daily": "每日", "week": "周", "buy": "购买", "sell": "出售",
    "shop": "商店", "store": "商店", "item": "道具", "name": "名称", "desc": "描述", "id": "编号", "type": "类型",
    "mult": "倍率", "free": "免费", "ad": "广告", "ads": "广告", "task": "任务", "quest": "任务", "stamina": "体力",
    "energy": "体力", "vip": "会员", "discount": "折扣", "coupon": "券", "gear": "装备", "equip": "装备", "skill": "技能",
    "hp": "生命", "atk": "攻击", "def": "防御", "speed": "速度", "rank": "排名", "score": "分数", "star": "星级",
}


def field_words(names: list[str]) -> list[str]:
    """把 unlockPondLv、free_per_day 这类字段名拆词，配上中文。"""
    out = []
    for n in names:
        for w in re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])", n):
            zh = FIELD_WORDS.get(w.lower())
            if zh and f"{w.lower()}={zh}" not in out:
                out.append(f"{w.lower()}={zh}")
    return out


def table_card(name: str, cols: list[str], notes: list[list[str]], data: list[list[str]]) -> str:
    note_txt = "；".join(" ".join(c for c in n if c) for n in notes)
    words = field_words([name] + cols)
    if words:
        note_txt = (note_txt + "；" if note_txt else "") + "字段词：" + "、".join(words)
    values, seen = [], set()
    for r in data:
        for c in r:
            if c and not re.fullmatch(r"-?\d+(\.\d+)?", c) and c not in seen and len(c) <= 40:
                seen.add(c)
                values.append(c)
                if len(values) >= 400:
                    break
        if len(values) >= 400:
            break
    return f"配置表 {name}\n字段：{'、'.join(cols)}\n注释：{note_txt}\n取值：{'、'.join(values)}"


def _flat(v, prefix: str, out: dict) -> None:
    """把一个值摊平成 {字段路径: 文本}：对象一层层展开（freePerDay.beta），数组写成 JSON 文本。"""
    if isinstance(v, dict) and v:
        for k, x in v.items():
            _flat(x, f"{prefix}.{k}" if prefix else str(k), out)
    else:
        out[prefix or "值"] = json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else cell_str(v)


def _is_records(x) -> bool:
    return isinstance(x, list) and bool(x) and sum(isinstance(i, dict) for i in x) >= max(1, len(x) / 2)


def json_tables(p: Path) -> list[tuple[str, list[str], list[list[str]]]]:
    """JSON 配置拆成表，表名 = 文件名/路径（例如 economy/gear.rod）：
    - 对象数组（gear.rod: [{tier, name, price}, …]）→ 一个对象一行
    - 子项都是结构相近的对象（species: {jiyu: {name, median}, liyu: {…}}）→ 一个子项一行，第一列“键”
    - 其余的单个数值 → “键 / 值”两列：顶层每一节一张表（economy/session），节里更深的对象摊平成
      a.b.c 这样的键，不再拆成小表；数组值写成 JSON 文本
    """
    data = json.loads(read_text_file(p))
    tables: list[tuple[str, list[str], list[list[str]]]] = []

    def name_of(path: str) -> str:
        return f"{p.stem}/{path}" if path else p.stem

    def add_records(path: str, items: list[tuple[str, object]], key_col: str | None) -> None:
        cols, rows = ([key_col] if key_col else []), []
        for key, item in items:
            flat = {key_col: key} if key_col else {}
            _flat(item, "", flat)
            cols += [c for c in flat if c not in cols]
            rows.append(flat)
        tables.append((name_of(path), cols, [[r.get(c, "") for c in cols] for r in rows]))

    def similar(objs: list[dict]) -> bool:
        keysets = [set(o) for o in objs]
        common = set.intersection(*keysets)
        return bool(common) and len(common) >= 0.3 * (sum(map(len, keysets)) / len(keysets))

    def map_kids(x: dict) -> dict:
        kids = {k: v for k, v in x.items() if isinstance(v, dict) and v}
        return kids if len(kids) >= 2 and len(kids) >= 0.6 * len(x) and similar(list(kids.values())) else {}

    def walk(x, path: str) -> None:
        if _is_records(x):
            add_records(path, [(str(i + 1), it) for i, it in enumerate(x)], None)
            return
        if not isinstance(x, dict):
            return
        pos, kv = len(tables), []

        def collect(obj: dict, prefix: str, objpath: str) -> None:
            for k, v in obj.items():
                key, sub = (f"{prefix}.{k}" if prefix else str(k)), (f"{objpath}.{k}" if objpath else str(k))
                if _is_records(v) or (isinstance(v, dict) and v and (map_kids(v) or not path)):
                    walk(v, sub)  # 对象数组、结构相近的子项单独成表；顶层每一节也单独成表
                elif isinstance(v, dict) and v:
                    collect(v, key, sub)
                else:
                    kv.append((key, v))

        kids = map_kids(x)
        if kids:
            add_records(path, list(kids.items()), "键")
        collect({k: v for k, v in x.items() if k not in kids}, "", path)
        if kv:
            tables.insert(pos, (name_of(path), ["键", "值"],
                                [[k, json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else cell_str(v)] for k, v in kv]))

    walk(data, "")
    return tables


def md_defect(text: str, stem: str) -> dict:
    """工作区自己登记的缺陷记录（templates/缺陷模板.md 的格式）：第一行标题，“- 字段：值”，“## 现象”等小节。"""
    lines = text.splitlines()
    title = (lines[0] if lines else stem).lstrip("# ").strip()
    f = {m.group(1).strip(): m.group(2).strip() for m in re.finditer(r"^[-*]\s*([^：:\n]{1,12})[：:][ \t]*(.*)$", text, re.M)}
    parts = []
    for sec in ("现象", "复现步骤", "原因", "以后怎么测"):
        m = re.search(rf"^##\s*{sec}\s*$(.*?)(?=^##\s|\Z)", text, re.M | re.S)
        if m and m.group(1).strip():
            parts.append(f"{sec}：{m.group(1).strip()}")
    did = stem.split("-")[0]
    return {"id": did, "title": re.sub(rf"^{re.escape(did)}\s*", "", title), "module": f.get("模块", ""),
            "severity": f.get("严重程度", ""), "status": "已回流" if f.get("已回流", "").startswith("是") else f.get("发现阶段", ""),
            "desc": "\n".join(parts), "created": "", "version": f.get("关联需求", ""), "resolution": f.get("归类", "")}


def read_sheets(p: Path) -> list[tuple[str, list[list[str]]]]:
    if p.suffix.lower() == ".csv":
        text = read_text_file(p)
        return [(p.stem, [[cell_str(c) for c in r] for r in csv.reader(io.StringIO(text))])]
    wb = open_workbook(p)
    try:
        return [(ws.title, sheet_rows(ws)) for ws in wb.worksheets]
    finally:
        wb.close()


# ───────────────────────── 缺陷 ─────────────────────────

def map_defect_columns(header: list[str], override: dict | None = None) -> dict:
    norm = [norm_header(h) for h in header]
    mapping = {}
    for field, names in DEFECT_COLUMNS.items():
        if override and override.get(field):
            want = norm_header(override[field])
            if want in norm:
                mapping[field] = norm.index(want)
            continue
        for name in names:
            n = norm_header(name)
            if n in norm:
                mapping[field] = norm.index(n)
                break
        else:
            for name in names:
                n = norm_header(name)
                hit = next((i for i, h in enumerate(norm) if n and n in h), None)
                if hit is not None and hit not in mapping.values():
                    mapping[field] = hit
                    break
    return mapping


# ───────────────────────── 向量 ─────────────────────────

def _post(url: str, body: dict, headers: dict | None = None, timeout: int = 300) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise IndexError_(f"向量接口报错 {e.code}：{e.read().decode('utf-8', 'replace')[:300]}") from e
    except urllib.error.URLError as e:
        raise IndexError_(f"连不上向量模型 {url}：{e.reason}（本地模型要先启动 Ollama）") from e


def embed(cfg: dict, texts: list[str]) -> list[list[float]]:
    provider = cfg.get("provider")
    if provider == "ollama":
        base = (cfg.get("base_url") or "http://localhost:11434").rstrip("/")
        return _post(f"{base}/api/embed", {"model": cfg["model"], "input": texts})["embeddings"]
    if provider == "openai":
        key = os.environ.get(cfg.get("api_key_env") or "", "")
        data = _post(cfg["base_url"].rstrip("/") + "/embeddings", {"model": cfg["model"], "input": texts},
                     {"Authorization": f"Bearer {key}"} if key else {})
        return [d["embedding"] for d in sorted(data["data"], key=lambda d: d["index"])]
    if provider == "mock":
        # 假向量：词组哈希到固定维度，只用来测流程（不代表真实语义效果）
        dim = int(cfg.get("dim", 64))
        vecs = []
        for t in texts:
            v = [0.0] * dim
            for tok in tokens(t):
                v[int(hashlib.md5(tok.encode()).hexdigest(), 16) % dim] += 1.0
            vecs.append(v)
        return vecs
    raise IndexError_(f"不认识的向量模型接入方式：{provider}")


def snippet(text: str, query: str, chars: int) -> str:
    """从和查询最贴近的那一行开始截取，长段落里也能直接看到命中的内容。"""
    q = set(tokens(query))
    lines = text.strip().split("\n")
    if not q or len(text) <= chars:
        return text.strip()[:chars]
    best = max(range(len(lines)), key=lambda i: len(q & set(tokens(lines[i]))))
    head = lines[best]
    pos = max((head.find(t) for t in q if t in head), default=0)
    start = max(0, pos - 20) if len(head) > chars else 0
    body = "\n".join([head[start:]] + lines[best + 1:])
    return ("…" if best or start else "") + body[:chars]


def normalize(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def pack(v: list[float]) -> bytes:
    return struct.pack(f"{len(v)}f", *v)


def unpack(b: bytes) -> list[float]:
    return list(struct.unpack(f"{len(b) // 4}f", b))


# ───────────────────────── 索引 ─────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, corpus TEXT, kind TEXT, sig TEXT, indexed TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS chunks(id INTEGER PRIMARY KEY, path TEXT, corpus TEXT, kind TEXT, title TEXT, text TEXT, ntok INTEGER);
CREATE INDEX IF NOT EXISTS chunks_path ON chunks(path);
CREATE INDEX IF NOT EXISTS chunks_kind ON chunks(kind);
CREATE TABLE IF NOT EXISTS postings(term TEXT, chunk INTEGER, tf INTEGER);
CREATE INDEX IF NOT EXISTS postings_term ON postings(term);
CREATE INDEX IF NOT EXISTS postings_chunk ON postings(chunk);
CREATE TABLE IF NOT EXISTS vectors(chunk INTEGER PRIMARY KEY, v BLOB);
CREATE TABLE IF NOT EXISTS tables(name TEXT PRIMARY KEY, path TEXT, sheet TEXT, corpus TEXT, columns TEXT, notes TEXT, nrows INTEGER);
CREATE TABLE IF NOT EXISTS trows(tbl TEXT, rn INTEGER, data TEXT);
CREATE INDEX IF NOT EXISTS trows_tbl ON trows(tbl);
CREATE TABLE IF NOT EXISTS defects(uid TEXT PRIMARY KEY, id TEXT, path TEXT, corpus TEXT, title TEXT, module TEXT, severity TEXT,
                                   status TEXT, created TEXT, version TEXT, resolution TEXT, descr TEXT);
CREATE INDEX IF NOT EXISTS defects_path ON defects(path);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
"""


class Index:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.con = sqlite3.connect(str(path))
        self.con.executescript(SCHEMA)

    def close(self):
        self.con.commit()
        self.con.close()

    # ── 写 ──
    def drop_file(self, path: str) -> None:
        c = self.con
        ids = [r[0] for r in c.execute("SELECT id FROM chunks WHERE path=?", (path,))]
        for i in range(0, len(ids), 500):
            part = ids[i:i + 500]
            marks = ",".join("?" * len(part))
            c.execute(f"DELETE FROM postings WHERE chunk IN ({marks})", part)
            c.execute(f"DELETE FROM vectors WHERE chunk IN ({marks})", part)
        c.execute("DELETE FROM chunks WHERE path=?", (path,))
        for t in [r[0] for r in c.execute("SELECT name FROM tables WHERE path=?", (path,))]:
            c.execute("DELETE FROM trows WHERE tbl=?", (t,))
        c.execute("DELETE FROM tables WHERE path=?", (path,))
        c.execute("DELETE FROM defects WHERE path=?", (path,))
        c.execute("DELETE FROM files WHERE path=?", (path,))

    def add_chunk(self, path: str, corpus: str, kind: str, title: str, text: str) -> int:
        toks = tokens(title + "\n" + text)
        cur = self.con.execute("INSERT INTO chunks(path, corpus, kind, title, text, ntok) VALUES(?,?,?,?,?,?)",
                               (path, corpus, kind, title, text, len(toks)))
        cid = cur.lastrowid
        self.con.executemany("INSERT INTO postings(term, chunk, tf) VALUES(?,?,?)", [(t, cid, n) for t, n in Counter(toks).items()])
        return cid

    def index_file(self, p: Path, corpus: str, kind: str, defect_override: dict | None = None) -> tuple[int, str]:
        key = str(p)
        self.drop_file(key)
        n, note = 0, ""
        if kind == "doc":
            for heading, body in chunk_doc(doc_text(p), p.stem):
                self.add_chunk(key, corpus, "doc", heading, body)
                n += 1
        elif kind == "config":
            if p.suffix.lower() == ".json":
                parsed = [(name, name.split("/", 1)[-1], cols, [], data) for name, cols, data in json_tables(p)]
            else:
                sheets = read_sheets(p)
                multi = len([s for s in sheets if s[1]]) > 1
                parsed = [(f"{p.stem}/{sheet}" if multi else p.stem, sheet, *parse_table(rows))
                          for sheet, rows in sheets if any(any(r) for r in rows)]
            for name, sheet, cols, notes, data in parsed:
                self.con.execute("INSERT OR REPLACE INTO tables VALUES(?,?,?,?,?,?,?)",
                                 (name, key, sheet, corpus, json.dumps(cols, ensure_ascii=False), json.dumps(notes, ensure_ascii=False), len(data)))
                self.con.executemany("INSERT INTO trows(tbl, rn, data) VALUES(?,?,?)",
                                     [(name, i + 1, json.dumps(r, ensure_ascii=False)) for i, r in enumerate(data)])
                self.add_chunk(key, corpus, "config", name, table_card(name, cols, notes, data))
                n += 1
        elif kind == "defect" and p.suffix.lower() == ".md":
            rec = md_defect(read_text_file(p), p.stem)
            self.con.execute("INSERT OR REPLACE INTO defects VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                             (f"{key}#{rec['id']}", rec["id"], key, corpus, rec["title"], rec["module"], rec["severity"], rec["status"],
                              rec["created"], rec["version"], rec["resolution"], rec["desc"][:4000]))
            self.add_chunk(key, corpus, "defect", f"{rec['id']} {rec['title']}",
                           f"模块：{rec['module']}　严重程度：{rec['severity']}　归类：{rec['resolution']}\n{rec['desc'][:1500]}")
            n = 1
        elif kind == "defect":
            _, rows = read_sheets(p)[0]
            rows = [r for r in rows if any(r)]
            if not rows:
                return 0, "空文件"
            header, data = rows[0], rows[1:]
            mp = map_defect_columns(header, defect_override)
            if "title" not in mp:
                return 0, f"认不出“标题”列（表头：{'、'.join(header[:12])}），在 qa.config.json 的 defect_columns 里指定"
            get = lambda r, f: r[mp[f]] if f in mp and mp[f] < len(r) else ""
            for i, r in enumerate(data):
                did = get(r, "id") or f"{p.stem}#{i + 1}"
                rec = {f: get(r, f) for f in DEFECT_COLUMNS}
                self.con.execute("INSERT OR REPLACE INTO defects VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                                 (f"{key}#{did}", did, key, corpus, rec["title"], rec["module"], rec["severity"], rec["status"],
                                  rec["created"], rec["version"], rec["resolution"], rec["desc"][:4000]))
                self.add_chunk(key, corpus, "defect", f"{did} {rec['title']}",
                               f"模块：{rec['module']}　严重程度：{rec['severity']}　状态：{rec['status']}\n{rec['desc'][:1500]}")
                n += 1
            missing = [f for f in ("module", "severity", "desc") if f not in mp]
            note = f"认出的列：{'、'.join(f'{k}={header[v]}' for k, v in mp.items())}" + (f"；没认出：{'、'.join(missing)}" if missing else "")
        return n, note

    def mark(self, p: Path, corpus: str, kind: str, note: str = "") -> None:
        self.con.execute("INSERT OR REPLACE INTO files VALUES(?,?,?,?,?,?)",
                         (str(p), corpus, kind, file_sig(p), datetime.now().isoformat(timespec="seconds"), note))

    def embed_missing(self, cfg: dict, batch: int = 32, progress=None) -> int:
        todo = self.con.execute("SELECT c.id, c.title, c.text FROM chunks c LEFT JOIN vectors v ON v.chunk=c.id "
                                "WHERE v.chunk IS NULL AND c.kind IN ('doc','defect')").fetchall()
        done = 0
        for i in range(0, len(todo), batch):
            part = todo[i:i + batch]
            vecs = embed(cfg, [f"{t}\n{x}"[:2000] for _, t, x in part])
            self.con.executemany("INSERT OR REPLACE INTO vectors VALUES(?,?)", [(cid, pack(normalize(v))) for (cid, _, _), v in zip(part, vecs)])
            done += len(part)
            self.con.commit()
            if progress:
                progress(done, len(todo))
        return done

    # ── 读 ──
    def stats(self) -> dict:
        q = lambda sql, *a: self.con.execute(sql, a).fetchone()[0]
        return {"files": q("SELECT COUNT(*) FROM files"), "docs": q("SELECT COUNT(DISTINCT path) FROM chunks WHERE kind='doc'"),
                "doc_chunks": q("SELECT COUNT(*) FROM chunks WHERE kind='doc'"), "tables": q("SELECT COUNT(*) FROM tables"),
                "table_rows": q("SELECT COUNT(*) FROM trows"), "defects": q("SELECT COUNT(*) FROM defects"),
                "vectors": q("SELECT COUNT(*) FROM vectors")}

    def bm25(self, query: str, kinds: tuple[str, ...], k: int = 50, k1: float = 1.5, b: float = 0.75) -> list[tuple[int, float]]:
        terms = Counter(tokens(query))
        if not terms:
            return []
        marks = ",".join("?" * len(kinds))
        n, avg = self.con.execute(f"SELECT COUNT(*), AVG(ntok) FROM chunks WHERE kind IN ({marks})", kinds).fetchone()
        if not n:
            return []
        scores = defaultdict(float)
        for term, qtf in terms.items():
            rows = self.con.execute(f"SELECT p.chunk, p.tf, c.ntok FROM postings p JOIN chunks c ON c.id=p.chunk "
                                    f"WHERE p.term=? AND c.kind IN ({marks})", (term, *kinds)).fetchall()
            df = len(rows)
            if not df or df > 0.6 * n and n > 50:  # 太常见的词组（如“测试”）区分不了，跳过
                continue
            idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
            for cid, tf, dl in rows:
                scores[cid] += idf * tf * (k1 + 1) / (tf + k1 * (1 - b + b * dl / (avg or 1)))
        return nlargest(k, scores.items(), key=lambda x: x[1])

    def vector_search(self, cfg: dict, query: str, kinds: tuple[str, ...], k: int = 50) -> list[tuple[int, float]]:
        marks = ",".join("?" * len(kinds))
        rows = self.con.execute(f"SELECT v.chunk, v.v FROM vectors v JOIN chunks c ON c.id=v.chunk WHERE c.kind IN ({marks})", kinds).fetchall()
        if not rows:
            return []
        qv = normalize(embed(cfg, [query])[0])
        try:
            import numpy as np
            mat = np.frombuffer(b"".join(r[1] for r in rows), dtype=np.float32).reshape(len(rows), -1)
            sims = mat @ np.asarray(qv, dtype=np.float32)
            order = np.argsort(-sims)[:k]
            return [(rows[i][0], float(sims[i])) for i in order]
        except ImportError:
            scored = [(cid, sum(a * b for a, b in zip(qv, unpack(v)))) for cid, v in rows]
            return nlargest(k, scored, key=lambda x: x[1])

    def search(self, query: str, kinds: tuple[str, ...] = ("doc",), k: int = 8, emb: dict | None = None) -> list[dict]:
        """关键词和向量两路检索后合并。没配向量模型或连不上时只用关键词。

        合并按分数加权（两路分数各自归一化到 0～1，向量乘权重 weight，默认 1.5），而不是按名次平均：
        关键词明确命中的段落（比如精确的规则名、缺陷标题）不会被向量结果挤掉。
        权重 1.5 是在钓鱼塘 44 份真实设计文档上实测选的：精确词问题 8/8 都排第 1，换了说法的问题平均倒数排名
        从只用关键词的 0.57 升到 0.75（0.6 时只有 0.65；1.0～3.0 之间差别不大，取中间）。
        """
        bm = self.bm25(query, kinds)
        vec, mode = [], "关键词"
        if emb:
            try:
                vec = self.vector_search(emb, query, kinds)
                mode = "关键词 + 向量" if vec else mode
            except IndexError_ as e:
                mode = f"关键词（向量没用上：{e}）"
        fused = defaultdict(float)
        if bm:
            top_score = bm[0][1] or 1.0
            for cid, s in bm:
                fused[cid] += s / top_score
        if vec:
            weight = float((emb or {}).get("weight", 1.5))
            hi, lo = vec[0][1], vec[-1][1]
            for cid, s in vec:
                fused[cid] += weight * ((s - lo) / (hi - lo) if hi > lo else 1.0)
        top = nlargest(k, fused.items(), key=lambda x: x[1])
        out = []
        for cid, score in top:
            path, corpus, kind, title, text = self.con.execute("SELECT path, corpus, kind, title, text FROM chunks WHERE id=?", (cid,)).fetchone()
            out.append({"id": cid, "path": path, "corpus": corpus, "kind": kind, "title": title, "text": text, "score": round(score, 4), "mode": mode})
        return out

    def table(self, name: str) -> dict | None:
        r = self.con.execute("SELECT name, path, sheet, corpus, columns, notes, nrows FROM tables WHERE name=?", (name,)).fetchone()
        if not r:
            r = self.con.execute("SELECT name, path, sheet, corpus, columns, notes, nrows FROM tables WHERE name LIKE ? ORDER BY length(name) LIMIT 1",
                                 (f"%{name}%",)).fetchone()
        if not r:
            return None
        return {"name": r[0], "path": r[1], "sheet": r[2], "corpus": r[3], "columns": json.loads(r[4]), "notes": json.loads(r[5]), "nrows": r[6]}

    def table_rows(self, name: str, where: list[tuple[str, str]], limit: int) -> tuple[list[tuple[int, list[str]]], int]:
        t = self.table(name)
        cols = t["columns"]
        idx = []
        for col, val in where:
            if col not in cols:
                raise IndexError_(f"表 {t['name']} 没有字段 {col}。字段：{'、'.join(cols)}")
            idx.append((cols.index(col), val))
        hits, total = [], 0
        for rn, data in self.con.execute("SELECT rn, data FROM trows WHERE tbl=? ORDER BY rn", (t["name"],)):
            row = json.loads(data)
            if all((row[i] if i < len(row) else "") == v for i, v in idx):
                total += 1
                if len(hits) < limit:
                    hits.append((rn, row))
        return hits, total

    def defect_stats(self, top: int = 20) -> dict:
        q = lambda col: self.con.execute(f"SELECT COALESCE(NULLIF({col}, ''), '（空）') x, COUNT(*) n FROM defects GROUP BY x ORDER BY n DESC LIMIT ?", (top,)).fetchall()
        return {"module": q("module"), "severity": q("severity"), "status": q("status")}


# ───────────────────────── 资料目录（corpora） ─────────────────────────

def corpus_files(c: dict) -> list[Path]:
    """c 里可以有 only：只收这些文件名（工作区自带的“本工作区需求”只收 需求.md 和 最终测试点.md）；
    glob：文件名要符合的通配（“本工作区缺陷”只收 D*.md）。"""
    exts = {"doc": DOC_EXT, "config": CONFIG_EXT, "defect": DEFECT_EXT}[c["kind"]]
    only, pattern = set(c.get("only") or []), c.get("glob")
    root = Path(c["path"])
    if root.is_file():
        return [root] if root.suffix.lower() in exts else []
    if not root.exists():
        return []
    out = []
    for p in root.rglob("*"):
        if (p.is_file() and p.suffix.lower() in exts and not p.name.startswith(("~$", ".")) and not (set(p.parts) & SKIP_DIRS)
                and (not only or p.name in only) and (not pattern or Path(p.name).match(pattern))):
            out.append(p)
    return sorted(out)


def file_chunks(idx: Index, name: str) -> tuple[list[str], list[tuple[str, str]]]:
    """按文件路径或文件名找已建索引的文件，返回 (匹配到的文件, 第一个文件的全部段落)。读 Word / PDF 不用再转换。"""
    paths = [r[0] for r in idx.con.execute("SELECT path FROM files WHERE path=? OR path LIKE ? ORDER BY path",
                                           (name, f"%{name}"))]
    if not paths:
        paths = [r[0] for r in idx.con.execute("SELECT path FROM files WHERE path LIKE ? ORDER BY path", (f"%{name}%",))]
    if not paths:
        return [], []
    rows = idx.con.execute("SELECT title, text FROM chunks WHERE path=? ORDER BY id", (paths[0],)).fetchall()
    return paths, rows


def pending(idx: Index, corpora: list[dict]) -> dict:
    """哪些文件要重建索引：新增、改动、删除（只比大小和修改时间，不读内容，几千个文件也很快）。
    同一个文件换了登记类型（比如从文档改成配置表）或换了资料名，也算改动，要按新类型重建。"""
    known = {r[0]: (r[1], r[2], r[3]) for r in idx.con.execute("SELECT path, sig, corpus, kind FROM files")}
    seen, new, changed = set(), [], []
    for c in corpora:
        for p in corpus_files(c):
            key = str(p)
            if key in seen:  # 同一个文件被两批资料登记了，只按先登记的那批建
                continue
            seen.add(key)
            if key not in known:
                new.append((c, p))
            elif known[key] != (file_sig(p), c["name"], c["kind"]):
                changed.append((c, p))
    deleted = [k for k in known if k not in seen]  # 资料名被移除、文件被删掉的；还在但换了资料名的算在 changed 里
    return {"new": new, "changed": changed, "deleted": deleted}


def build(idx: Index, corpora: list[dict], defect_override: dict | None = None, progress=None) -> dict:
    todo = pending(idx, corpora)
    for key in todo["deleted"]:
        idx.drop_file(key)
    work = todo["new"] + todo["changed"]
    done, skipped, notes = 0, [], []
    t0 = time.monotonic()
    for i, (c, p) in enumerate(work, 1):
        try:
            n, note = idx.index_file(p, c["name"], c["kind"], defect_override)
            idx.mark(p, c["name"], c["kind"], note)
            if note:
                notes.append(f"{p.name}：{note}")
            done += 1
        except IndexError_ as e:
            skipped.append(f"{p.name}：{e}")
        except Exception as e:  # noqa: BLE001  坏文件不让整次建索引失败
            skipped.append(f"{p.name}：{type(e).__name__}: {str(e)[:120]}")
        if i % 20 == 0:
            idx.con.commit()
            if progress:
                progress(i, len(work))
    idx.con.commit()
    return {"indexed": done, "deleted": len(todo["deleted"]), "skipped": skipped, "notes": notes,
            "seconds": round(time.monotonic() - t0, 1), "new": len(todo["new"]), "changed": len(todo["changed"])}
