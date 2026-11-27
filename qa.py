#!/usr/bin/env python3
"""AI 测试管线：资料 → 知识库 → AI 生成测试点 → 人工审核 → 接口自动化 → 缺陷回流 → 指标；可接多家模型、做选型对比。

    python qa.py status      现在该做什么（AI 每次开工先跑这个）
    python qa.py next        下一件事的详细做法
    python qa.py --help      全部命令

本文件只用 Python 标准库；跑接口用例另需 pytest、httpx，示例服务另需 fastapi、uvicorn，调 Claude 接口另需 anthropic（python qa.py setup）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

KIT = Path(__file__).resolve().parent
sys.path.insert(0, str(KIT))
import qa_models  # noqa: E402

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


# ───────────────────────── 通用 ─────────────────────────

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


def md_tables(text: str) -> list[list[list[str]]]:
    """拆出 Markdown 表格：每张表是若干行，每行是若干格（去掉分隔行）。"""
    tables, cur = [], []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("|"):
            cells = [c.strip() for c in s.strip("|").split("|")]
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


# ───────────────────────── 工作区 ─────────────────────────

def load_local() -> dict:
    return json.loads(read(LOCAL)) if LOCAL.exists() else {"current": None, "workspaces": []}


def save_local(data: dict) -> None:
    write(LOCAL, json.dumps(data, ensure_ascii=False, indent=2))


def find_ws(args) -> Path | None:
    if getattr(args, "ws", None):
        return Path(args.ws).resolve()
    here = Path.cwd().resolve()
    for p in [here, *here.parents]:
        if (p / CONFIG).exists():
            return p
    cur = load_local().get("current")
    return Path(cur) if cur and (Path(cur) / CONFIG).exists() else None


class Ws:
    def __init__(self, root: Path):
        self.root = root
        if not (root / CONFIG).exists():
            raise QaError(f"{root} 不是工作区（没有 {CONFIG}）")
        self.cfg = json.loads(read(root / CONFIG))
        if "target" in self.cfg and "targets" not in self.cfg:  # 兼容早期只有一个被测服务的写法
            self.cfg["targets"] = {"default": {**DEFAULT_TARGET, **self.cfg.pop("target")}}
        self.cfg.setdefault("targets", {"default": dict(DEFAULT_TARGET)})
        self.state_path = root / ".qa" / "state.json"
        self.sources_path = root / ".qa" / "sources.json"
        self.state = json.loads(read(self.state_path)) if self.state_path.exists() else {}
        self.state.setdefault("wiki", {"commits": []})
        self.state.setdefault("reqs", {})
        self.state.setdefault("autotest", {"runs": []})
        self.sources = json.loads(read(self.sources_path)) if self.sources_path.exists() else {}
        self.raw, self.wiki, self.reqs = root / "raw", root / "wiki", root / "reqs"
        self.defects, self.auto = root / "defects", root / "autotest"

    def save(self) -> None:
        write(self.state_path, json.dumps(self.state, ensure_ascii=False, indent=2))
        write(self.sources_path, json.dumps(self.sources, ensure_ascii=False, indent=2))

    def save_cfg(self) -> None:
        write(self.root / CONFIG, json.dumps(self.cfg, ensure_ascii=False, indent=2))

    def rel(self, p: Path) -> str:
        return p.resolve().relative_to(self.root).as_posix()

    def raw_files(self) -> list[Path]:
        return sorted(p for p in self.raw.rglob("*") if p.is_file() and not p.name.startswith("."))

    def wiki_pages(self) -> list[Path]:
        return sorted(p for p in self.wiki.rglob("*.md") if p.is_file())

    def modules(self) -> list[str]:
        d = self.wiki / "modules"
        return sorted(p.stem for p in d.glob("*.md")) if d.exists() else []

    def req_dirs(self) -> list[Path]:
        return sorted(p for p in self.reqs.glob("R*") if p.is_dir()) if self.reqs.exists() else []

    def req_dir(self, rid: str) -> Path:
        rid = rid.upper()
        hit = [p for p in self.req_dirs() if p.name.split("-")[0] == rid]
        if not hit:
            raise QaError(f"没有需求 {rid}。现有：{', '.join(p.name for p in self.req_dirs()) or '无'}")
        return hit[0]

    def req_meta(self, rid: str) -> dict:
        return self.state["reqs"].setdefault(rid.upper(), {})

    def defect_files(self) -> list[Path]:
        return sorted(self.defects.glob("D*.md")) if self.defects.exists() else []

    def target(self, name: str | None = None) -> tuple[str, dict]:
        ts = self.cfg["targets"]
        if name is None:
            name = "default" if "default" in ts else (next(iter(ts)) if len(ts) == 1 else None)
        if name is None or name not in ts:
            raise QaError(f"要指定被测服务（--target），可选：{', '.join(ts)}")
        return name, {**DEFAULT_TARGET, **ts[name]}

    def api_files(self, tname: str | None = None) -> list[Path]:
        names = [tname] if tname else list(self.cfg["targets"])
        files = []
        for n in names:
            d = self.auto / self.target(n)[1]["api"]
            files += [p for p in d.rglob("*") if p.is_file() and p.name != ".gitkeep"] if d.exists() else []
        return files

    def template(self, name: str) -> str:
        p = self.root / "templates" / name
        return read(p if p.exists() else KIT / "templates" / name)

    def prompt_text(self, rel: str) -> str:
        p = self.root / "prompts" / rel
        return read(p if p.exists() else KIT / "prompts" / rel)

    @property
    def access(self) -> str:
        return self.cfg.get("access") if self.cfg.get("access") in ACCESS else "interface"

    def values(self) -> dict:
        name, note = ACCESS[self.access]
        return {"qa": qa_cmd(), "kit": str(KIT), "ws": str(self.root), "project": self.cfg.get("project", ""),
                "language": self.cfg.get("language") or "中文", "access_name": name, "access_note": note,
                "source_paths": "、".join(self.cfg.get("source_paths") or []) or "（没登记）",
                "risk_categories": "、".join(self.cfg.get("risk_categories") or DEFAULT_RISKS)}


def ws_from_args(args) -> Ws:
    root = find_ws(args)
    if root is None:
        raise QaError("找不到工作区。在工作区文件夹里运行，或加 --ws <文件夹>；还没有就先 python qa.py init <文件夹> --name <项目名>")
    return Ws(root)


def run_hook(ws: Ws, event: str, extra: dict | None = None) -> None:
    cmd = (ws.cfg.get("hooks") or {}).get(event)
    if not cmd:
        return
    env = sub_env({"QA_EVENT": event, "QA_WS": str(ws.root), **{k: str(v) for k, v in (extra or {}).items()}})
    r = subprocess.run(cmd, shell=True, cwd=ws.root, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
    out(f"（钩子 {event}：退出码 {r.returncode}{'，' + (r.stdout or r.stderr).strip()[-200:] if (r.stdout or r.stderr).strip() else ''}）")


# ───────────────────────── git ─────────────────────────

def in_git_repo(path: Path) -> bool:
    return run(["git", "rev-parse", "--is-inside-work-tree"], cwd=path).stdout.strip() == "true"


def git(ws: Ws, *args: str, check: bool = True):
    r = run(["git", "-c", "core.quotepath=false", *args], cwd=ws.root)
    if check and r.returncode != 0:
        raise QaError(f"git {' '.join(args)} 失败：{(r.stderr or r.stdout).strip()}")
    return r


def git_commit(ws: Ws, msg: str) -> bool:
    """只提交工作区这个文件夹里的改动（工作区放在别的仓库里时也不会带上外面的改动）。"""
    git(ws, "add", "-A", "--", ".")
    if not git(ws, "diff", "--cached", "--name-only", "--", ".").stdout.strip():
        return False
    git(ws, "commit", "-q", "-m", msg, "--", ".")
    return True


# ───────────────────────── doctor / setup ─────────────────────────

def doctor_report() -> tuple[bool, list[tuple[str, bool, str]]]:
    rows = []
    ok_py = sys.version_info >= (3, 10)
    rows.append(("Python 3.10 以上", ok_py, f"当前 {sys.version.split()[0]}（{sys.executable}）"))
    has_git = shutil.which("git") is not None
    rows.append(("git", has_git, "已安装" if has_git else "没装：到 https://git-scm.com 下载安装"))
    for group, pkgs, need in (("跑接口用例", ("pytest", "httpx"), True), ("示例服务（可选）", ("fastapi", "uvicorn"), False),
                              ("调 Claude 接口（可选）", ("anthropic",), False)):
        missing = []
        for mod in pkgs:
            try:
                __import__(mod)
            except Exception:
                missing.append(mod)
        note = "都装好了" if not missing else f"缺 {'、'.join(missing)}：运行 python qa.py setup"
        rows.append((f"{group}：{'、'.join(pkgs)}", not missing or not need, note))
    clis = [c for c in ("claude", "codex", "gemini") if shutil.which(c)]
    rows.append(("命令行 AI 工具（可选）", True, "、".join(clis) if clis else "没找到 claude / codex / gemini，不影响使用"))
    rows.append(("模型清单（可选）", True, "已有 models.local.json" if (KIT / "models.local.json").exists()
                 else "没有 models.local.json：要接模型 API 时照 models.example.json 建一份"))
    if os.name == "nt":
        rows.append(("PYTHONUTF8=1（建议）", True, "已设置" if os.environ.get("PYTHONUTF8") == "1"
                     else "没设：运行 setx PYTHONUTF8 1 后重开终端，中文不容易乱码"))
    return all(good for _, good, _ in rows), rows


def cmd_doctor(args) -> int:
    ok, rows = doctor_report()
    out("环境检查：")
    for name, good, note in rows:
        out(f"  {'✅' if good else '❌'} {name}：{note}")
    out("")
    out("结论：" + ("可以用了。" if ok else "有必需项没通过，按提示处理后再运行 python qa.py doctor。"))
    return 0 if ok else 1


def cmd_setup(args) -> int:
    out("安装依赖：pip install -r requirements.txt")
    r = subprocess.run([sys.executable, "-m", "pip", "install", "-r", str(KIT / "requirements.txt")])
    out("安装完成，运行 python qa.py doctor 确认。" if r.returncode == 0 else "安装失败，看上面的报错（常见是没联网或 pip 太旧）。")
    return r.returncode


# ───────────────────────── init / use ─────────────────────────

def cmd_init(args) -> int:
    root = Path(args.dir).resolve()
    try:
        root.relative_to(KIT)
        if not args.force:
            out("工作区不要放在框架文件夹里面：框架要上传 GitHub，工作区里是项目的私有资料。换个位置，例如 D:\\qa-workspace\\项目名。")
            return 1
    except ValueError:
        pass
    if (root / CONFIG).exists():
        out(f"{root} 已经是工作区了。要切换到它：python qa.py use \"{root}\"")
        return 1
    if root.exists() and any(root.iterdir()) and not args.force:
        out(f"{root} 不是空文件夹。换一个空文件夹，或确认要放在这里就加 --force。")
        return 1
    if shutil.which("git") is None:
        out("没有 git，先安装（python qa.py doctor 会提示）。")
        return 1
    name = args.name or ("示例项目" if args.example else root.name)
    root.mkdir(parents=True, exist_ok=True)
    target = dict(DEFAULT_TARGET)
    if args.example:
        target.update({"base_url": "http://127.0.0.1:8000", "start_cmd": f'"{sys.executable}" "{EXAMPLE / "server.py"}" --port 8000',
                       "start_cwd": str(EXAMPLE)})
    access = getattr(args, "access", None) or "interface"
    cfg = {"project": name, "framework": str(KIT), "language": "中文", "access": access, "source_paths": [],
           "risk_categories": DEFAULT_RISKS, "targets": {"default": target}, "stage_models": {}, "models": {},
           "context_chars": 150000, "hooks": {}}
    write(root / CONFIG, json.dumps(cfg, ensure_ascii=False, indent=2))
    ws = Ws(root)
    rules = render(read(KIT / "templates" / "workspace_AGENTS.md"), ws.values())
    for fname in ("AGENTS.md", "CLAUDE.md", "GEMINI.md"):
        write(root / fname, rules)
    for t in ("测试点模板.md", "需求模板.md", "缺陷模板.md", "审核表.md"):
        write(root / "templates" / t, read(KIT / "templates" / t))
    write(root / "autotest" / "conftest.py", read(KIT / "templates" / "conftest.py"))
    write(root / "prompts" / "README.md", "放在这里的同名文件会覆盖框架自带的提示词（框架的在 " + str(KIT / "prompts") + "）。\n")
    for d in ("raw", "wiki", "reqs", "defects", "autotest/api", "autotest/tests", "autotest/runs", "metrics"):
        write(root / d / ".gitkeep", "")
    write(root / ".gitignore", "__pycache__/\n.pytest_cache/\n*.pyc\n")
    # Windows 上 git 默认转换换行符，每次提交都刷警告；统一用 LF
    write(root / ".gitattributes", "* text=auto eol=lf\n*.png binary\n*.jpg binary\n")
    ws.save()
    nested = in_git_repo(root)
    if not nested:
        git(ws, "init", "-q")
    if not run(["git", "config", "user.email"], cwd=root).stdout.strip():
        git(ws, "config", "user.email", "qa@localhost")
        git(ws, "config", "user.name", "qa")
    if args.example:
        ingest_paths(ws, sorted((EXAMPLE / "raw").glob("*.md")), into="raw", quiet=True)
        ingest_paths(ws, [EXAMPLE / "api.md"], into="api", quiet=True)
        new_req(ws, "广告宝箱", EXAMPLE / "需求-广告宝箱.md", ["广告领奖"], quiet=True)
        ws.save()
    git_commit(ws, f"建立工作区：{name}")
    if not args.no_register:
        local = load_local()
        local["current"] = str(root)
        if str(root) not in local["workspaces"]:
            local["workspaces"].append(str(root))
        save_local(local)
    out(f"工作区建好了：{root}（项目：{name}，环境：{ACCESS[access][0]}{'，已放好示例资料、接口文档和一个需求' if args.example else ''}）")
    if nested:
        out("注意：这个文件夹在别的 git 仓库里面，没有另建仓库；qa.py 提交时只提交这个文件夹里的改动。")
    out("")
    out(f"下一步：在这个文件夹里开一个 AI 会话（它会读 AGENTS.md），或在这里运行 {qa_cmd()} status")
    return 0


def cmd_use(args) -> int:
    root = Path(args.dir).resolve()
    Ws(root)
    local = load_local()
    local["current"] = str(root)
    if str(root) not in local["workspaces"]:
        local["workspaces"].append(str(root))
    save_local(local)
    out(f"当前工作区：{root}")
    return 0


# ───────────────────────── 被测服务 ─────────────────────────

def cmd_target(args) -> int:
    ws = ws_from_args(args)
    if args.action == "list":
        for name, t in ws.cfg["targets"].items():
            t = {**DEFAULT_TARGET, **t}
            out(f"  {name}：{t['base_url'] or '（地址没配）'}　用例 autotest/{t['tests']}　接口文档 autotest/{t['api']}" +
                (f"　启动：{t['start_cmd']}" if t["start_cmd"] else "") + f"　{'在线' if healthy(t) else '没在跑'}")
        return 0
    name = args.name or "default"
    t = {**DEFAULT_TARGET, **ws.cfg["targets"].get(name, {})}
    if name != "default" and name not in ws.cfg["targets"]:
        t.update({"tests": f"tests/{name}", "api": f"api/{name}"})
    if args.health:
        if re.match(r"^[A-Za-z]:[\\/]", args.health):
            out(f"健康检查路径成了 {args.health}：在 Windows 的 Git Bash 里，以 / 开头的参数会被自动改成 Windows 路径。"
                "写成不带斜杠的 health（框架会补上 /），或在 PowerShell 里运行。")
            return 1
        args.health = "/" + args.health.lstrip("/")
    for key, val in (("base_url", args.url), ("start_cmd", args.start), ("start_cwd", args.cwd), ("health_path", args.health)):
        if val is not None:
            t[key] = val
    ws.cfg["targets"][name] = t
    ws.save_cfg()
    for sub in (t["tests"], t["api"]):
        write(ws.auto / sub / ".gitkeep", "")
    out(f"被测服务 {name} 已保存：{t['base_url'] or '（地址没配）'}，用例放 autotest/{t['tests']}，接口文档放 autotest/{t['api']}")
    return 0


# ───────────────────────── 导入资料 ─────────────────────────

def redact_text(text: str) -> str:
    for pat, what in LEAK_PATTERNS:
        text = re.sub(pat, f"【已脱敏：{what}】", text)
    return text


def copy_source(ws: Ws, src: Path, key: str, redact: bool | None = None) -> str:
    """把一份资料复制到工作区的 key 位置并记下来源（要脱敏的复制时打码）。返回 added / changed / same。"""
    dst = ws.root / key
    existed = dst.exists()
    info = ws.sources.get(key, {})
    flipped = redact is not None and bool(redact) != bool(info.get("redact"))
    if redact is not None:
        info["redact"] = redact
    info["from"] = str(src)
    ws.sources[key] = info
    h = sha256(src)
    if existed and not flipped and (info.get("src_hash") == h if info.get("src_hash") else sha256(dst) == h):
        return "same"
    if info.get("redact"):
        write(dst, redact_text(read(src)))
    else:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
    info.update({"src_hash": h, "imported": now_iso()})
    return "changed" if existed else "added"


def ingest_paths(ws: Ws, paths: list[Path], into: str = "raw", quiet: bool = False, tname: str | None = None, redact: bool = False):
    base = ws.raw if into == "raw" else ws.auto / ws.target(tname)[1]["api"]
    added = changed = 0
    skipped, leaks = [], []
    for src in paths:
        src = src.resolve()
        if not src.exists():
            raise QaError(f"找不到 {src}")
        files = [src] if src.is_file() else sorted(p for p in src.rglob("*") if p.is_file())
        for f in files:
            if any(part in (".git", "node_modules", "__pycache__") for part in f.parts):
                continue
            if f.suffix.lower() not in TEXT_EXT or f.stat().st_size > MAX_FILE:
                skipped.append(str(f))
                continue
            rel_inside = f.name if src.is_file() else (Path(src.name) / f.relative_to(src)).as_posix()
            key = (base / rel_inside).relative_to(ws.root).as_posix()
            r = copy_source(ws, f, key, redact=True if redact else None)
            added += r == "added"
            changed += r == "changed"
            text = read(ws.root / key)
            leaks += [f"{key}：{what}" for pat, what in LEAK_PATTERNS if re.search(pat, text)]
    if not quiet:
        out(f"导入完成：新增 {added} 份，更新 {changed} 份，放在 {base}" + ("（已脱敏）" if redact else ""))
        if skipped:
            out(f"跳过 {len(skipped)} 份（不是文本文件或超过 2 MB）：" + "；".join(skipped[:5]) + ("…" if len(skipped) > 5 else ""))
        if leaks:
            out(f"⚠️ 导入的资料里有敏感信息（{'；'.join(leaks[:5])}）。原文件不用改，重新导入时加 --redact 会在副本里打码，以后 --refresh 也保持打码。")
    return added, changed, skipped


def cmd_ingest(args) -> int:
    ws = ws_from_args(args)
    if args.refresh:
        changed, missing = 0, []
        for key, info in sorted(ws.sources.items()):
            src = Path(info.get("from", ""))
            if not src.is_file():
                missing.append(key)
                continue
            changed += copy_source(ws, src, key) == "changed"
        out(f"按原来源刷新：更新 {changed} 份。" + (f"原文件已不存在：{'；'.join(missing[:5])}" if missing else ""))
    elif args.har:
        src = Path(args.har).resolve()
        try:
            har = json.loads(read(src))
        except Exception as e:
            raise QaError(f"读不了 {src}：{e}（要浏览器或抓包工具导出的 HAR 文件）") from e
        _, t = ws.target(args.target)
        dst = ws.auto / t["api"] / f"抓包_{src.stem}.md"
        write(dst, har_to_markdown(har, src.name))
        ws.sources[ws.rel(dst)] = {"from": str(src), "imported": now_iso(), "har": True}
        out(f"已把抓包整理成接口文档：{dst}（敏感信息已打码）。字段含义、取值范围请人补充；接着可以 {qa_cmd()} prompt tests-plan")
    else:
        if not args.paths:
            out("要导入什么？例如：python qa.py ingest D:\\项目\\docs\\策划案.md  或  --refresh 按原来源刷新")
            return 1
        ingest_paths(ws, [Path(p) for p in args.paths], into="api" if args.api else "raw", tname=args.target, redact=args.redact)
    ws.save()
    out(f"之后运行 {qa_cmd()} status 看知识库要不要更新。")
    return 0


def raw_changes(ws: Ws) -> dict:
    commits = ws.state["wiki"]["commits"]
    last = commits[-1]["sources"] if commits else {}
    now = {ws.rel(p): sha256(p) for p in ws.raw_files()}
    return {"new": sorted(k for k in now if k not in last), "changed": sorted(k for k in now if k in last and last[k] != now[k]),
            "deleted": sorted(k for k in last if k not in now), "built": bool(commits)}


# ───────────────────────── 知识库 ─────────────────────────

COUPLING = "耦合关系.md"


def split_names(s: str) -> list[str]:
    return [x.strip() for x in re.split(r"[、,，/；;]", s) if x.strip() and x.strip() not in ("—", "-", "无")]


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


def wiki_check(ws: Ws) -> tuple[list[str], dict]:
    problems = []
    index = ws.wiki / "index.md"
    pages = ws.wiki_pages()
    if not index.exists():
        problems.append("没有 wiki/index.md")
    index_text = read(index) if index.exists() else ""
    todo = 0
    for p in pages:
        rel = p.relative_to(ws.wiki).as_posix()
        text = read(p)
        todo += text.count("待确认")
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
    return problems, {"pages": len(pages), "todo": todo, "coupling": len(coupling_rows(ws))}


def cmd_wiki(args) -> int:
    ws = ws_from_args(args)
    problems, stats = wiki_check(ws)
    if args.action == "check":
        out(f"知识库：{stats['pages']} 页，“待确认” {stats['todo']} 处。")
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
    snapshot = {ws.rel(p): sha256(p) for p in ws.raw_files()}
    ws.state["wiki"]["commits"].append({"time": now_iso(), "message": args.message, "sources": snapshot, "feedback": marked})
    ws.save()
    committed = git_commit(ws, f"知识库：{args.message}")
    out(f"已提交知识库（{stats['pages']} 页，基于 {len(snapshot)} 份资料）" + ("" if committed else "，没有文件改动，只记录了资料快照") +
        (f"；标记为已回流：{', '.join(marked)}" if marked else "") + "。")
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


# ───────────────────────── 需求、测试点、审核 ─────────────────────────

def next_id(existing: list[str], prefix: str) -> str:
    nums = [int(m.group(1)) for x in existing if (m := re.match(rf"{prefix}(\d+)", x))]
    return f"{prefix}{(max(nums) + 1) if nums else 1:03d}"


def safe_name(title: str) -> str:
    return re.sub(r'[\\/:*?"<>|\s]+', "_", title.strip())[:40] or "未命名"


def new_req(ws: Ws, title: str, src: Path | None, modules: list[str], quiet: bool = False) -> Path:
    rid = next_id([p.name for p in ws.req_dirs()], "R")
    d = ws.reqs / f"{rid}-{safe_name(title)}"
    write(d / "需求.md", read(src) if src else render(ws.template("需求模板.md"), {"title": title}))
    meta = ws.req_meta(rid)
    meta.update({"title": title, "modules": modules, "created": now_iso()})
    if src:
        meta["from"] = str(src.resolve())
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
        mods = [m.strip() for m in (args.module or "").replace("，", ",").split(",") if m.strip()]
        unknown = [m for m in mods if ws.modules() and m not in ws.modules()]
        d = new_req(ws, args.title, Path(args.src) if args.src else None, mods)
        ws.save()
        if unknown:
            out(f"提醒：知识库里还没有这些模块页：{'、'.join(unknown)}（现有：{'、'.join(ws.modules())}）")
        out(f"下一步：{qa_cmd()} prompt testpoints {d.name.split('-')[0]}（让工作区里的 AI 生成），或 {qa_cmd()} gen testpoints {d.name.split('-')[0]} --model <模型>（直接调模型接口）")
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
        for r in table[1:]:
            if col < len(r) and r[col]:
                pts.append({"no": get(r, "编号") or str(len(pts) + 1), "point": r[col].replace("|", "/"),
                            "risk": get(r, "风险"), "type": get(r, "类型"), "basis": get(r, "依据"), "verify": get(r, "验证")})
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


def make_review(ws: Ws, src: Path, dst: Path, title: str) -> int:
    pts = parse_points(src)
    rows = "\n".join(f"| {p['no']} | {p['point']} |  |  |  |" for p in pts) or "| 1 | （没解析出测试点，把 AI 的测试点手动抄进来） |  |  |  |"
    write(dst, render(ws.template("审核表.md"), {"req": title, "rows": rows}))
    return len(pts)


def cmd_review(args) -> int:
    ws = ws_from_args(args)
    d = ws.req_dir(args.req)
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
    ws.save()
    git_commit(ws, f"需求 {d.name}：测试点定稿")
    out(f"已定稿：{d / '最终测试点.md'}（{len(final)} 条）。AI 生成 {st['generated']} 条，"
        f"采纳率 {pct(st['accepted'] + st['modified'], st['generated'])}，编造 {st['fabricated']} 条，人工补充 {st['added']} 条，审核 {meta['审核耗时（分钟）']} 分钟。")
    for g in gaps:
        out(f"  ⚠️ 耦合提醒：{g}（已定稿，可以重新审核补上，或确认确实不相关）")
    run_hook(ws, "after_accept", {"QA_REQ": rid})
    return 0


def req_rows(ws: Ws) -> list[dict]:
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


# ───────────────────────── 缺陷 ─────────────────────────

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


# ───────────────────────── 交给 AI 的任务说明 ─────────────────────────

PROMPTS = ("wiki-build", "wiki-update", "wiki-lint", "testpoints", "tests-plan", "tests-write", "triage", "feedback")


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
    if name in ("tests-plan", "tests-write"):
        tname, t = ws.target(args.target)
        v.update({"target": tname, "api_dir": f"autotest/{t['api']}", "tests_dir": f"autotest/{t['tests']}",
                  "target_opt": "" if tname == "default" else f" --target {tname}",
                  "focus": (f"重点覆盖 {v['req_dir']}/最终测试点.md 里能用接口验证的测试点；" if args.req else "")})
        if name == "tests-plan" and not ws.api_files(tname):
            out(f"autotest/{t['api']}/ 里还没有接口文档。先导入：python qa.py ingest <接口文档> --api" + v["target_opt"])
            return 1
    if name == "wiki-update":
        ch = raw_changes(ws)
        if not ch["built"]:
            out("还没生成过知识库，用 wiki-build。")
            return 1
        lines = [f"- 新增：{x}" for x in ch["new"]] + [f"- 有改动：{x}" for x in ch["changed"]] + [f"- 已删除：{x}" for x in ch["deleted"]]
        if not lines:
            out("资料自上次提交知识库以来没有变化，不用更新。")
            return 0
        v["changes"] = "\n".join(lines)
    if name == "triage":
        lr = last_run(ws)
        if not lr or not lr["failed"]:
            out("最近一次运行没有失败的用例，不用归因。")
            return 0
        v.update({"run_dir": lr["dir"], "failed": "\n".join(f"- {x}" for x in lr["failed"]), "target": lr.get("target", "default")})
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


# ───────────────────────── 直接调模型：生成、对比、用量 ─────────────────────────

def bigrams(text: str) -> set[str]:
    t = re.sub(r"\s+", "", text)
    return {t[i:i + 2] for i in range(len(t) - 1)}


def wiki_context(ws: Ws, query: str, modules: list[str]) -> tuple[str, list[str]]:
    """按预算挑知识页：目录、风险清单、标注模块的页优先，其余按和需求的相关度排。"""
    budget = int(ws.cfg.get("context_chars") or 150000)
    pages = ws.wiki_pages()
    if not pages:
        raise QaError("知识库是空的，先生成知识库（prompt wiki-build）")
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
    v = {**ws.values(), **req_values(ws, rid)}
    d = ws.root / v["req_dir"]
    req_text = read(d / "需求.md")
    wiki, skipped = wiki_context(ws, req_text, ws.req_meta(v["req"]).get("modules") or [])
    v.update({"requirement": req_text.strip(), "wiki": wiki, "template": ws.template("测试点模板.md").strip()})
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


def cmd_gen(args) -> int:
    ws = ws_from_args(args)
    reg = qa_models.load_registry(ws.cfg)
    try:
        label, m = qa_models.pick_model(reg, args.model, args.what, ws.cfg)
    except qa_models.ModelError as e:
        raise QaError(str(e)) from e
    if args.what == "testpoints":
        if not args.req:
            out("要指定需求编号：python qa.py gen testpoints R001 --model <模型>")
            return 1
        d = ws.req_dir(args.req)
        rid = d.name.split("-")[0]
        if (d / "测试点.md").exists() and not args.force:
            out(f"{d / '测试点.md'} 已经有了，要覆盖就加 --force。")
            return 1
        system, user, skipped = build_testpoints_prompt(ws, rid)
        out(f"调用 {label}（{m.get('provider')} / {m.get('model', m.get('command', ''))}）生成 {rid} 的测试点……" +
            (f"（知识库太大，没放进去：{'、'.join(skipped[:5])}）" if skipped else ""))
        r = qa_models.call(m, system, user, cwd=ws.root)
        log_call(ws, "testpoints", label, r, req=rid)
        if not r.ok:
            out(f"失败：{r.error}")
            return 1
        write(d / "测试点.md", f"<!-- 生成：{label}（{r.model_id}），{now_iso()}，{usage_line(r)} -->\n\n" + strip_fence(r.text).strip() + "\n")
        ws.req_meta(rid)["model"] = label
        ws.save()
        n = len(parse_points(d / "测试点.md"))
        out(f"已生成 {d / '测试点.md'}（{n} 条）。{usage_line(r)}" + ("。⚠️ 输出被截断了，调大 max_tokens 再试" if r.extra.get("truncated") else ""))
        if not n:
            out("⚠️ 没解析出测试点表格，打开文件看看模型输出了什么。")
        for g in coupling_gaps(ws, rid, parse_points(d / "测试点.md")):
            out(f"  ⚠️ 耦合提醒：{g}")
        out(f"下一步：{qa_cmd()} review {rid}，请人审核。")
        return 0
    # triage
    lr = last_run(ws)
    if not lr or not lr["failed"]:
        out("最近一次运行没有失败的用例，不用归因。")
        return 0
    run_dir = ws.root / lr["dir"]
    tests_src = []
    for f in sorted({x.split("::")[0] for x in lr["failed"]}):
        cand = [p for p in ws.auto.rglob("*.py") if p.stem == f.split(".")[-1]]
        tests_src += [f"===== {ws.rel(p)} =====\n{read(p)[:20000]}" for p in cand[:1]]
    risks = ws.wiki / "risks" / "风险清单.md"
    v = {**ws.values(), "failed": "\n".join(f"- {x}" for x in lr["failed"]),
         "pytest_output": read(run_dir / "pytest输出.txt")[-30000:] if (run_dir / "pytest输出.txt").exists() else "",
         "tests_src": "\n".join(tests_src), "risks": read(risks)[:20000] if risks.exists() else "（没有风险清单）",
         "api_doc": "\n".join(read(p)[:30000] for p in ws.api_files(lr.get("target")))}
    system = render(ws.prompt_text("api/system.md"), v).strip()
    user = render(ws.prompt_text("api/triage.md"), v).strip()
    out(f"调用 {label} 分析 {len(lr['failed'])} 个失败……")
    r = qa_models.call(m, system, user, cwd=ws.root)
    log_call(ws, "triage", label, r, run=lr["dir"])
    if not r.ok:
        out(f"失败：{r.error}")
        return 1
    write(run_dir / "归因.md", f"<!-- 生成：{label}（{r.model_id}），{now_iso()}，{usage_line(r)}。要人确认 -->\n\n" + strip_fence(r.text).strip() + "\n")
    out(f"已写 {run_dir / '归因.md'}。{usage_line(r)}。请人确认，真缺陷用 {qa_cmd()} defect new 登记。")
    return 0


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
                e["points"] = make_review(ws, f, bdir / f"审核_{label}.md", f"{d.name} · {label}")
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


# ───────────────────────── 环境权限、手工执行、抓包、改动影响 ─────────────────────────

def cmd_access(args) -> int:
    ws = ws_from_args(args)
    if args.level:
        ws.cfg["access"] = args.level
        if args.source:
            ws.cfg["source_paths"] = [str(Path(p).resolve()) for p in args.source]
        ws.save_cfg()
        for fname in ("AGENTS.md", "CLAUDE.md", "GEMINI.md"):  # 工作区规则里写着环境说明，跟着更新
            write(ws.root / fname, render(read(KIT / "templates" / "workspace_AGENTS.md"), Ws(ws.root).values()))
        ws = Ws(ws.root)
    name, note = ACCESS[ws.access]
    out(f"环境：{name}（{ws.access}）。{note}")
    if ws.access == "source":
        out(f"代码位置：{ws.values()['source_paths']}" + ("" if ws.cfg.get("source_paths") else "，用 access source --source <代码文件夹> 登记"))
    return 0


EXEC_RESULTS = ("通过", "失败", "阻塞", "未测")


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


# ───────────────────────── 接口自动化 ─────────────────────────

def healthy(t: dict) -> bool:
    url = os.environ.get("QA_TARGET_URL_OVERRIDE") or t.get("base_url") or ""
    if not url:
        return False
    path = t.get("health_path") or ""
    if path and url.startswith(("http://", "https://")):
        try:
            with urllib.request.urlopen(url.rstrip("/") + path, timeout=2) as r:
                return 200 <= r.status < 500
        except urllib.error.HTTPError as e:
            return e.code < 500
        except Exception:
            return False
    # 非 HTTP 协议（tcp://、ws:// 等）或没配健康检查路径：只看端口能不能连上
    m = re.match(r"(\w+)://\[?([^/\]]+?)\]?(?::(\d+))?(?:/|$)", url)
    if not m:
        return False
    port = int(m.group(3) or {"https": 443, "wss": 443}.get(m.group(1), 80))
    with socket.socket() as s:
        s.settimeout(1)
        return s.connect_ex((m.group(2), port)) == 0


class Target:
    """按被测服务的 start_cmd 启动它，用完关掉（连子进程一起）。"""

    def __init__(self, ws: Ws, t: dict):
        self.ws, self.t, self.proc, self.log = ws, t, None, None

    def __enter__(self):
        if not self.t.get("start_cmd"):
            raise QaError("这个被测服务没配 start_cmd，不知道怎么启动。手动启动后去掉 --start 再跑。")
        self.log = tempfile.TemporaryFile()
        # 标准输入设为空：被测服务用不到它，免得和调用方抢输入
        kw = {"cwd": self.t.get("start_cwd") or str(self.ws.root), "env": sub_env(), "stdin": subprocess.DEVNULL,
              "stdout": self.log, "stderr": subprocess.STDOUT}
        self.proc = subprocess.Popen(self.t["start_cmd"], shell=True, **({} if os.name == "nt" else {"start_new_session": True}), **kw)
        for _ in range(240):
            if self.proc.poll() is not None:
                self.log.seek(0)
                raise QaError("被测服务没起来：" + self.log.read().decode("utf-8", "replace").strip()[-800:])
            if healthy(self.t):
                return self
            time.sleep(0.25)
        self.log.seek(0)
        tail = self.log.read().decode("utf-8", "replace").strip()[-800:]
        self.__exit__()
        raise QaError(f"被测服务 60 秒内没响应（检查 base_url {self.t.get('base_url')} 和 health_path {self.t.get('health_path')}）。服务输出：{tail or '（没有输出）'}")

    def __exit__(self, *exc):
        if self.proc and self.proc.poll() is None:
            if os.name == "nt":
                run(["taskkill", "/F", "/T", "/PID", str(self.proc.pid)])
            else:
                try:
                    os.killpg(self.proc.pid, signal.SIGTERM)
                except Exception:
                    self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if self.log:
            self.log.close()


def run_pytest(ws: Ws, tname: str, t: dict, junit: Path, kexpr: str | None) -> dict:
    tests_dir = ws.auto / t["tests"]
    cmd = [sys.executable, "-m", "pytest", str(tests_dir), "-q", "-p", "no:cacheprovider", "--rootdir", str(ws.auto), "--junitxml", str(junit)]
    if kexpr:
        cmd += ["-k", kexpr]
    if tname == "default" and len(ws.cfg["targets"]) > 1:
        cmd += [f"--ignore={ws.auto / x['tests']}" for n, x in ws.cfg["targets"].items() if n != "default" and x.get("tests", "").startswith("tests/")]
    r = run(cmd, cwd=ws.auto, env=sub_env({"QA_TARGET_URL": t["base_url"], "QA_TARGET": tname}), timeout=3600)
    res = {"exit": r.returncode, "tests": [], "failed": [], "output": (r.stdout or "") + (r.stderr or "")}
    if junit.exists():
        for tc in ET.parse(junit).getroot().iter("testcase"):
            tid = f"{tc.get('classname')}::{tc.get('name')}"
            res["tests"].append(tid)
            if tc.find("failure") is not None or tc.find("error") is not None:
                res["failed"].append(tid)
    return res


def cmd_autotest(args) -> int:
    ws = ws_from_args(args)
    tname, t = ws.target(args.target)
    if args.action == "start":
        if not t.get("start_cmd"):
            out(f"被测服务 {tname} 没配 start_cmd。按你们项目平时的方式启动服务即可。")
            return 1
        if healthy(t):
            out(f"被测服务已经在跑：{t['base_url']}")
            return 0
        out(f"启动被测服务 {tname}：{t['start_cmd']}（地址 {t['base_url']}，按 Ctrl+C 停止）")
        try:
            return subprocess.call(t["start_cmd"], shell=True, cwd=t.get("start_cwd") or str(ws.root), env=sub_env())
        except KeyboardInterrupt:
            return 0
    tests = sorted((ws.auto / t["tests"]).rglob("test_*.py"))
    if not tests:
        out(f"autotest/{t['tests']}/ 里还没有 test_*.py。先让 AI 写：{qa_cmd()} prompt tests-plan")
        return 1
    if not t["base_url"]:
        out(f"被测服务 {tname} 的地址没配：{qa_cmd()} target set {tname} --url http://…")
        return 1
    st = stamp()
    run_dir = ws.auto / "runs" / st
    run_dir.mkdir(parents=True, exist_ok=True)
    started = None
    if not healthy(t):
        if not args.start:
            out(f"被测服务没在跑（{t['base_url']}）。先启动它（{qa_cmd()} autotest start，或项目平时的启动方式），或者加 --start 让我临时启动。")
            return 1
        started = Target(ws, t).__enter__()
    t0 = time.monotonic()
    try:
        res = run_pytest(ws, tname, t, run_dir / "junit.xml", args.k)
    finally:
        if started:
            started.__exit__()
    write(run_dir / "pytest输出.txt", res["output"])
    runs = ws.state["autotest"]["runs"]
    prev = next((x for x in reversed(runs) if x.get("target", "default") == tname), None)
    failed, prev_failed = set(res["failed"]), set(prev["failed"]) if prev else set()
    summary = {"time": now_iso(), "target": tname, "dir": ws.rel(run_dir), "total": len(res["tests"]), "failed": sorted(failed),
               "new_failed": sorted(failed - prev_failed), "fixed": sorted((prev_failed - failed) & set(res["tests"])),
               "seconds": round(time.monotonic() - t0, 1), "k": args.k}
    runs.append(summary)
    del runs[:-100]
    ws.save()
    lines = [f"# 接口自动化运行 {st}", "", f"被测服务：{tname}（{t['base_url']}）　用例 {summary['total']} 个，失败 {len(failed)} 个，用时 {summary['seconds']} 秒" +
             (f"（筛选：-k {args.k}）" if args.k else ""), ""]
    if prev:
        lines += [f"和上次（{prev['time']}）比：新失败 {len(summary['new_failed'])} 个，修好 {len(summary['fixed'])} 个", ""]
    if failed:
        lines += ["## 失败的用例", ""] + [f"- {x}" + ("（新）" if x in summary["new_failed"] else "") for x in sorted(failed)] + [""]
    if summary["total"] == 0:
        lines += ["没跑出任何用例，看 pytest输出.txt。", ""]
    write(run_dir / "摘要.md", "\n".join(lines))
    out("\n".join(lines[2:]).strip())
    out("")
    if failed:
        out(f"下一步：归因（工作区里的 AI 用 {qa_cmd()} prompt triage，或直接调模型 {qa_cmd()} gen triage --model <模型>），人确认后，真缺陷用 {qa_cmd()} defect new 登记。")
    out(f"明细：{run_dir}")
    run_hook(ws, "after_autotest", {"QA_RUN_DIR": str(run_dir), "QA_FAILED": len(failed), "QA_TOTAL": summary["total"]})
    return 0 if summary["total"] else 1


# ───────────────────────── 指标 ─────────────────────────

def cmd_metrics(args) -> int:
    ws = ws_from_args(args)
    defects = [defect_info(p) for p in ws.defect_files()]
    rows, by_mod = [], {}
    tot = {"generated": 0, "kept": 0, "fabricated": 0, "added": 0, "minutes": 0.0}
    for d in ws.req_dirs():
        rid = d.name.split("-")[0]
        m = ws.req_meta(rid)
        ds = [x for x in defects if x["req"] == rid]
        esc = sum(x["escaped"] for x in ds)
        mods = m.get("modules") or ["（未标模块）"]
        for mod in mods:
            b = by_mod.setdefault(mod, {"reqs": 0, "generated": 0, "kept": 0, "defects": 0, "escaped": 0})
            b["reqs"] += 1
            b["defects"] += len(ds)
            b["escaped"] += esc
        if "generated" in m:
            kept = m["accepted"] + m["modified"]
            for k, val in (("generated", m["generated"]), ("kept", kept), ("fabricated", m["fabricated"]), ("added", m["added"]), ("minutes", m["minutes"])):
                tot[k] += val
            for mod in mods:
                by_mod[mod]["generated"] += m["generated"]
                by_mod[mod]["kept"] += kept
            rows.append(f"| {d.name} | {'、'.join(m.get('modules') or [])} | {m.get('model', '')} | {m['generated']} | {pct(kept, m['generated'])} | "
                        f"{m['fabricated']} | {m['added']} | {m['minutes']:g} | {len(ds)} | {esc} | {pct(esc, len(ds)) if ds else '—'} |")
        else:
            rows.append(f"| {d.name} | {'、'.join(m.get('modules') or [])} | | 未定稿 |  |  |  |  | {len(ds)} | {esc} |  |")
    mod_rows = [f"| {k} | {b['reqs']} | {b['generated']} | {pct(b['kept'], b['generated'])} | {b['defects']} | {b['escaped']} |" for k, b in sorted(by_mod.items())]
    lr = last_run(ws)
    _, wstats = wiki_check(ws)
    ch = raw_changes(ws)
    cs = calls(ws)
    text = f"""# 指标：{ws.cfg.get('project', '')}

