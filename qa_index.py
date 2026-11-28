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


def _text_over_numbers(rows: list[list[str]], i: int) -> bool:
    """第 i 行是不是说明行：下面的数据里全是数字的那些列，这一行填的却是文字（“物品ID的说明”）。
    不管表头是英文还是中文都能认；认不出的话说明行会混进数据，编号列就不唯一了，表间关联也认不准。"""
    num = re.compile(r"-?\d+(\.\d+)?")
    r = rows[i]
    below = [x for x in rows[i + 1:i + 21] if any(x)]
    filled = [c for c in r if c]
    if len(below) < 3 or not filled or sum(not num.fullmatch(c) for c in filled) < 0.6 * len(filled):
        return False
    numcols = []
    for j in range(len(r)):
        vals = [x[j] for x in below if j < len(x) and x[j]]
        if len(vals) >= 0.8 * len(below) and sum(bool(num.fullmatch(v)) for v in vals) >= 0.9 * len(vals):
            numcols.append(j)
    texty = sum(1 for j in numcols if r[j] and not num.fullmatch(r[j]))
    return bool(numcols) and texty >= 0.6 * len(numcols)


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
        if typeish or chinese_comment or _text_over_numbers(rows, i):
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


COMMENT_KEYS = {"comment", "comments", "note", "notes", "desc", "description", "remark", "说明", "备注", "注释"}


def _is_comment(key: str, val) -> bool:
    k = str(key).lower().strip("_")
    return isinstance(val, str) and len(val) >= 4 and (k in COMMENT_KEYS or k.endswith(("note", "comment", "desc")))


def json_tables(p: Path) -> list[tuple[str, list[str], list[list[str]], list[str]]]:
    """JSON 配置拆成表，表名 = 文件名/路径（例如 economy/gear.rod）：
    - 对象数组（gear.rod: [{tier, name, price}, …]）→ 一个对象一行
    - 子项都是结构相近的对象（species: {jiyu: {name, median}, liyu: {…}}）→ 一个子项一行，第一列“键”
    - 其余的单个数值 → “键 / 值”两列：顶层每一节一张表（economy/session），节里更深的对象摊平成
      a.b.c 这样的键，不再拆成小表；数组值写成 JSON 文本
    每张表还带上它所在对象和上层对象里的注释（_comment、note、desc 这类字段）：
    英文表名、全是数字的表（homePond.levels 只有 rep 一列）靠这些中文说明才找得到。
    """
    data = json.loads(read_text_file(p))
    tables: list[tuple[str, list[str], list[list[str]], str]] = []
    comments: dict[str, list[str]] = {}

    def gather(x, path: str) -> None:
        if isinstance(x, dict):
            comments[path] = [v for k, v in x.items() if _is_comment(k, v)]
            for k, v in x.items():
                gather(v, f"{path}.{k}" if path else str(k))

    gather(data, "")

    def notes_for(path: str) -> list[str]:
        parts = path.split(".") if path else []
        # 自己在前，越往上越靠后；文件最外层的注释（整份文件的通用说明）只给最外层那张表，免得每张表都一样
        chain = [".".join(parts[:i]) for i in range(len(parts), 0, -1)] or [""]
        out, size = [], 0
        for c in chain:
            for t in comments.get(c, []):
                if t not in out and size < 800:
                    out.append(t[:400])
                    size += len(t[:400])
        return out

    def name_of(path: str) -> str:
        return f"{p.stem}/{path}" if path else p.stem

    def add_records(path: str, items: list[tuple[str, object]], key_col: str | None) -> None:
        cols, rows = ([key_col] if key_col else []), []
        for key, item in items:
            flat = {key_col: key} if key_col else {}
            _flat(item, "", flat)
            cols += [c for c in flat if c not in cols]
            rows.append(flat)
        tables.append((name_of(path), cols, [[r.get(c, "") for c in cols] for r in rows], path))

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
                                [[k, json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else cell_str(v)] for k, v in kv], path))

    walk(data, "")
    return [(name, cols, rows, notes_for(path)) for name, cols, rows, path in tables]


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


# ───────────────────────── 配置表之间的关联 ─────────────────────────
#
# 游戏配置常见“商城表的物品编号 → 物品表 → 品质字段 → 品质表”这种多表关联，表里一般不写外键。
# 框架按取值自动认：A 表某一列的值，全都能在 B 表的编号列（每行都有、不重复的列）里找到，就认为 A 这一列引用 B。
# 再看字段名和表名对不对得上（物品ID ↔ 物品表、pond ↔ ponds）定可信度：
#   高：值全对上，而且字段名对得上表名；或者值是文字编号（p0、jiyu）、字段名对得上编号列名
#   中：值全对上但名字对不上（文字编号，或 100 以上的数字编号）；名字对得上表名、只有一两个值找不到（可能填错了）
# 只用“高”的连带查（cfg show --expand）；“中”的要人或模型确认。

