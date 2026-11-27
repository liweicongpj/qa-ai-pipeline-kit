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
import qa_index  # noqa: E402

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


def git_commit(ws: Ws, msg: str, push_after: bool = True) -> bool:
    """只提交工作区这个文件夹里的改动（工作区放在别的仓库里时也不会带上外面的改动）；开了自动推送就顺手推到项目仓库。"""
    git(ws, "add", "-A", "--", ".")
    if not git(ws, "diff", "--cached", "--name-only", "--", ".").stdout.strip():
        return False
    git(ws, "commit", "-q", "-m", msg, "--", ".")
    if push_after and ws.cfg.get("auto_push") and own_repo(ws) and remote_url(ws):
        ok, why = push(ws, interactive=False)
        out("（已自动推送到项目仓库）" if ok else f"（自动推送没成功：{why}。在你自己的终端里运行 {qa_cmd()} sync，第一次会弹出登录）")
    return True


def own_repo(ws: Ws) -> bool:
    """工作区自己是一个 git 仓库（不是放在别的仓库里面）。"""
    top = run(["git", "rev-parse", "--show-toplevel"], cwd=ws.root).stdout.strip()
    return bool(top) and Path(top).resolve() == ws.root.resolve()


def remote_url(ws: Ws) -> str:
    r = run(["git", "remote", "get-url", "origin"], cwd=ws.root)
    return r.stdout.strip() if r.returncode == 0 else ""


def push(ws: Ws, interactive: bool) -> tuple[bool, str]:
    """推到 origin。自动推送时不弹登录（GIT_TERMINAL_PROMPT=0），没有保存过登录就直接失败，不卡住。"""
    if interactive:
        r = subprocess.run(["git", "push", "-u", "origin", "HEAD"], cwd=ws.root)
        return r.returncode == 0, "" if r.returncode == 0 else f"git push 退出码 {r.returncode}"
    r = run(["git", "push", "-u", "origin", "HEAD"], cwd=ws.root, env=sub_env({"GIT_TERMINAL_PROMPT": "0"}), timeout=120)
    return r.returncode == 0, (r.stderr or r.stdout).strip().splitlines()[-1] if r.returncode else ""


def cmd_remote(args) -> int:
    ws = ws_from_args(args)
    if not own_repo(ws):
        out("这个工作区放在别的 git 仓库里面，跟着那个仓库提交和上传，不单独设项目仓库。")
        return 1
    if args.action == "show":
        out(f"项目仓库：{remote_url(ws) or '（没设，只在本地）'}；自动推送：{'开' if ws.cfg.get('auto_push') else '关'}")
        return 0
    if not args.url:
        out("要写项目仓库地址：qa.py remote set https://github.com/你的账号/项目-qa.git（建议建成私有仓库）")
        return 1
    git(ws, "remote", "set-url" if remote_url(ws) else "add", "origin", args.url)
    ws.cfg["auto_push"] = not args.no_auto
    ws.save_cfg()
    email = run(["git", "config", "user.email"], cwd=ws.root).stdout.strip()
    out(f"项目仓库已设为 {args.url}，自动推送{'关' if args.no_auto else '开'}。之后 accept、wiki commit 提交后会自动推送。")
    if "github.com" in args.url and email and not email.endswith("users.noreply.github.com"):
        out(f"⚠️ 提交作者邮箱是 {email}，推上去后看得到仓库的人都能看到。想隐藏就在工作区里设：git config user.email <GitHub 隐藏邮箱>（Settings → Emails 里找）")
    out(f"第一次推送要登录：在你自己的终端里运行 {qa_cmd()} sync")
    return 0


def cmd_sync(args) -> int:
    ws = ws_from_args(args)
    if not own_repo(ws) or not remote_url(ws):
        out("还没设项目仓库：qa.py remote set <仓库地址>")
        return 1
    git_commit(ws, "同步：提交未提交的改动", push_after=False)
    ok, why = push(ws, interactive=True)
    out(f"已推送到 {remote_url(ws)}" if ok else f"推送失败：{why}（看上面 git 的报错；没登录的话会弹出登录窗口）")
    return 0 if ok else 1


# ───────────────────────── doctor / setup ─────────────────────────

