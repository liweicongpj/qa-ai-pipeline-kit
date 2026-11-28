"""常量和通用小工具：读写文件、模板渲染、Markdown 表格和字段解析、时间、运行子进程。"""
from __future__ import annotations

import sys
import hashlib
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path


try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

KIT = Path(__file__).resolve().parent.parent  # 框架仓库根目录（本文件在 qakit/ 里）


LOCAL = KIT / "qa.local.json"


EXAMPLE = KIT / "examples" / "mini-shop"


CONFIG = "qa.config.json"


VERDICTS = ("采纳", "改后采纳", "否决")


REJECT_REASONS = ("重复", "太笼统", "不符合规则", "无关")


SEVERITIES = ("P0", "P1", "P2", "P3")


TEXT_EXT = {".md", ".txt", ".json", ".csv", ".yaml", ".yml", ".ts", ".js", ".mjs", ".py", ".html", ".xml",
            ".proto", ".sql", ".ini", ".toml", ".cfg", ".log", ".tsv", ".lua", ".cs", ".java", ".go"}


MAX_FILE = 2_000_000


DEFAULT_RISKS = ["资产一致性", "幂等", "并发", "边界", "时间（跨天、服务器时间）", "配置", "安全", "兼容与弱网"]


DEFAULT_TARGET = {"base_url": "", "start_cmd": "", "start_cwd": "", "health_path": "/health", "tests": "tests", "api": "api"}


ACCESS = {
    "blackbox": ("无代码权限", "只能拿到策划案、文档、客户端和抓包。测试点照常生成；执行靠人工（qa.py exec 记执行结果），能抓包的可以 ingest --har 把请求整理成接口文档，再走接口自动化。不要去找、也不要猜代码。"),
    "interface": ("开放接口或协议", "能拿到开放给测试的接口或协议（HTTP、WebSocket、TCP+protobuf……）和文档，看不到服务端代码。用例只依据接口文档和知识库写。"),
    "source": ("完整代码权限", "可以读服务端代码（qa.config.json 的 source_paths）。整理知识库时可以对着代码核对规则、找出改同一个数值的代码位置补进耦合关系表；改版时用 qa.py impact 按代码改动推荐回归范围。资料和代码不一致时两边都写上，标“待确认”。"),
}


class QaError(Exception):
    pass


def out(text: str = "") -> None:
    print(text, flush=True)


def read(p: Path) -> str:
    return p.read_text(encoding="utf-8", errors="replace")


def write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8", newline="\n")


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def qa_cmd() -> str:
    return f'python "{KIT / "qa.py"}"'


def sub_env(extra: dict | None = None) -> dict:
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env.update(extra or {})
    return env


def run(cmd, cwd: Path | None = None, env: dict | None = None, timeout: int | None = None):
    return subprocess.run(cmd, cwd=cwd, env=env or sub_env(), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)


def render(text: str, values: dict) -> str:
    def rep(m):
        key = m.group(1).strip()
        return str(values[key]) if key in values else m.group(0)
    return re.sub(r"\{\{\s*([\w\-]+)\s*\}\}", rep, text)


# 资料各部分之间的分隔（看不见的字符）：拼提示词时按部分分配字数，拼好后换成空行
PART = "\n\n\x1e"