LINK_GENERIC = {"id", "ids", "key", "keys", "name", "no", "type", "value", "list", "data", "info", "config", "cfg", "table",
                "键", "值", "编号", "序号", "名称", "名字", "类型", "列表", "表", "配置", "数据"}
# 字段名和表名一个英文一个中文时靠这张对照对上（Quality ↔ 品质表、DropID ↔ 掉落、物品ID ↔ Items）。只放各类游戏通用的词
LINK_WORDS = {
    "item": "物品 道具", "prop": "道具", "quality": "品质", "rarity": "品质 稀有度", "monster": "怪物", "enemy": "怪物 敌人", "boss": "首领",
    "drop": "掉落", "loot": "掉落", "skill": "技能", "buff": "增益 状态", "effect": "效果", "quest": "任务", "task": "任务", "mission": "任务",
    "achievement": "成就", "equip": "装备", "equipment": "装备", "gear": "装备", "weapon": "武器", "armor": "防具", "slot": "部位 槽位",
    "set": "套装", "suit": "套装", "hero": "英雄", "role": "角色", "character": "角色", "job": "职业", "class": "职业", "profession": "职业",
    "level": "等级", "stage": "关卡", "chapter": "章节", "dungeon": "副本", "map": "地图", "scene": "场景", "mount": "坐骑", "pet": "宠物",
    "mail": "邮件", "activity": "活动", "event": "活动", "currency": "货币", "money": "货币", "pack": "礼包", "gift": "礼包", "bundle": "礼包",
    "shop": "商城 商店", "store": "商城 商店", "mall": "商城", "goods": "商品", "product": "商品", "reward": "奖励", "guild": "公会",
    "title": "称号", "avatar": "头像", "frame": "头像框", "talent": "天赋", "rune": "符文", "card": "卡牌", "chest": "宝箱", "box": "宝箱",
    "npc": "npc", "dialog": "对话", "story": "剧情", "building": "建筑", "vip": "vip", "privilege": "特权", "sign": "签到", "checkin": "签到",
    "attr": "属性", "attribute": "属性", "formula": "公式", "language": "语言 多语言", "text": "文本", "sound": "音效", "model": "模型",
}
_LINK_ZH: dict[str, set[str]] = defaultdict(set)
for _en, _zh in LINK_WORDS.items():
    for _w in _zh.split():
        _LINK_ZH[_w].add(_en)
LINK_KEYISH = {"id", "key", "no", "code", "lv", "level", "tier", "键", "编号", "序号", "代号", "等级", "档位"}
_NUM = re.compile(r"-?\d+(?:\.\d+)?")


def ref_values(cell: str) -> list[str]:
    """一格里可能引用别的表的值：JSON 列表取元素、JSON 对象取键、“1001;1002”按分隔符拆，其余整格算一个值。"""
    v = (cell or "").strip()
    if not v or v.lower() in ("true", "false", "null", "none"):
        return []
    if v[0] in "[{":
        try:
            x = json.loads(v)
        except ValueError:
            return [v]
        if isinstance(x, dict):
            return [str(k) for k in x]
        if isinstance(x, list) and x and all(isinstance(e, (str, int)) and not isinstance(e, bool) for e in x):
            return [str(e) for e in x]
        return []
    pair = re.compile(r"([\w.-]+?)[:：*×#](\d+(?:\.\d+)?)")  # “物品ID:数量”“1001*5”这类写法取前面的编号
    if re.fullmatch(r"[\w.:：*×#-]+(?:[;|,，；][\w.:：*×#-]+)+", v):
        parts = [p for p in re.split(r"[;|,，；]", v) if p]
        return [m.group(1) if (m := pair.fullmatch(p)) else p for p in parts]
    m = pair.fullmatch(v)
    return [m.group(1)] if m and not _NUM.fullmatch(v) else [v]