def doctor_report() -> tuple[bool, list[tuple[str, bool, str]]]:
    rows = []
    ok_py = sys.version_info >= (3, 10)
    rows.append(("Python 3.10 以上", ok_py, f"当前 {sys.version.split()[0]}（{sys.executable}）"))
    has_git = shutil.which("git") is not None
    rows.append(("git", has_git, "已安装" if has_git else "没装：到 https://git-scm.com 下载安装"))
    for group, pkgs, need in (("跑接口用例", ("pytest", "httpx"), True), ("示例服务（可选）", ("fastapi", "uvicorn"), False),
                              ("调 Claude 接口（可选）", ("anthropic",), False),
                              ("导入 Word / Excel 资料（可选）", ("docx", "openpyxl"), False)):
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


# ───────────────────────── 需求、测试点、审核 ─────────────────────────

def next_id(existing: list[str], prefix: str) -> str:
    nums = [int(m.group(1)) for x in existing if (m := re.match(rf"{prefix}(\d+)", x))]
    return f"{prefix}{(max(nums) + 1) if nums else 1:03d}"


def safe_name(title: str) -> str:
    return re.sub(r'[\\/:*?"<>|\s]+', "_", title.strip())[:40] or "未命名"


def new_req(ws: Ws, title: str, src: Path | None, modules: list[str], quiet: bool = False) -> Path:
    rid = next_id([p.name for p in ws.req_dirs()], "R")
    d = ws.reqs / f"{rid}-{safe_name(title)}"
    if src:
        if not src.is_file():
            raise QaError(f"找不到需求文件 {src}")
        try:  # Word、Excel、网页、PDF 先转成文字（禅道、飞书等导出的需求单也行），Markdown 和文本原样用
            body = qa_index.doc_text(src)
        except qa_index.IndexError_ as e:
            raise QaError(str(e)) from e
        if src.suffix.lower() not in (".md", ".txt"):
            body = f"# 需求：{title}\n\n> 来源：{src.name}（{today()} 从原文件转换导入；图片和流程图没有转进来，以原文件为准）\n\n{body.strip()}\n"
        write(d / "需求.md", body)
    else:
        write(d / "需求.md", render(ws.template("需求模板.md"), {"title": title}))
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
    rows = "\n".join(f"| {p['no']} | {p['point']} |  |  |  |" for p in pts) or "| 1 | （没解析出测试点，把 AI 的测试点手动抄进来） |  |  |  |"
    text = render(ws.template("审核表.md"), {"req": title, "rows": rows})
    model = "" if blind else generated_by(src)  # 模型对比要盲审，不预填
    if model:
        text = re.sub(r"^(模型名称[：:])[ \t]*$", lambda m: m.group(1) + model, text, count=1, flags=re.M)
    write(dst, text)
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
    out(f"下一步：把这个需求定下的新规则、新耦合并进知识库，以后的需求才看得到 → {qa_cmd()} prompt wiki-merge {rid}"
        f"（没有新规则就 {qa_cmd()} req merged {rid}）；同时可以开始执行或写自动化用例。")
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

PROMPTS = ("wiki-build", "wiki-update", "wiki-merge", "wiki-lint", "testpoints", "tests-plan", "tests-write", "triage", "feedback")


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


def untriaged_runs(ws: Ws) -> list[dict]:
    """有失败、却没写归因（运行目录里没有 归因.md）的运行，旧的在前。"""
    return [r for r in ws.state["autotest"]["runs"] if r["failed"] and not (ws.root / r["dir"] / "归因.md").exists()]


def triage_target(ws: Ws, which: str | None) -> dict | None:
    """要归因的那次运行：指定的；否则最近一次有失败、还没归因的。"""
    runs = ws.state["autotest"]["runs"]
    if which:
        return next((r for r in runs if which in r["dir"]), None)
    pend = untriaged_runs(ws)
    return pend[-1] if pend else None