生成时间：{now_iso()}

## 测试点（每个需求定稿时记录）

| 需求 | 模块 | 生成模型 | AI 生成 | 采纳率 | 编造 | 人工补充 | 审核分钟 | 缺陷 | 其中漏测 | 漏测率 |
|---|---|---|---|---|---|---|---|---|---|---|
{chr(10).join(rows) or '| （还没有需求） | | | | | | | | | | |'}

合计：AI 生成 {tot['generated']} 条，采纳率 {pct(tot['kept'], tot['generated'])}，编造 {tot['fabricated']} 条，人工补充 {tot['added']} 条，审核共 {tot['minutes']:g} 分钟。

- 采纳率 =（采纳 + 改后采纳）÷ AI 生成；编造 = 否决原因是“不符合规则”的条数（AI 编了资料里没有的规则）
- 漏测 = 缺陷记录里“被最终测试点覆盖：否”；漏测率 = 漏测 ÷ 该需求的缺陷数

## 按功能模块

| 模块 | 需求数 | AI 生成 | 采纳率 | 缺陷 | 漏测 |
|---|---|---|---|---|---|
{chr(10).join(mod_rows) or '| （还没有） | | | | | |'}

## 缺陷

共 {len(defects)} 条，漏测 {sum(x['escaped'] for x in defects)} 条，已回流 {sum(x['escaped'] and x['fed_back'] for x in defects)} 条，自动化发现 {sum('自动化' in x['how'] for x in defects)} 条。