def name_parts(s: str) -> set[str]:
    """字段名、表名拆成词：驼峰、下划线、点、中英文交界都拆开；英文加上单数形式，中文去掉“表 / 编号 / ID”这类尾巴。"""
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", s or "")
    s = re.sub(r"(?<=[一-鿿])(?=[A-Za-z0-9])|(?<=[A-Za-z0-9])(?=[一-鿿])", " ", s)
    out = set()
    for t in re.split(r"[^0-9A-Za-z一-鿿]+", s):
        t = t.lower()
        if not t or t.isdigit():
            continue
        if t.isascii():
            forms = {t}
            if t == "lv":
                forms.add("level")
            elif len(t) > 3 and t.endswith("ies"):
                forms.add(t[:-3] + "y")
            elif len(t) > 3 and t.endswith("es"):
                forms |= {t[:-2], t[:-1]}
            elif len(t) > 2 and t.endswith("s"):
                forms.add(t[:-1])
            out |= forms
            for f in forms:  # 英文词配上中文（quality → 品质）
                out |= set(LINK_WORDS.get(f, "").split())
        else:
            w = re.sub(r"(编号|序号|列表|表|号)$", "", t) or t
            out.add(w)
            for zh, ens in _LINK_ZH.items():  # 中文词配上英文（物品 → item），含在长词里的也算（掉落物品名 → drop、item）
                if zh in w:
                    out |= ens
    return out


def link_hint(col: str, table: str, key: str) -> str:
    """字段名和被引用的表对不对得上：'表名'（物品ID ↔ 物品表）、'编号列名'（tier ↔ tier）或 ''。"""
    a = name_parts(col) - LINK_GENERIC
    if not a:
        return ""

    def match(b: set[str]) -> bool:
        b = b - LINK_GENERIC
        return bool(a & b) or any(len(x) >= 2 and len(y) >= 2 and not x.isascii() and not y.isascii() and (x in y or y in x)
                                  for x in a for y in b)
    if match(name_parts(table.split("/", 1)[-1])):
        return "表名"
    return "编号列名" if match(name_parts(key)) else ""


def generic_hint(col: str, src: str, dst: str) -> str:
    """字段名是“类型”“type”这种通用词时：被引用的表名正好是“本表名 + 字段名”（物品表的“类型” → 物品类型表）才算对得上。"""
    c = col.split(".")[-1].strip().lower()
    if not c or (name_parts(c) - LINK_GENERIC):
        return ""
    s = re.sub(r"表$", "", src.split("/", 1)[-1].split(".")[-1].lower())
    d = re.sub(r"表$", "", dst.split("/", 1)[-1].split(".")[-1].lower())
    return "表名" if s and d in (s + c, s + "_" + c) else ""


LINK_VALUE_COLS = {"值", "value", "说明", "备注", "描述", "注释", "note", "desc", "comment"}


def is_kv_table(cols: list[str]) -> bool:
    """JSON 里零散的参数拆成的“键 / 值”两列表：一行就是一个参数（unlockMain = m14a）。"""
    return cols == ["键", "值"]


def key_columns(table: str, cols: list[str], rows: list[list[str]]) -> list[tuple[int, str, set[str], str]]:
    """能被别的表引用的列，第四项是种类：
    - 编号：每行都有、互不重复。数字列还要名字像编号（id、tier、lv、物品ID、道具编号），
      而且除了“id / 编号”这类词以外只能是表名本身（物品表的“物品ID”算，ponds 的 unlockPondLv 不算），免得价格、解锁等级被当成编号
    - 弱编号：互不重复、但名字不像编号的数字列（品质表的“品质等级” 1～4）：要字段名对得上表名才认
    - 分类：有重复的短文字列（品质表的“类型”A/B/C/D，一个类型对应几行）：要字段名对得上才认，算一对多"""
    out = []
    if len(rows) < 2 or is_kv_table(cols):
        return out
    tparts = name_parts(table.split("/", 1)[-1]) - LINK_GENERIC
    for i, c in enumerate(cols):
        if c.lower() in LINK_VALUE_COLS:
            continue
        vals = [r[i] if i < len(r) else "" for r in rows]
        filled = [v for v in vals if v]
        if not filled or any(v[0] in "[{" or len(v) > 40 or ref_values(v) != [v] for v in filled):
            continue  # 一格里写了几个值（1001;1002、1001:5）的列是在引用别人，不是编号
        numeric = all(_NUM.fullmatch(v) for v in filled)
        if len(filled) == len(vals) and len(set(vals)) == len(vals):
            if numeric:
                parts = name_parts(c)
                keyish = parts & LINK_KEYISH or re.search(r"(id|key|编号|序号|代号)$", c, re.I)
                if any("." in v for v in vals):
                    continue
                # 第一列叫“某某ID”的照样算（商城表的“商品ID”）；别的列要名字就是表名本身
                strong = keyish and (i == 0 or (parts - LINK_KEYISH - LINK_GENERIC) <= tparts)
                out.append((i, c, set(vals), "编号" if strong else "弱编号"))
            else:
                out.append((i, c, set(vals), "编号"))
        elif (sum(not _NUM.fullmatch(v) for v in filled) >= 0.8 * len(filled) and 2 <= len(set(filled)) <= 300
              and all(len(v) <= 20 for v in filled)):
            out.append((i, c, set(filled), "分类"))
    return out


