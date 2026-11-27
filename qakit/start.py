"""环境检查和安装依赖、建工作区、切换工作区、配置被测服务。"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from .common import ACCESS, CONFIG, DEFAULT_RISKS, DEFAULT_TARGET, EXAMPLE, KIT, out, qa_cmd, read, render, run, write
from .workspace import Ws, git, git_commit, in_git_repo, load_local, save_local, ws_from_args
from .ingest import ingest_paths
from .reqs import new_req
from .autotest import healthy


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
    write(root / "README.md", render(read(KIT / "templates" / "workspace_README.md"), ws.values()))
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