## 手工执行

{chr(10).join(f"- {p.name}（{rid}）：" + "，".join(f"{k} {v}" for k, v in c.items()) + ("（没填完）" if pr else "") for rid, p, pr, c in exec_status(ws)) or '还没有执行记录。'}

## 接口自动化

运行 {len(ws.state['autotest']['runs'])} 次。{f"最近一次 {lr['time']}（{lr.get('target', 'default')}）：用例 {lr['total']} 个，失败 {len(lr['failed'])} 个。" if lr else ''}

## 模型用量（gen、bench 自动记录，usage add 手动补记）

{usage_table(cs) if cs else '还没有记录。'}

## 知识库

{wstats['pages']} 页，“待确认” {wstats['todo']} 处，提交 {len(ws.state['wiki']['commits'])} 次。资料自上次提交后：新增 {len(ch['new'])}、改动 {len(ch['changed'])}、删除 {len(ch['deleted'])}。
"""
    write(ws.root / "metrics" / "指标.md", text)
    out(text)
    out(f"已保存：{ws.root / 'metrics' / '指标.md'}")
    return 0


def cmd_modules(args) -> int:
    ws = ws_from_args(args)
    mods = ws.modules()
    if not mods:
        out("知识库里还没有模块页（wiki/modules/*.md）。模块来自知识库：生成知识库时按业务模块分页。")
        return 0
    defects = [defect_info(p) for p in ws.defect_files()]
    for mod in mods:
        reqs = [rid for rid, m in ws.state["reqs"].items() if mod in (m.get("modules") or [])]
        ds = [d for d in defects if mod in d["module"]]
        out(f"  {mod}：需求 {len(reqs)} 个，缺陷 {len(ds)} 条（漏测 {sum(d['escaped'] for d in ds)}）")
    return 0


# ───────────────────────── 状态与下一步 ─────────────────────────

ONBOARD = [
    ("env", "检查环境", "01_环境.md"),
    ("init", "建工作区", "02_建工作区.md"),
    ("source", "登记代码位置", "08_环境与权限.md"),
    ("raw", "放进原始资料", "03_原始资料.md"),
    ("wiki", "生成知识库并人工审核", "04_知识库.md"),
    ("req", "跑通第一个需求（生成 → 审核 → 定稿）", "05_需求与测试点.md"),
    ("exec", "按最终测试点执行一轮并记录结果", "09_手工执行.md"),
    ("target", "接上被测服务和接口文档", "06_被测服务.md"),
    ("tests", "AI 写接口用例并跑通一次", "07_接口用例.md"),
]
# 不同环境走的上手步骤：无代码权限靠人工执行；开放接口走接口自动化；完整代码权限再多登记代码位置
ONBOARD_BY_ACCESS = {
    "blackbox": ("env", "init", "raw", "wiki", "req", "exec"),
    "interface": ("env", "init", "raw", "wiki", "req", "target", "tests"),
    "source": ("env", "init", "source", "raw", "wiki", "req", "target", "tests"),
}


def onboarding(ws: Ws | None) -> list[dict]:
    env_ok, _ = doctor_report()
    steps = [s for s in ONBOARD if s[0] in ONBOARD_BY_ACCESS[ws.access if ws else "interface"]]
    if ws is None:
        return [{"id": i, "title": t, "doc": d, "done": (i == "env" and env_ok), "detail": ""} for i, t, d in steps]
    _, t = ws.target()
    tests = list((ws.auto / t["tests"]).rglob("test_*.py"))
    execs = exec_status(ws)
    checks = {
        "env": (env_ok, "运行 python qa.py doctor"),
        "init": (True, ""),
        "source": (bool(ws.cfg.get("source_paths")), "用 qa.py access source --source <代码文件夹> 登记"),
        "raw": (bool(ws.raw_files()), "raw/ 里还没有资料"),
        "wiki": (bool(ws.state["wiki"]["commits"]) and (ws.wiki / "index.md").exists(), "还没提交过知识库"),
        "req": (any((d / "最终测试点.md").exists() for d in ws.req_dirs()), "还没有定稿的需求"),
        "exec": (any(not p for _, _, p, _ in execs), "还没有填完的执行记录（qa.py exec R001）"),
        "target": (bool(t["base_url"]) and bool(ws.api_files()), "被测服务地址没配" if not t["base_url"] else "还没有接口文档"),
        "tests": (bool(tests) and bool(ws.state["autotest"]["runs"]), "还没有用例" if not tests else "用例还没跑过"),
    }
    return [{"id": i, "title": tt, "doc": d, "done": checks[i][0], "detail": "" if checks[i][0] else checks[i][1]} for i, tt, d in steps]


def daily(ws: Ws) -> list[str]:
    todo = []
    ch = raw_changes(ws)
    if ch["built"] and (ch["new"] or ch["changed"] or ch["deleted"]):
        todo.append(f"知识库要更新：资料新增 {len(ch['new'])}、改动 {len(ch['changed'])}、删除 {len(ch['deleted'])} 份 → {qa_cmd()} prompt wiki-update")
    probs, st = wiki_check(ws)
    if ch["built"] and probs:
        todo.append(f"知识库格式有 {len(probs)} 个问题 → {qa_cmd()} wiki check")
    if st["todo"]:
        todo.append(f"知识库里有 {st['todo']} 处“待确认”，找人确认后改掉")
    for r in req_rows(ws):
        if not r["done"]:
            todo.append(f"需求 {r['name']}：{r['stage']}")
            tp = ws.req_dir(r["id"]) / "测试点.md"
            gaps = coupling_gaps(ws, r["id"], parse_points(tp)) if tp.exists() else []
            if gaps:
                todo.append(f"需求 {r['id']} 的耦合提醒：" + "；".join(gaps) + f" → {qa_cmd()} coupling {r['id']}")
    ds = [defect_info(p) for p in ws.defect_files()]
    bad = [d["id"] for d in ds if d["problems"]]
    if bad:
        todo.append(f"缺陷记录没填完：{', '.join(bad)} → {qa_cmd()} defect list")
    items, ids = feedback_items(ws)
    if items:
        todo.append(f"待回流：{', '.join(ids)} → {qa_cmd()} prompt feedback")
    for rid, p, probs, _ in exec_status(ws):
        if probs:
            todo.append(f"执行记录 {p.name}（{rid}）没填完：{probs[0]}")
    for tname in ws.cfg["targets"]:
        _, t = ws.target(tname)
        lr = next((x for x in reversed(ws.state["autotest"]["runs"]) if x.get("target", "default") == tname), None)
        tests = list((ws.auto / t["tests"]).rglob("test_*.py"))
        if lr:
            if lr["failed"] and not (ws.root / lr["dir"] / "归因.md").exists():
                todo.append(f"{tname} 上次接口自动化有 {len(lr['failed'])} 个失败还没归因 → {qa_cmd()} prompt triage")
            if tests and max(p.stat().st_mtime for p in tests) > datetime.fromisoformat(lr["time"]).timestamp():
                todo.append(f"{tname} 的用例在上次运行后改过，重跑 → {qa_cmd()} autotest run" + ("" if tname == "default" else f" --target {tname}"))
    return todo


def cmd_status(args) -> int:
    root = find_ws(args)
    ws = Ws(root) if root else None
    ob = onboarding(ws)
    nxt = next((s for s in ob if not s["done"]), None)
    todo = daily(ws) if ws else []
    if args.json:
        out(json.dumps({"workspace": str(root) if root else None, "project": ws.cfg.get("project") if ws else None,
                        "onboarding": ob, "onboarding_next": nxt["id"] if nxt else None, "todo": todo}, ensure_ascii=False, indent=2))
        return 0
    out(f"工作区：{root}（项目：{ws.cfg.get('project')}）" if ws else "还没有工作区。")
    if nxt:
        out("")
        out("上手进度：")
        for i, s in enumerate(ob, 1):
            mark = "✅" if s["done"] else ("👉" if s is nxt else "⬜")
            out(f" {mark} {i}. {s['title']}" + (f"  —— {s['detail']}" if s is nxt and s["detail"] else ""))
    out("")
    if todo:
        out("待办：")
        for item in todo:
            out("  · " + item)
    elif ws and not nxt:
        out("没有待办。来新需求时：python qa.py req new \"标题\" --from 需求文件 --module 模块")
    out("")
    if nxt:
        out(f"下一步：「{nxt['title']}」，详细做法：python qa.py next")
    return 0


def cmd_next(args) -> int:
    root = find_ws(args)
    ws = Ws(root) if root else None
    ob = onboarding(ws)
    step = next((s for s in ob if s["id"] == args.step), None) if args.step else next((s for s in ob if not s["done"]), None)
    v = ws.values() if ws else {"qa": qa_cmd(), "kit": str(KIT), "ws": "<工作区>", "project": ""}
    if step:
        if step["detail"]:
            out(f"当前情况：{step['detail']}\n")
        out(render(read(KIT / "docs" / "上手" / step["doc"]), v))
    else:
        out(render(read(KIT / "docs" / "日常.md"), v))
        out("\n## 现在的待办\n")
        out("\n".join(f"- {item}" for item in daily(ws)) or "- 没有待办")
    return 0


# ───────────────────────── 检查、自检 ─────────────────────────

LEAK_PATTERNS = [
    (r"cloud1-[a-z0-9]{6,}", "微信云开发环境 ID"),
    (r"\bwx[0-9a-f]{16}\b", "微信 AppID"),
    (r"(?i)(secret|api[_-]?key|access[_-]?token|password)\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{12,}", "疑似密钥"),
    (r"-----BEGIN [A-Z ]*PRIVATE KEY-----", "私钥"),
    (r"\bsk-[A-Za-z0-9_\-]{20,}", "疑似 API Key"),
]


def scan_leaks(root: Path) -> list[str]:
    found = []
    for p in root.rglob("*"):
        if not p.is_file() or any(x in p.parts for x in (".git", "__pycache__", ".pytest_cache", "venv", ".venv", "node_modules")):
            continue
        if p.suffix.lower() not in TEXT_EXT or p.stat().st_size > MAX_FILE:
            continue
        text = read(p)
        for pat, what in LEAK_PATTERNS:
            for m in re.finditer(pat, text):
                found.append(f"{p.relative_to(root)}:{text.count(chr(10), 0, m.start()) + 1}  {what}：{m.group(0)[:40]}")
    return found


def cmd_check(args) -> int:
    targets = [("框架", KIT)]
    root = find_ws(args)
    if root:
        targets.append(("工作区", root))
    bad = 0
    for name, r in targets:
        found = scan_leaks(r)
        out(f"{name}（{r}）：" + ("没发现密钥、AppID、云环境 ID" if not found else f"发现 {len(found)} 处："))
        for f in found:
            out("   · " + f)
        bad += len(found)
    return 1 if bad else 0


def cmd_selftest(args) -> int:
    """在临时文件夹里用示例项目把整条管线走一遍（模型用假模型，不花钱），不碰你的工作区。"""
    try:
        import fastapi, uvicorn, httpx, pytest  # noqa: F401
    except Exception:
        out("自检要用示例服务，需要 fastapi、uvicorn、httpx、pytest：先运行 python qa.py setup")
        return 1
    tmp = Path(tempfile.mkdtemp(prefix="qa-selftest-"))
    root = tmp / "示例工作区"
    fails = []

    def step(name, fn):
        try:
            r = fn()
            ok = r in (None, 0, True)
        except Exception as e:  # noqa: BLE001
            ok = False
            out(f"     {type(e).__name__}: {e}")
        out(f"  {'✅' if ok else '❌'} {name}")
        if not ok:
            fails.append(name)

    ns = lambda **kw: argparse.Namespace(ws=str(root), **kw)
    out("在临时文件夹里用示例项目走一遍：")
    step("建工作区（示例）", lambda: cmd_init(argparse.Namespace(dir=str(root), name=None, example=True, force=False, no_register=True)))
    cfg = json.loads(read(root / CONFIG))  # 自检换一个不常用的端口，免得和正在跑的服务撞上；配两个假模型
    t = cfg["targets"]["default"]
    t["base_url"], t["start_cmd"] = "http://127.0.0.1:8790", t["start_cmd"].replace("--port 8000", "--port 8790")
    cfg["models"] = {"假模型甲": {"provider": "mock", "seed": "甲", "price_in": 1.0, "price_out": 5.0}, "假模型乙": {"provider": "mock", "seed": "乙"}}
    cfg["stage_models"] = {"testpoints": "假模型甲", "triage": "假模型乙"}
    write(root / CONFIG, json.dumps(cfg, ensure_ascii=False, indent=2))
    step("上手进度停在“生成知识库”", lambda: next(s for s in onboarding(Ws(root)) if not s["done"])["id"] == "wiki")

    def fake_wiki():
        write(root / "wiki" / "index.md", "# 目录\n\n- modules/广告领奖.md：广告领奖规则\n- modules/渔具店.md：渔具店\n"
                                          f"- {COUPLING}：模块之间共用的数值\n- risks/风险清单.md：通用风险\n")
        write(root / "wiki" / "modules" / "广告领奖.md", "# 广告领奖\n\n来源：raw/广告领奖规则.md\n\n- 每日 0 点重置（raw/广告领奖规则.md）\n")
        write(root / "wiki" / "modules" / "渔具店.md", "# 渔具店\n\n来源：raw/渔具店规则.md\n\n- 金币不能为负（raw/渔具店规则.md）\n")
        write(root / "wiki" / COUPLING, "# 耦合关系\n\n来源：raw/广告领奖规则.md、raw/渔具店规则.md\n\n"
                                       "| 共用的数值或状态 | 会改它的模块 | 会读它的模块 | 约束 | 来源 |\n|---|---|---|---|---|\n"
                                       "| 金币 | 广告领奖、渔具店 | 对账 | 不能为负，向下取整 | raw/渔具店规则.md |\n")
        write(root / "wiki" / "risks" / "风险清单.md", "# 风险清单\n\n来源：raw/常见缺陷.md\n\n## 幂等\n- 回包丢失重试\n")
        write(root / "wiki" / "log.md", f"# 变更记录\n\n- {today()} 初版\n")
        return cmd_wiki(ns(action="check", message=None, feedback=None, force=False))
    step("知识库格式检查", fake_wiki)
    step("提交知识库", lambda: cmd_wiki(ns(action="commit", message="初版", feedback=None, force=False)))

    def review_flow():
        d = Ws(root).req_dir("R001")
        write(d / "测试点.md", "| 编号 | 测试点 | 风险等级 | 类型 | 依据 | 怎么验证 |\n|---|---|---|---|---|---|\n"
                             "| 1 | 同一宝箱重复领取 | 高 | 异常 | wiki/risks/风险清单.md | 对账 |\n"
                             "| 2 | 23:59 看完 0 点后领 | 中 | 边界 | wiki/modules/广告领奖.md | 日志 |\n"
                             "| 3 | 宝箱可以分享给好友 | 低 | 正常 | 无 | 界面 |\n")
        assert cmd_review(ns(req="R001", force=False)) == 0
        txt = read(d / "审核.md").replace("审核耗时（分钟）：", "审核耗时（分钟）：12")
        txt = txt.replace("| 1 | 同一宝箱重复领取 |  |  |  |", "| 1 | 同一宝箱重复领取 | 采纳 |  |  |")
        txt = txt.replace("| 2 | 23:59 看完 0 点后领 |  |  |  |", "| 2 | 23:59 看完 0 点后领 | 改后采纳 |  | 23:59 看完第 3 次，0 点后领木箱 |")
        txt = txt.replace("| 3 | 宝箱可以分享给好友 |  |  |  |", "| 3 | 宝箱可以分享给好友 | 否决 | 不符合规则 |  |")
        write(d / "审核.md", txt.replace("## 审核时补充的测试点\n- ", "## 审核时补充的测试点\n- 免费次数算不算进累计"))
        assert cmd_accept(ns(req="R001")) == 0
        m = Ws(root).req_meta("R001")
        return m["generated"] == 3 and m["fabricated"] == 1 and m["added"] == 1 and m["final"] == 3
    step("需求：审核表 → 人工审核 → 定稿，指标正确", review_flow)
    step("耦合提醒：需求只测了广告领奖，没提到和渔具店共用的“金币” → 查出 1 处",
         lambda: len(coupling_gaps(Ws(root), "R001", parse_points(Ws(root).req_dir("R001") / "最终测试点.md"))) == 1
         and cmd_coupling(ns(req="R001")) == 0)

    def combos_flow():
        f = root / "autotest" / "组合因素.md"
        write(f, "| 因素 | 取值 |\n|---|---|\n| 商品价 | 0、1、500、3000 |\n| 券 | 无券、满减券、折扣券、两张叠加 |\n| 会员 | 否、是 |\n| 数量 | 1、2、99 |\n")
        assert cmd_combos(ns(file=str(f))) == 0
        rows = pairwise([("商品价", ["0", "1", "500", "3000"]), ("券", ["无", "满减", "折扣", "叠加"]), ("会员", ["否", "是"]), ("数量", ["1", "2", "99"])])
        pairs = {(i, r[i], j, r[j]) for r in rows for i in range(4) for j in range(i + 1, 4)}
        need = 4 * 4 + 4 * 2 + 4 * 3 + 4 * 2 + 4 * 3 + 2 * 3
        return len(pairs) == need and len(rows) < 4 * 4 * 2 * 3
    step("两两组合：4 个因素全组合 96 种，生成的组合覆盖全部两两搭配且行数少得多", combos_flow)

    def gen_flow():
        w = Ws(root)
        new_req(w, "渔具店特惠", EXAMPLE / "需求-广告宝箱.md", ["广告领奖"], quiet=True)
        w.save()
        assert cmd_gen(ns(what="testpoints", req="R002", model=None, force=False)) == 0
        return len(parse_points(Ws(root).req_dir("R002") / "测试点.md")) >= 4
    step("直接调模型生成测试点（假模型），记下用量", gen_flow)

    def bench_flow():
        w = Ws(root)
        new_req(w, "模型对比用", EXAMPLE / "需求-广告宝箱.md", ["广告领奖"], quiet=True)
        w.save()
        assert cmd_bench(ns(action="testpoints", req="R003", models="假模型甲,假模型乙", repeat=1, stamp=None, label=None, force=False)) == 0
        meta = bench_runs(Ws(root), "R003")[-1]
        bdir = root / meta["dir"]
        assert cmd_bench(ns(action="report", req="R003", models=None, repeat=1, stamp=None, label=None, force=False)) == 1  # 没审完不揭晓
        for e in meta["entries"]:
            rv = bdir / f"审核_{e['label']}.md"
            txt = read(rv).replace("审核耗时（分钟）：", "审核耗时（分钟）：5")
            txt = re.sub(r"^(\| \d+ \| [^|]+ \|)  \|  \|  \|$", r"\1 采纳 |  |  |", txt, flags=re.M)
            write(rv, txt)
        assert cmd_bench(ns(action="report", req="R003", models=None, repeat=1, stamp=None, label=None, force=False)) == 0
        rep = read(bdir / "对比报告.md")
        assert "假模型甲" in rep and "假模型乙" in rep and "100%" in rep
        first = meta["entries"][0]["label"]
        assert cmd_bench(ns(action="adopt", req="R003", models=None, repeat=1, stamp=None, label=first, force=False)) == 0
        return cmd_accept(ns(req="R003")) == 0
    step("模型对比：两个模型 → 盲审 → 揭晓报告 → 采用其中一份并定稿", bench_flow)

    def defect_flow():
        assert cmd_defect(ns(action="new", title="回包丢失重试重复到账", req="R001", stage="测试", from_test=None, ids=[])) == 0
        f = Ws(root).defect_files()[0]
        txt = read(f)
        for k, val in (("严重程度", "P1"), ("被最终测试点覆盖", "否"), ("归类", "幂等")):
            txt = set_field(txt, k, val)
        write(f, txt.replace("## 现象\n", "## 现象\n领奖回包丢失后重试，奖励到账两次\n", 1))
        assert "广告领奖" in defect_info(f)["module"]
        assert any("待回流" in x for x in daily(Ws(root)))
        assert cmd_prompt(ns(name="feedback", req=None, force=False, target=None)) == 0
        assert cmd_wiki(ns(action="commit", message="回流 D001", feedback="D001,R001", force=False)) == 0
        return not any("待回流" in x for x in daily(Ws(root)))
    step("缺陷登记（自动带模块）→ 回流 → 标记已回流", defect_flow)

    def auto_flow():
        shutil.copyfile(EXAMPLE / "sample_tests" / "test_sample.py", root / "autotest" / "tests" / "test_sample.py")
        shutil.copyfile(EXAMPLE / "sample_tests" / "invariants.py", root / "autotest" / "invariants.py")
        r = cmd_autotest(ns(action="run", start=True, k=None, target=None))
        lr = last_run(Ws(root))
        return r == 0 and lr["total"] >= 10 and not lr["failed"]
    step("接口自动化：临时启动示例服务 → 跑样例用例（每个用例后自动查不变量）全过 → 关服务", auto_flow)

    def invariant_catches():
        # 故意写一个把金币改成负数也不报错的“用例”，不变量检查要在用例结束时把它抓出来
        write(root / "autotest" / "tests" / "test_zz_bad.py",
              "import httpx\n\ndef test_buy_then_break(base_url):\n"
              "    with httpx.Client(base_url=base_url) as c:\n        c.post('/reset')\n"
              "        c.post('/shop/buy', json={'player': 'A', 'kind': 'rod', 'tier': 2})\n")
        inv = root / "autotest" / "invariants.py"
        good = read(inv)
        write(inv, good.replace("START_GOLD = 1000", "START_GOLD = 2000"))  # 让对账基准对不上，模拟“价格算错”
        cmd_autotest(ns(action="run", start=True, k="buy_then_break", target=None))
        lr = last_run(Ws(root))
        write(inv, good)
        (root / "autotest" / "tests" / "test_zz_bad.py").unlink()
        return len(lr["failed"]) == 1
    step("不变量兜底：用例本身没断言，账对不上也会在用例结束时被抓出来", invariant_catches)
    step("第二个被测服务可以单独配置", lambda: cmd_target(ns(action="set", name="admin", url="http://127.0.0.1:9", start=None, cwd=None, health=None)) == 0
         and "admin" in Ws(root).cfg["targets"] and (root / "autotest" / "tests" / "admin").exists())
    step("上手进度全部完成", lambda: all(s["done"] for s in onboarding(Ws(root))))
    step("指标（含按模块、模型用量）", lambda: cmd_metrics(ns()) == 0 and "编造 1 条" in read(root / "metrics" / "指标.md")
         and "假模型甲" in read(root / "metrics" / "指标.md"))
    step("资料改动后提示更新知识库", lambda: (write(root / "raw" / "广告领奖规则.md", "# 改了\n"), any("知识库要更新" in x for x in daily(Ws(root))))[1])
    step("工作区里没有密钥", lambda: not scan_leaks(root))

    def blackbox_flow():
        bb = tmp / "无代码权限"
        assert cmd_init(argparse.Namespace(dir=str(bb), name="黑盒项目", example=False, access="blackbox", force=False, no_register=True)) == 0
        ids_ = [s["id"] for s in onboarding(Ws(bb))]
        assert "exec" in ids_ and "tests" not in ids_ and "target" not in ids_
        shutil.copytree(root / "wiki", bb / "wiki", dirs_exist_ok=True)
        shutil.copytree(root / "raw", bb / "raw", dirs_exist_ok=True)
        w = Ws(bb)
        d = new_req(w, "示例", None, [], quiet=True)
        w.save()
        write(d / "最终测试点.md", "| 编号 | 测试点 | 风险等级 | 类型 |\n|---|---|---|---|\n| T01 | 重复领取只到账一次 | 高 | 异常 |\n| T02 | 跨 0 点重置 | 中 | 边界 |\n")
        nsb = lambda **kw: argparse.Namespace(ws=str(bb), **kw)
        assert cmd_exec(nsb(req="R001", round="v1", force=False)) == 0
        f = d / "执行记录_v1.md"
        txt = read(f).replace("| T01 | 重复领取只到账一次 |  |", "| T01 | 重复领取只到账一次 | 通过 |")
        write(f, txt.replace("| T02 | 跨 0 点重置 |  |", "| T02 | 跨 0 点重置 | 失败 |"))
        assert cmd_exec(nsb(req="R001", round="v1", force=False)) == 1  # 失败没填缺陷编号
        write(f, read(f).replace("| T02 | 跨 0 点重置 | 失败 |  |", "| T02 | 跨 0 点重置 | 失败 | D001 |"))
        return cmd_exec(nsb(req="R001", round="v1", force=False)) == 0 and next(s for s in onboarding(Ws(bb)) if s["id"] == "exec")["done"]
    step("无代码权限：上手步骤换成手工执行；执行清单 → 填结果 → 失败必须挂缺陷", blackbox_flow)

    def har_flow():
        fake_key = "sk" + "-" + "a" * 24  # 运行时拼出来的假密钥，源码里不留像密钥的字面值
        har = {"log": {"entries": [
            {"request": {"method": "POST", "url": "https://game.example.com/api/shop/buy?x=1", "queryString": [{"name": "x", "value": "1"}],
                         "postData": {"text": json.dumps({"item": 3, "token": fake_key})}},
             "response": {"status": 200, "content": {"text": '{"ok": true}'}}},
            {"request": {"method": "GET", "url": "https://game.example.com/api/me"}, "response": {"status": 200, "content": {"text": "{}"}}}]}}
        f = tmp / "抓包.har"
        write(f, json.dumps(har))
        assert cmd_ingest(ns(paths=[], api=False, target=None, refresh=False, redact=False, har=str(f))) == 0
        doc = read(root / "autotest" / "api" / "抓包_抓包.md")
        return "POST /api/shop/buy" in doc and "GET /api/me" in doc and fake_key not in doc
    step("抓包导入：HAR 整理成接口文档，密钥自动打码", har_flow)

    def source_flow():
        code = tmp / "代码"
        code.mkdir()
        run(["git", "init", "-q"], cwd=code)
        run(["git", "config", "user.email", "qa@localhost"], cwd=code)
        run(["git", "config", "user.name", "qa"], cwd=code)
        write(code / "shop.py", "PRICE = 500\n")
        run(["git", "add", "-A"], cwd=code)
        run(["git", "commit", "-q", "-m", "初版"], cwd=code)
        write(code / "shop.py", "PRICE = 450\n")
        run(["git", "commit", "-q", "-am", "改价"], cwd=code)
        assert cmd_access(ns(level="source", source=[str(code)])) == 0
        assert "source" in [s["id"] for s in onboarding(Ws(root))]
        return cmd_impact(ns(range=None)) == 0 and "完整代码权限" in read(root / "AGENTS.md")
    step("完整代码权限：登记代码位置 → 按 git 改动出回归范围任务", source_flow)
    shutil.rmtree(tmp, ignore_errors=True)
    out("")
    out("框架自检：" + ("全部通过。" if not fails else f"没通过：{'；'.join(fails)}"))
    return 0 if not fails else 1


# ───────────────────────── 入口 ─────────────────────────

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="qa.py", description="AI 测试管线。AI 每次开工先运行 python qa.py status。")
    ap.add_argument("--ws", help="指定工作区文件夹（默认：当前文件夹或上次用的工作区）")
    sp = ap.add_subparsers(dest="cmd", required=True)
    sp.add_parser("doctor", help="检查环境")
    sp.add_parser("setup", help="安装依赖（pip install -r requirements.txt）")
    p = sp.add_parser("init", help="建工作区")
    p.add_argument("dir")
    p.add_argument("--name", help="项目名")
    p.add_argument("--example", action="store_true", help="放进示例资料、接口文档、需求，并把示例服务配成被测服务")
    p.add_argument("--access", choices=list(ACCESS), default="interface",
                   help="环境：blackbox 无代码权限 / interface 开放接口或协议（默认） / source 完整代码权限")
    p.add_argument("--force", action="store_true")
    p.add_argument("--no-register", action="store_true", help=argparse.SUPPRESS)
    p = sp.add_parser("access", help="查看或设置环境权限：blackbox / interface / source")
    p.add_argument("level", nargs="?", choices=list(ACCESS))
    p.add_argument("--source", nargs="*", help="完整代码权限时登记代码文件夹")
    p = sp.add_parser("exec", help="手工执行：生成执行清单 / 检查执行结果")
    p.add_argument("req")
    p.add_argument("--round", help="第几轮或版本号，用于文件名（默认今天日期）")
    p.add_argument("--force", action="store_true")
    p = sp.add_parser("impact", help="完整代码权限：按代码改动推荐回归范围（打印给 AI 的任务说明）")
    p.add_argument("--range", help="git 改动范围，默认 HEAD~1..HEAD")
    p = sp.add_parser("use", help="切换当前工作区")
    p.add_argument("dir")
    p = sp.add_parser("status", help="上手进度和待办")
    p.add_argument("--json", action="store_true")
    p = sp.add_parser("next", help="下一件事的详细做法")
    p.add_argument("step", nargs="?", help="上手步骤 id：env init raw wiki req target tests")
    p = sp.add_parser("ingest", help="把资料复制进 raw/（或 --api 放进接口文档目录），记下来源")
    p.add_argument("paths", nargs="*")
    p.add_argument("--api", action="store_true", help="作为接口文档导入")
    p.add_argument("--target", help="接口文档属于哪个被测服务（默认 default）")
    p.add_argument("--refresh", action="store_true", help="按记录的原路径重新导入全部资料")
    p.add_argument("--redact", action="store_true", help="复制时把密钥、AppID、云环境 ID 打码（原文件不动）")
    p.add_argument("--har", help="把抓包导出的 HAR 文件整理成接口文档（放进接口文档目录）")
    p = sp.add_parser("wiki", help="知识库：check 格式检查 / commit 提交")
    p.add_argument("action", choices=["check", "commit"])
    p.add_argument("-m", "--message")
    p.add_argument("--feedback", help="这次提交回流了哪些缺陷 / 需求，如 D001,R002")
    p.add_argument("--force", action="store_true")
    p = sp.add_parser("req", help="需求：new 新建 / list 列表")
    p.add_argument("action", choices=["new", "list"])
    p.add_argument("title", nargs="?")
    p.add_argument("--from", dest="src", help="需求文件")
    p.add_argument("--module", help="属于哪些功能模块，逗号分隔（对应 wiki/modules/ 的页名）")
    p = sp.add_parser("prompt", help="打印某一步交给 AI 的任务说明")
    p.add_argument("name", help="、".join(PROMPTS))
    p.add_argument("req", nargs="?", help="需求编号（testpoints 必填，tests-plan 可选）")
    p.add_argument("--target", help="被测服务（tests-plan / tests-write 用）")
    p.add_argument("--force", action="store_true")
    p = sp.add_parser("gen", help="直接调模型接口：生成测试点 / 失败归因")
    p.add_argument("what", choices=["testpoints", "triage"])
    p.add_argument("req", nargs="?")
    p.add_argument("--model", help="模型名（模型清单里的名字）")
    p.add_argument("--force", action="store_true")
    p = sp.add_parser("bench", help="模型对比：testpoints 跑对比 / report 揭晓报告 / adopt 采用某个方案")
    p.add_argument("action", choices=["testpoints", "report", "adopt"])
    p.add_argument("req")
    p.add_argument("label", nargs="?", help="adopt 时要采用的方案，如 方案B")
    p.add_argument("--models", help="参加对比的模型，逗号分隔")
    p.add_argument("--repeat", type=int, default=1, help="每个模型跑几次（看稳定性）")
    p.add_argument("--stamp", help="哪一次对比（默认最近一次）")
    p.add_argument("--force", action="store_true")
    p = sp.add_parser("models", help="列出模型清单；--test 名字 试调一次")
    p.add_argument("--test")
    p = sp.add_parser("usage", help="模型用量：show 汇总 / add 手动补记")
    p.add_argument("action", nargs="?", default="show", choices=["show", "add"])
    p.add_argument("--stage")
    p.add_argument("--model")
    p.add_argument("--req")
    p.add_argument("--input", type=int)
    p.add_argument("--output", type=int)
    p.add_argument("--seconds", type=float)
    p.add_argument("--cost", type=float)
    p.add_argument("--note")
    p = sp.add_parser("review", help="为需求的测试点生成审核表")
    p.add_argument("req")
    p.add_argument("--force", action="store_true")
    p = sp.add_parser("accept", help="审核填完后定稿")
    p.add_argument("req")
    p = sp.add_parser("defect", help="缺陷：new 登记 / list 列表 / done 标记已回流")
    p.add_argument("action", choices=["new", "list", "done"])
    p.add_argument("title", nargs="?")
    p.add_argument("ids", nargs="*")
    p.add_argument("--req")
    p.add_argument("--stage", choices=["测试", "线上"])
    p.add_argument("--from-test", dest="from_test")
    p = sp.add_parser("target", help="被测服务：list 列表 / set 新增或修改")
    p.add_argument("action", choices=["list", "set"])
    p.add_argument("name", nargs="?")
    p.add_argument("--url")
    p.add_argument("--start", help="启动命令")
    p.add_argument("--cwd", help="启动命令在哪个文件夹运行")
    p.add_argument("--health", help="健康检查路径，如 /health；空字符串表示只看端口")
    p = sp.add_parser("autotest", help="接口自动化：run 跑用例 / start 启动被测服务")
    p.add_argument("action", choices=["run", "start"])
    p.add_argument("--target")
    p.add_argument("--start", action="store_true", help="服务没在跑时临时启动，跑完关掉")
    p.add_argument("-k", help="只跑名字匹配的用例（pytest -k）")
    sp.add_parser("metrics", help="算指标")
    sp.add_parser("modules", help="列出功能模块和各自的需求、缺陷数")
    p = sp.add_parser("coupling", help="模块耦合：列出共用数值；带需求编号时检查测试点有没有覆盖")
    p.add_argument("req", nargs="?")
    p = sp.add_parser("combos", help="两两组合：读“因素 | 取值”表，生成组合用例")
    p.add_argument("file")
    sp.add_parser("check", help="扫描密钥、AppID、云环境 ID")
    sp.add_parser("selftest", help="框架自检（临时文件夹里走一遍示例，不碰你的工作区）")
    args = ap.parse_args(argv)
    if args.cmd == "defect" and args.action == "done":
        args.ids = ([args.title] if args.title else []) + args.ids
    handler = {"doctor": cmd_doctor, "setup": cmd_setup, "init": cmd_init, "use": cmd_use, "status": cmd_status,
               "next": cmd_next, "ingest": cmd_ingest, "wiki": cmd_wiki, "req": cmd_req, "prompt": cmd_prompt,
               "gen": cmd_gen, "bench": cmd_bench, "models": cmd_models, "usage": cmd_usage, "review": cmd_review,
               "accept": cmd_accept, "defect": cmd_defect, "target": cmd_target, "autotest": cmd_autotest,
               "metrics": cmd_metrics, "modules": cmd_modules, "coupling": cmd_coupling, "combos": cmd_combos,
               "access": cmd_access, "exec": cmd_exec, "impact": cmd_impact,
               "check": cmd_check, "selftest": cmd_selftest}[args.cmd]
    try:
        return handler(args)
    except QaError as e:
        out(f"出错了：{e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