def _link_columns(cols: list[str], rows: list[list[str]]):
    """要检查的“列”，产出 (列名, 序号或 None, 值集合, 一格里有没有多个值, 类型)：
    普通表的每一列（类型“列”）；键值表的每一行，列名 = 参数名（“键值行”）；
    编号写在字段名里的（mix.jiyu、mix.luofei → 鱼种表的 jiyu、luofei），列名写成 mix.*（“字段名”）。"""
    kv = is_kv_table(cols)
    names = [r[0] for r in rows if r] if kv else cols
    if kv:
        for r in rows:
            vals = ref_values(r[1] if len(r) > 1 else "")
            if r and vals:
                yield r[0], None, set(vals), len(vals) > 1, "键值行"
    else:
        for ci, col in enumerate(cols):
            vals, multi = set(), False
            for r in rows:
                part = ref_values(r[ci] if ci < len(r) else "")
                multi = multi or len(part) > 1
                vals.update(part)
            if vals:
                yield col, ci, vals, multi, "列"
    groups: dict[str, set[str]] = defaultdict(set)
    for n in names:
        if "." in n:
            pre, suf = n.rsplit(".", 1)
            groups[pre].add(suf)
    for pre, sufs in groups.items():
        if len(sufs) >= 2 and not any(_NUM.fullmatch(s) for s in sufs):
            yield f"{pre}.*", None, sufs, True, "字段名"