def triage_note(ws: Ws, run: dict) -> str:
    later = [r for r in ws.state["autotest"]["runs"] if r["time"] > run["time"] and r.get("target") == run.get("target")]
    passed = sorted({x for r in later for x in r.get("fixed", [])} & set(run["failed"]))
    if not passed:
        return ""
    return ("注意：这些失败之后又跑通了（" + "、".join(x.split("::")[-1] for x in passed) +
            "）。要说明中间改了什么：是用例写错改对了，还是服务修好了；如果是改了断言，写清为什么原来的预期不对、依据哪条规则。")


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
        cc = corpus_changes(ws) or {"new": [], "changed": [], "deleted": []}
        lines += ([f"- 资料文件夹新增：{x}" for x in cc["new"]] + [f"- 资料文件夹有改动：{x}" for x in cc["changed"]] +
                  [f"- 资料文件夹已删除：{x}" for x in cc["deleted"]])
        if not lines:
            out("资料自上次提交知识库以来没有变化，不用更新。")
            return 0
        if cc["new"] or cc["changed"]:
            pend = index_pending(ws)
            if pend and (pend["new"] or pend["changed"]):
                out(f"资料索引还没更新，先运行 {qa_cmd()} index（任务说明里让 AI 用 index show 读新文档，读的是索引里的内容）。")
                return 1
        v["changes"] = "\n".join(lines)
    if name == "wiki-merge":
        if not args.req:
            out(f"要指定需求编号：{qa_cmd()} prompt wiki-merge R002")
            return 1
        v.update(req_values(ws, args.req))
        d = ws.root / v["req_dir"]
        if not (d / "最终测试点.md").exists():
            out(f"需求 {v['req']} 还没定稿：先 review、accept，再把新规则并进知识库。")
            return 1
        rows, _ = parse_review(d / "审核.md") if (d / "审核.md").exists() else ([], {})
        wrong = [f"- {r['no']}：{r['point']}" for r in rows if r["verdict"] == "否决" and r["reason"] == "不符合规则"]
        v["wrong"] = "\n".join(wrong) or "（没有）"
    if name == "triage":
        lr = triage_target(ws, args.run)
        if not lr or not lr["failed"]:
            out("没有要归因的失败（每次有失败的运行都写过 归因.md 了）。" if not args.run else f"找不到有失败的运行：{args.run}")
            return 0
        v.update({"run_dir": lr["dir"], "failed": "\n".join(f"- {x}" for x in lr["failed"]), "target": lr.get("target", "default"),
                  "note": triage_note(ws, lr)})
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
    lr = triage_target(ws, getattr(args, "run", None))
    if not lr or not lr["failed"]:
        out("没有要归因的失败（每次有失败的运行都写过 归因.md 了）。")
        return 0
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


# ───────────────────────── 大批资料：登记目录、建索引、检索、查配置表、查历史缺陷 ─────────────────────────

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
    ws = ws_from_args(args)
    idx = open_index(ws)
    try:
        kinds = ("doc", "config", "defect") if args.kind == "all" else (args.kind,)
        hits = idx.search(" ".join(args.query), kinds, k=args.n, emb=ws.cfg.get("embedding"))
    finally:
        idx.close()
    if not hits:
        out("没找到相关内容。换几个关键词试试（中文按两个字一组匹配）。")
        return 1
    show_hits(hits, args.chars, " ".join(args.query))
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
        out("| 行号 | " + " | ".join(pick) + " |")
        out("|---|" + "---|" * len(pick))
        for rn, row in hits:
            out(f"| {rn} | " + " | ".join((row[i] if i < len(row) else "").replace("|", "/") for i in ids) + " |")
        return 0
    finally:
        idx.close()


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


