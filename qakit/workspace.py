"""工作区：找工作区、读写配置和进度（Ws）、钩子、git 提交和推送到项目仓库。"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .common import ACCESS, CONFIG, DEFAULT_RISKS, DEFAULT_TARGET, KIT, LOCAL, QaError, out, qa_cmd, read, run, sub_env, write


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
        # 用真实路径：macOS 的 /var 其实是 /private/var，Windows 有 RUNNER~1 这种短路径名、也可能走 junction；
        # 不统一的话，拿文件的真实路径去比工作区路径会对不上（ws.rel 报错）
        root = Path(root).resolve()
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