def find_links(tables: dict[str, tuple[list[str], list[list[str]]]]) -> list[dict]:
    """tables：{表名: (字段, 数据行)}。返回认出的关联，每条 {from, col, to, key, level, why, n, missing, multi, kind}。"""
    keys = {name: key_columns(name, cols, rows) for name, (cols, rows) in tables.items()}
    inv: dict[str, list[tuple[str, int]]] = defaultdict(list)  # 值 → [(表, 列序号)]
    for name, ks in keys.items():
        for i, _, vals, _sort in ks:
            for v in vals:
                inv[v].append((name, i))
    found = []
    for t, (cols, rows) in tables.items():
        own = {i: vals for i, _, vals, sort in keys[t] if sort == "编号"}
        for col, ci, vals, multi, kind in _link_columns(cols, rows):
            if len(vals) > 5000:
                continue
            hits = Counter(x for v in vals for x in set(inv.get(v, ())))
            numeric = all(_NUM.fullmatch(v) for v in vals)
            cands = []
            for (u, ki), m in hits.items():
                if u == t and (ki == ci or ci is None):
                    continue
                kcol, kvals, sort = next((c, s, k) for i, c, s, k in keys[u] if i == ki)
                same = sort == "编号" and ci in own and own[ci] == kvals
                if same and (t, col) > (u, kcol):
                    continue  # 两张表用同一套编号：只记一次
                hint = link_hint(col, u, kcol) or generic_hint(col, t, u)
                if u == t and (hint != "表名" or sort != "编号"):
                    continue
                if ci in own and not same and not hint:
                    continue  # 这一列是本表自己的编号：名字对不上时，编号范围碰巧落在别的表里多半是巧合
                cover, level = m / len(vals), ""
                if sort == "分类":  # 引用的是 B 表里有重复的字段（品质 A → 品质表里类型为 A 的几行）：名字要对得上
                    if cover == 1.0 and hint and len(vals) >= 2:
                        level = "高" if hint == "表名" else "中"
                elif sort == "弱编号":  # B 表不重复、但名字不像编号的数字列：要字段名对得上表名
                    if cover == 1.0 and hint == "表名" and len(vals) >= 3:
                        level = "中"
                elif cover == 1.0:
                    if numeric:  # 数字太容易碰巧对上：要字段名对得上表名，或者是 100 以上、至少 3 个的编号
                        if hint == "表名" and len(vals) >= 2:
                            level = "高"
                        elif len(vals) >= 3 and min(float(v) for v in vals) >= 100:
                            level = "中"
                    elif hint == "表名" or (hint and len(vals) >= 2):
                        level = "高"
                    elif len(vals) >= 2:
                        level = "中"
                elif hint == "表名" and m >= 3 and len(vals) - m <= max(1, 0.2 * len(vals)):
                    level = "中"  # 只差一两个对不上：多半是配置填错了，正好是要测的
                if not level:
                    continue
                missing = sorted(vals - kvals)[:5]
                why = (f"{m} 个值全能在 {u}.{kcol} 找到" if cover == 1.0 else f"{len(vals)} 个值里 {len(vals) - m} 个找不到（可能填错了）：{'、'.join(missing)}")
                why += {"表名": "；字段名对得上表名", "编号列名": "；字段名对得上编号列名"}.get(hint, "；名字对不上，要确认")
                if sort == "分类":
                    why += f"；一对多：{u} 里一个 {kcol} 对应好几行"
                elif sort == "弱编号":
                    why += f"；{u}.{kcol} 不重复但名字不像编号，要确认"
                elif numeric and level != "高":
                    why += "；数字编号，可能是巧合"
                if same:
                    why += "；两张表用同一套编号"
                if kind == "字段名":
                    why = why.replace("个值", "个字段名的后半截", 1)
                cands.append({"from": t, "col": col, "to": u, "key": kcol, "level": level, "why": why, "n": len(vals),
                              "missing": missing, "multi": multi, "kind": kind, "target": sort,
                              "_rank": (level != "高", sort != "编号", hint != "表名", not hint, -cover, -len(kvals), -len(tables[u][0]))})
            cands.sort(key=lambda x: x["_rank"])
            if cands and "字段名对得上表名" in cands[0]["why"]:  # 已经有名字对得上的主表：只靠同名字段对上的分类列不再列
                cands = cands[:1] + [c for c in cands[1:] if not (c["target"] == "分类" and "字段名对得上表名" not in c["why"])]
            for j, c in enumerate(cands[:3]):
                c.pop("_rank")
                if j and c["level"] == "高":  # 同一列能对上好几张表：编号最全、字段最多的当主表，其余的要确认
                    c["level"], c["why"] = "中", c["why"] + f"；同一列也对得上 {cands[0]['to']}，要确认是哪张"
                found.append(c)
    found.sort(key=lambda x: (x["from"], x["level"] != "高", x["col"]))
    return found


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
                parsed = [(name, name.split("/", 1)[-1], cols, [[n] for n in notes], data) for name, cols, data, notes in json_tables(p)]
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
        # 配置表的目录卡片也算向量：英文表名（gear.rod）靠意思才对得上“鱼竿”
        todo = self.con.execute("SELECT c.id, c.title, c.text FROM chunks c LEFT JOIN vectors v ON v.chunk=c.id "
                                "WHERE v.chunk IS NULL AND c.kind IN ('doc','defect','config')").fetchall()
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

    def links(self) -> list[dict]:
        """配置表之间的关联（见 find_links）。配置表没变时直接用上次算好的。"""
        sig = hashlib.sha1(json.dumps(self.con.execute(
            "SELECT t.name, t.nrows, f.sig FROM tables t LEFT JOIN files f ON f.path=t.path ORDER BY t.name").fetchall()).encode()).hexdigest()
        r = self.con.execute("SELECT value FROM meta WHERE key='cfg_links'").fetchone()
        if r:
            cached = json.loads(r[0])
            if cached.get("sig") == sig:
                return cached["links"]
        tables = {}
        for name, cols in self.con.execute("SELECT name, columns FROM tables").fetchall():
            tables[name] = (json.loads(cols), [json.loads(d) for (d,) in self.con.execute("SELECT data FROM trows WHERE tbl=? ORDER BY rn", (name,))])
        found = find_links(tables)
        self.con.execute("INSERT OR REPLACE INTO meta VALUES('cfg_links', ?)", (json.dumps({"sig": sig, "links": found}, ensure_ascii=False),))
        self.con.commit()
        return found

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
    # os.walk 默认不进链接文件夹：共享盘、SVN 目录里有指回上层的链接（或 Windows 目录联接）时不会绕圈
    for d, subdirs, files in os.walk(root):
        subdirs[:] = [s for s in subdirs if s not in SKIP_DIRS]
        for f in files:
            p = Path(d) / f
            if (p.suffix.lower() in exts and not f.startswith(("~$", ".")) and (not only or f in only)
                    and (not pattern or Path(f).match(pattern))):
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