def retrieved_context(ws: Ws, query: str, budget: int = 40000, exclude: Path | None = None) -> str:
    """直接调模型生成测试点时，从资料索引里找相关的策划段落（含以前做过的需求）、历史缺陷和配置表，放进提示词。
    exclude：这次需求自己的文件夹，它的需求原文已经在提示词里了，不重复放。"""
    idx = open_index(ws, must=False)
    if idx is None:
        return "（这个工作区没有建资料索引）"
    emb = ws.cfg.get("embedding")
    skip = str(exclude.resolve()) if exclude else None
    try:
        docs = [h for h in idx.search(query, ("doc",), k=16, emb=emb) if not (skip and h["path"].startswith(skip))][:12]
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
    fixed = sorted((prev_failed - failed) & set(res["tests"]))
    # 上次失败、这次通过，中间改过用例、上次又没写归因：可能是把断言改成迁就服务，要补记录
    untriaged_fix = []
    if fixed and not (ws.root / prev["dir"] / "归因.md").exists():
        prev_t = datetime.fromisoformat(prev["time"]).timestamp()
        if any(p.stat().st_mtime > prev_t for p in tests) or ((ws.auto / "invariants.py").exists() and (ws.auto / "invariants.py").stat().st_mtime > prev_t):
            untriaged_fix = fixed
    summary = {"time": now_iso(), "target": tname, "dir": ws.rel(run_dir), "total": len(res["tests"]), "failed": sorted(failed),
               "new_failed": sorted(failed - prev_failed), "fixed": fixed, "untriaged_fix": untriaged_fix,
               "seconds": round(time.monotonic() - t0, 1), "k": args.k}
    runs.append(summary)
    del runs[:-100]
    ws.save()
    lines = [f"# 接口自动化运行 {st}", "", f"被测服务：{tname}（{t['base_url']}）　用例 {summary['total']} 个，失败 {len(failed)} 个，用时 {summary['seconds']} 秒" +
             (f"（筛选：-k {args.k}）" if args.k else ""), ""]
    if prev:
        lines += [f"和上次（{prev['time']}）比：新失败 {len(summary['new_failed'])} 个，修好 {len(summary['fixed'])} 个", ""]
    if untriaged_fix:
        lines += [f"⚠️ 上次失败的 {len(untriaged_fix)} 个用例，改了用例以后才通过，上次的失败却没写归因（{prev['dir']}/归因.md）。"
                  f"请补上：为什么失败、改了什么、原来的预期为什么不对 → {qa_cmd()} prompt triage", ""]
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
    git_commit(ws, f"接口自动化：{tname} 运行 {st}，用例 {summary['total']} 个，失败 {len(failed)} 个")
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
    git_commit(ws, "指标：更新")
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
        "raw": (bool(ws.raw_files()) or bool(ws.state.get("index", {}).get("files")), "raw/ 里还没有资料，也没有建资料索引"),
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
    pend_idx = index_pending(ws)
    if pend_idx and (pend_idx["new"] or pend_idx["changed"] or pend_idx["deleted"]):
        todo.append("登记的资料目录还没建索引 → " + f"{qa_cmd()} index" if pend_idx.get("never") else
                    f"资料索引要更新：新增 {len(pend_idx['new'])}、改动 {len(pend_idx['changed'])}、删除 {len(pend_idx['deleted'])} 个文件 → {qa_cmd()} index")
    if ch["built"] and (ch["new"] or ch["changed"] or ch["deleted"]):
        todo.append(f"知识库要更新：资料新增 {len(ch['new'])}、改动 {len(ch['changed'])}、删除 {len(ch['deleted'])} 份 → {qa_cmd()} prompt wiki-update")
    cc = corpus_changes(ws)
    if cc and (cc["new"] or cc["changed"] or cc["deleted"]):
        todo.append(f"资料文件夹里的文档自上次提交知识库后有变化（新增 {len(cc['new'])}、改动 {len(cc['changed'])}、删除 {len(cc['deleted'])} 份），"
                    f"知识库可能要跟着更新 → {qa_cmd()} prompt wiki-update")
    um = unmerged_reqs(ws)
    if um:
        todo.append(f"已定稿的需求 {', '.join(um)} 定下的新规则还没并进知识库 → {qa_cmd()} prompt wiki-merge {um[0]}"
                    f"（没有新规则就 {qa_cmd()} req merged {um[0]}）")
    probs, st = wiki_check(ws)
    if ch["built"] and probs:
        todo.append(f"知识库格式有 {len(probs)} 个问题 → {qa_cmd()} wiki check")
    if st["todo"]:
        todo.append(f"知识库里有 {st['todo']} 个待确认的问题，找人确认后改掉 → {qa_cmd()} wiki todo 看清单")
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
    pend = untriaged_runs(ws)
    if pend:
        later_fixed = [r for r in pend if triage_note(ws, r)]
        todo.append(f"有 {len(pend)} 次运行的失败还没归因（最近一次 {pend[-1]['dir']}，{len(pend[-1]['failed'])} 个）"
                    + (f"，其中 {len(later_fixed)} 次是后来改了用例才通过的，要补记录为什么改" if later_fixed else "")
                    + f" → {qa_cmd()} prompt triage")
    for tname in ws.cfg["targets"]:
        _, t = ws.target(tname)
        lr = next((x for x in reversed(ws.state["autotest"]["runs"]) if x.get("target", "default") == tname), None)
        tests = list((ws.auto / t["tests"]).rglob("test_*.py"))
        if lr:
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

    def todo_flow():
        mod = root / "wiki" / "modules" / "渔具店.md"
        risk = root / "wiki" / "risks" / "风险清单.md"
        write(mod, read(mod) + "\n## 待确认\n\n- 跳档要不要补中间档差价（raw/渔具店规则.md）→ 待确认\n")
        write(risk, read(risk) + "\n## 待确认\n\n- 跳档要不要补中间档差价（raw/常见缺陷.md）→ 待确认\n")
        items = todo_items(Ws(root))
        write(mod, read(mod).split("\n## 待确认")[0] + "\n")
        write(risk, read(risk).split("\n## 待确认")[0] + "\n")
        return len(items) == 1  # 标题不算，同一个问题抄在两页只算一次
    step("待确认只数真正的问题：标题不算、两页重复的只算一次", todo_flow)

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
        assert cmd_review(ns(req="R002", force=False)) == 0
        assert "模型名称：假模型甲" in read(Ws(root).req_dir("R002") / "审核.md")  # 审核表自动记下是哪个模型出的
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

    def merge_flow():
        import contextlib
        import io
        w = Ws(root)
        assert "R001" in unmerged_reqs(w) and any("还没并进知识库" in x for x in daily(w))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            assert cmd_prompt(ns(name="wiki-merge", req="R001", force=False, target=None)) == 0
        assert "宝箱可以分享给好友" in buf.getvalue()  # 审核时因“不符合规则”否决的点要带上，告诉 AI 哪里理解错了
        assert cmd_prompt(ns(name="wiki-merge", req="R002", force=False, target=None)) == 1  # 没定稿的不能并
        mod = root / "wiki" / "modules" / "渔具店.md"
        write(mod, read(mod) + "- 购买第 2 档起要口碑达标（需求 R001）\n")
        assert cmd_wiki(ns(action="commit", message="并入需求 R001", feedback=None, merged="R001", force=False)) == 0
        assert cmd_req(ns(action="merged", title="R003", src=None, module=None)) == 0  # 没有新规则的直接标记
        return not unmerged_reqs(Ws(root)) and not any("还没并进知识库" in x for x in daily(Ws(root)))
    step("需求定稿后：提醒把新规则并进知识库 → 任务说明带上被否决的错误理解 → 提交后不再提醒", merge_flow)

    def req_from_file():
        f = tmp / "禅道需求单.html"
        write(f, "<html><body><h1>鱼竿口碑门槛</h1><table><tr><th>档位</th><th>口碑</th></tr>"
                 "<tr><td>2</td><td>50</td></tr></table><script>var x=1</script></body></html>")
        w = Ws(root)
        d = new_req(w, "鱼竿口碑门槛", f, ["渔具店"], quiet=True)
        w.save()
        txt = read(d / "需求.md")
        return "鱼竿口碑门槛" in txt and "50" in txt and "<td>" not in txt and "var x" not in txt and "来源：禅道需求单.html" in txt
    step("需求文件可以是网页 / Word / PDF：导入时转成文字", req_from_file)

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

    def untriaged_flow():
        t = root / "autotest" / "tests" / "test_zz_fix.py"
        write(t, "def test_zz_fix(base_url):\n    assert 1 + 1 == 3\n")
        cmd_autotest(ns(action="run", start=True, k="zz_fix", target=None))
        failing = last_run(Ws(root))
        time.sleep(1.2)
        write(t, "def test_zz_fix(base_url):\n    assert 1 + 1 == 2\n")  # 没写归因就把用例改成通过
        cmd_autotest(ns(action="run", start=True, k="zz_fix", target=None))
        w = Ws(root)
        ok = (last_run(w).get("untriaged_fix") and any("改了用例才通过" in x for x in daily(w))
              and triage_target(w, None)["dir"] == failing["dir"])
        for r in untriaged_runs(w):  # 补上归因，提醒消失
            write(root / r["dir"] / "归因.md", "用例写错了：1 + 1 应该等于 2\n")
        t.unlink()
        return ok and not untriaged_runs(Ws(root))
    step("失败后没写归因就改用例通过：运行摘要和待办都会提醒，补上归因后消失", untriaged_flow)
    step("第二个被测服务可以单独配置", lambda: cmd_target(ns(action="set", name="admin", url="http://127.0.0.1:9", start=None, cwd=None, health=None)) == 0
         and "admin" in Ws(root).cfg["targets"] and (root / "autotest" / "tests" / "admin").exists())
    step("上手进度全部完成", lambda: all(s["done"] for s in onboarding(Ws(root))))
    def push_flow():
        bare = tmp / "项目仓库.git"
        run(["git", "init", "-q", "--bare", str(bare)])
        assert cmd_remote(ns(action="set", url=str(bare), no_auto=False)) == 0
        write(root / "raw" / "新资料.md", "# 新资料\n")
        git_commit(Ws(root), "试一下自动推送")
        local = run(["git", "rev-parse", "HEAD"], cwd=root).stdout.strip()
        remote = run(["git", "--git-dir", str(bare), "rev-parse", "HEAD"]).stdout.strip()
        (root / "raw" / "新资料.md").unlink()
        git_commit(Ws(root), "删掉试推送的资料")
        return bool(local) and local == remote
    step("设了项目仓库后，提交会自动推送过去", push_flow)
    step("指标（含按模块、模型用量）", lambda: cmd_metrics(ns()) == 0 and "编造 1 条" in read(root / "metrics" / "指标.md")
         and "假模型甲" in read(root / "metrics" / "指标.md"))
    step("资料改动后提示更新知识库", lambda: (write(root / "raw" / "广告领奖规则.md", "# 改了\n"), any("知识库要更新" in x for x in daily(Ws(root))))[1])
    step("工作区里没有密钥", lambda: not scan_leaks(root))

    def corpus_flow():
        try:
            import docx as docx_lib
            import openpyxl
        except ImportError:
            out("     （没装 python-docx / openpyxl，跳过这一项；python qa.py setup 后再测）")
            return True
        src = tmp / "资料"
        for sub in ("策划", "配置", "缺陷"):
            (src / sub).mkdir(parents=True)
        doc = src / "策划" / "营业款.docx"
        d = docx_lib.Document()
        d.add_heading("营业款设计", 1)
        d.add_heading("翻倍规则", 2)
        d.add_paragraph("营业款翻倍封顶为参考场费的两倍，翻完本金清零。")
        d.add_heading("领取", 2)
        d.add_paragraph("领取向下取整，零头留到下次。")
        d.save(doc)
        wb = openpyxl.Workbook()
        sh = wb.active
        sh.title = "鱼竿"
        for row in (["id", "name", "price"], ["int", "string", "int"], ["编号", "名称", "价格"], [1, "竹节入门竿", 0], [6, "龙骨巨物竿", 38000]):
            sh.append(row)
        wb.save(src / "配置" / "装备.xlsx")
        with open(src / "缺陷" / "禅道导出.csv", "w", newline="", encoding="gbk") as fh:  # 禅道默认导出 GBK
            fh.write("Bug编号,所属模块,Bug标题,严重程度,Bug状态,重现步骤\n1,/营业款,营业款翻倍后本金没有清零,1,激活,翻倍后再领一次\n"
                     "2,/体力,体力跨天没重置,3,已关闭,改到第二天\n")
        write(src / "配置" / "economy.json", json.dumps({  # 很多游戏的配置是 JSON：对象数组、结构相近的子项、单个数值三种都要能查
            "version": 3, "stamina": {"cap": 100, "regen": {"min": 5}},
            "gear": {"note": "装备", "rod": [{"tier": 1, "price": 0, "repMin": 0}, {"tier": 2, "price": 800, "repMin": 50}]},
            "species": {"jiyu": {"name": "鲫鱼", "median": 0.6}, "liyu": {"name": "鲤鱼", "median": 3}}}, ensure_ascii=False))
        nsc = lambda **kw: ns(**kw)
        assert cmd_corpus(nsc(action="add", target=str(src / "策划"), kind="doc", name="策划案")) == 0
        assert cmd_corpus(nsc(action="add", target=str(src / "配置"), kind="config", name="配置表")) == 0
        assert cmd_corpus(nsc(action="add", target=str(src / "缺陷"), kind="defect", name="缺陷")) == 0
        assert cmd_embed(nsc(action="set", provider="mock", model="假向量", url=None, key_env=None)) == 0
        assert cmd_index(nsc(action="build", rebuild=False, no_embed=False)) == 0
        w = Ws(root)
        idx = open_index(w)
        try:
            doc_hit = idx.search("营业款翻倍 封顶", ("doc",), k=1, emb=w.cfg.get("embedding"))
            t = idx.table("装备")
            rows, _ = idx.table_rows(t["name"], [("name", "龙骨巨物竿")], 5)
            bug = idx.search("本金没有清零", ("defect",), k=1)
        finally:
            idx.close()
        ok = (doc_hit and "翻倍规则" in doc_hit[0]["title"] and rows and rows[0][1][2] == "38000"
              and bug and "本金" in bug[0]["title"] and ".qa/index.sqlite" in read(root / ".gitignore"))
        time.sleep(1.1)
        d.add_paragraph("新增：领取冷却 10 秒。")
        d.save(doc)
        stale = index_pending(w)
        ctx = retrieved_context(w, "营业款翻倍")
        return bool(ok) and len(stale["changed"]) == 1 and "翻倍规则" in ctx and "装备" in ctx
    step("大批资料：Word / Excel / 禅道 CSV 建索引 → 检索、精确查表、查缺陷 → 改文件后提示重建 → 生成时带上检索内容", corpus_flow)

    def idx_ok_json(w: Ws) -> bool:
        idx = open_index(w)
        try:
            rod, _ = idx.table_rows("economy/gear.rod", [("tier", "2")], 5)
            cols = idx.table("economy/gear.rod")["columns"]
            cap, _ = idx.table_rows("economy/stamina", [("键", "regen.min")], 5)
            fish, _ = idx.table_rows("economy/species", [("键", "liyu")], 5)
        finally:
            idx.close()
        return bool(rod and rod[0][1][cols.index("repMin")] == "50" and cap and cap[0][1][1] == "5"
                    and fish and fish[0][1][1] == "鲤鱼")
    step("JSON 配置：对象数组、结构相近的子项、深层单个数值都拆成表，cfg show 能精确查", lambda: idx_ok_json(Ws(root)))

    def builtin_flow():
        import contextlib
        import io
        w = Ws(root)
        idx = open_index(w)
        try:
            own_bug = idx.search("回包丢失 重复到账", ("defect",), k=3)
            own_req = idx.search("鱼竿口碑门槛", ("doc",), k=3)
        finally:
            idx.close()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            shown = cmd_index(ns(action="show", file="营业款.docx", rebuild=False, no_embed=False))
        return (any(h["corpus"] == "本工作区缺陷" for h in own_bug) and any(h["corpus"] == "本工作区需求" for h in own_req)
                and shown == 0 and "翻倍封顶" in buf.getvalue())
    step("本工作区的需求和缺陷自动进索引；index show 能看 Word 全文", builtin_flow)

    def corpus_wiki_flow():
        w = Ws(root)
        cc = corpus_changes(w)  # 建索引时已把当时的文档记为基线，刚才又改了营业款.docx
        assert cc and len(cc["changed"]) == 1 and any("知识库可能要跟着更新" in x for x in daily(w))
        assert cmd_prompt(ns(name="wiki-update", req=None, force=False, target=None)) == 1  # 索引还没更新，先 index
        assert cmd_index(ns(action="build", rebuild=False, no_embed=False, file=None)) == 0
        assert cmd_prompt(ns(name="wiki-update", req=None, force=False, target=None)) == 0
        assert cmd_wiki(ns(action="commit", message="按资料更新", feedback=None, merged=None, force=False)) == 0
        cc = corpus_changes(Ws(root))
        return cc is not None and not (cc["new"] or cc["changed"] or cc["deleted"])
    step("资料文件夹里的文档变了：提醒更新知识库 → 先更新索引 → 任务说明 → 提交后清零", corpus_wiki_flow)

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
    p = sp.add_parser("wiki", help="知识库：check 格式检查 / commit 提交 / todo 待确认清单")
    p.add_argument("action", choices=["check", "commit", "todo"])
    p.add_argument("-m", "--message")
    p.add_argument("--feedback", help="这次提交回流了哪些缺陷 / 需求，如 D001,R002")
    p.add_argument("--merged", help="这次提交并入了哪些已定稿需求的新规则，如 R002")
    p.add_argument("--force", action="store_true")
    p = sp.add_parser("req", help="需求：new 新建 / list 列表 / merged 标记不用并入知识库")
    p.add_argument("action", choices=["new", "list", "merged"])
    p.add_argument("title", nargs="?", help="new：需求标题；merged：需求编号")
    p.add_argument("--from", dest="src", help="需求文件（Markdown、文本、Word、Excel、网页、PDF；禅道等系统导出的需求单也行）")
    p.add_argument("--module", help="属于哪些功能模块，逗号分隔（对应 wiki/modules/ 的页名）")
    p = sp.add_parser("prompt", help="打印某一步交给 AI 的任务说明")
    p.add_argument("name", help="、".join(PROMPTS))
    p.add_argument("req", nargs="?", help="需求编号（testpoints、wiki-merge 必填，tests-plan 可选）")
    p.add_argument("--target", help="被测服务（tests-plan / tests-write 用）")
    p.add_argument("--run", help="triage 用：指定哪次运行（运行目录名，如 20261126-212022），默认最近一次没归因的")
    p.add_argument("--force", action="store_true")
    p = sp.add_parser("gen", help="直接调模型接口：生成测试点 / 失败归因")
    p.add_argument("what", choices=["testpoints", "triage"])
    p.add_argument("req", nargs="?")
    p.add_argument("--model", help="模型名（模型清单里的名字）")
    p.add_argument("--run", help="triage 用：指定哪次运行，默认最近一次没归因的")
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
    p = sp.add_parser("corpus", help="大批资料：add 登记资料目录（资料留在原处）/ list / remove")
    p.add_argument("action", choices=["add", "list", "remove"])
    p.add_argument("target", nargs="?", help="add：文件夹或文件；remove：名字")
    p.add_argument("--kind", choices=["doc", "config", "defect"], help="doc 文档 / config 配置表 / defect 缺陷导出文件")
    p.add_argument("--name", help="给这批资料起个名字（默认用文件夹名）")
    p = sp.add_parser("index", help="资料索引：build 建或增量更新（默认）/ stats 看概况 / show 看某个文件的全文")
    p.add_argument("action", nargs="?", default="build", choices=["build", "stats", "show"])
    p.add_argument("file", nargs="?", help="show：要看的文件名或路径")
    p.add_argument("--rebuild", action="store_true", help="删掉旧索引全部重建")
    p.add_argument("--no-embed", action="store_true", help="这次不算向量")
    p = sp.add_parser("search", help="在资料索引里检索（默认查文档）")
    p.add_argument("query", nargs="+")
    p.add_argument("--kind", default="doc", choices=["doc", "config", "defect", "all"])
    p.add_argument("-n", type=int, default=8, help="返回几条")
    p.add_argument("--chars", type=int, default=600, help="每条最多显示多少字")
    p = sp.add_parser("cfg", help="配置表：find 按关键词找表 / show 精确查表 / list 列表名")
    p.add_argument("action", choices=["find", "show", "list"])
    p.add_argument("words", nargs="*")
    p.add_argument("--where", action="append", help="show 的筛选条件：字段=值，可以写多个")
    p.add_argument("--cols", help="show 只显示这些字段，逗号分隔")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("-n", type=int, default=8)
    p = sp.add_parser("history", help="历史缺陷：find 检索 / stats 按模块、严重程度统计")
    p.add_argument("action", choices=["find", "stats"])
    p.add_argument("words", nargs="*")
    p.add_argument("--module", help="只看这个模块")
    p.add_argument("-n", type=int, default=8)
    p.add_argument("--chars", type=int, default=500)
    p = sp.add_parser("embed", help="向量模型：set 设置 / test 试调 / show / off 关闭")
    p.add_argument("action", choices=["set", "test", "show", "off"])
    p.add_argument("provider", nargs="?", choices=["ollama", "openai", "mock"])
    p.add_argument("--model")
    p.add_argument("--url")
    p.add_argument("--key-env", dest="key_env")
    sp.add_parser("metrics", help="算指标")
    sp.add_parser("modules", help="列出功能模块和各自的需求、缺陷数")
    p = sp.add_parser("coupling", help="模块耦合：列出共用数值；带需求编号时检查测试点有没有覆盖")
    p.add_argument("req", nargs="?")
    p = sp.add_parser("combos", help="两两组合：读“因素 | 取值”表，生成组合用例")
    p.add_argument("file")
    p = sp.add_parser("remote", help="项目仓库：show 查看 / set 设置地址（之后提交会自动推送）")
    p.add_argument("action", choices=["show", "set"])
    p.add_argument("url", nargs="?")
    p.add_argument("--no-auto", action="store_true", help="只设地址，不自动推送（手动 qa.py sync）")
    sp.add_parser("sync", help="把工作区推到项目仓库（第一次会弹出登录，要在你自己的终端里运行）")
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
               "access": cmd_access, "exec": cmd_exec, "impact": cmd_impact, "remote": cmd_remote, "sync": cmd_sync,
               "corpus": cmd_corpus, "index": cmd_index, "search": cmd_search, "cfg": cmd_cfg, "history": cmd_history,
               "embed": cmd_embed,
               "check": cmd_check, "selftest": cmd_selftest}[args.cmd]
    try:
        return handler(args)
    except QaError as e:
        out(f"出错了：{e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