def fit_text(text: str, n: int) -> str:
    """压到 n 字以内：留开头一大截和结尾一小截（段尾常有“历史缺陷要写进测试点”这类要求），中间注明截掉了多少、可以用工具查。"""
    if len(text) <= n:
        return text
    note = "\n…（这部分太长，中间 {} 字没放进来：需要时用 search、cfg_show、read_doc、read_file 查）…\n"
    tail = min(400, n // 5)
    head = max(0, n - tail - len(note) - 8)
    return text[:head] + note.format(len(text) - head - tail) + (text[-tail:] if tail else "")


def fit_parts(parts: list[str], budget: int) -> list[str]:
    """几份资料一起压到 budget 字以内：小的整份留，大的按长短比例分剩下的字数（本地小模型上下文短时用）。"""
    total = sum(len(p) for p in parts)
    if total <= budget or not parts:
        return parts
    budget = max(budget, 600 * len(parts))
    small = [i for i, p in enumerate(parts) if len(p) <= budget / (2 * len(parts))]
    room = budget - sum(len(parts[i]) for i in small)
    big = [i for i in range(len(parts)) if i not in small]
    big_total = sum(len(parts[i]) for i in big) or 1
    return [p if i in small else fit_text(p, max(600, int(room * len(p) / big_total))) for i, p in enumerate(parts)]


def md_tables(text: str) -> list[list[list[str]]]:
    """拆出 Markdown 表格：每张表是若干行，每行是若干格（去掉分隔行）。
    格子里转义的竖线 \\| 不算分隔（照 Markdown 的规矩），拆完还原成 |：引用原文里的表格行时要用。"""
    tables, cur = [], []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("|"):
            s = s[1:]
            if s.endswith("|") and not s.endswith("\\|"):
                s = s[:-1]
            cells = [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", s)]
            if not all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
                cur.append(cells)
        elif cur:
            tables.append(cur)
            cur = []
    if cur:
        tables.append(cur)
    return tables


def pct(a: float, b: float) -> str:
    return f"{int(a * 100 // b)}%" if b else "—"


def money(x) -> str:
    return "—" if x is None else f"${x:.4f}"


def fields(text: str) -> dict:
    """读“- 键：值”形式的字段。"""
    return {m.group(1).strip(): m.group(2).strip()
            for m in re.finditer(r"^-[ \t]*([^：:\n]+)[：:][ \t]*(.*)$", text, re.M)}


def set_field(text: str, key: str, value: str) -> str:
    pat = rf"^(-[ \t]*{re.escape(key)}[：:])[ \t]*.*$"
    return re.sub(pat, lambda m: f"{m.group(1)}{value}", text, count=1, flags=re.M) if re.search(pat, text, re.M) else text


def section(text: str, title: str) -> str:
    m = re.search(rf"^##[ \t]*{re.escape(title)}[^\n]*\n(.*?)(?=^##[ \t]|\Z)", text, re.M | re.S)
    return m.group(1).strip() if m else ""


def strip_fence(text: str) -> str:
    m = re.fullmatch(r"\s*```[a-zA-Z]*\n(.*?)\n```\s*", text, re.S)
    return m.group(1) if m else text


def split_names(s: str) -> list[str]:
    return [x.strip() for x in re.split(r"[、,，/；;]", s) if x.strip() and x.strip() not in ("—", "-", "无")]


def read_table(path: Path) -> tuple[list[str], list[dict]]:
    """文件里的第一张表：表头和每一行（按表头名取值）。"""
    for table in md_tables(read(path)):
        head = table[0]
        return head, [{h: (r[i] if i < len(r) else "") for i, h in enumerate(head)} for r in table[1:]]
    return [], []


def next_id(existing: list[str], prefix: str) -> str:
    nums = [int(m.group(1)) for x in existing if (m := re.match(rf"{prefix}(\d+)", x))]
    return f"{prefix}{(max(nums) + 1) if nums else 1:03d}"


def safe_name(title: str) -> str:
    return re.sub(r'[\\/:*?"<>|\s]+', "_", title.strip())[:40] or "未命名"


def bigrams(text: str) -> set[str]:
    t = re.sub(r"\s+", "", text)
    return {t[i:i + 2] for i in range(len(t) - 1)}


EXEC_RESULTS = ("通过", "失败", "阻塞", "未测")


LEAK_PATTERNS = [
    (r"cloud1-[a-z0-9]{6,}", "微信云开发环境 ID"),
    (r"\bwx[0-9a-f]{16}\b", "微信 AppID"),
    (r"(?i)(secret|api[_-]?key|access[_-]?token|password)\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{12,}", "疑似密钥"),
    (r"-----BEGIN [A-Z ]*PRIVATE KEY-----", "私钥"),
    (r"\bsk-[A-Za-z0-9_\-]{20,}", "疑似 API Key"),
]
