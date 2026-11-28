"""接口自动化：被测服务健康检查和启停、跑 pytest、和上次对比。"""
from __future__ import annotations

import os
import re
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

from .common import QaError, now_iso, out, qa_cmd, run, stamp, sub_env, write
from .workspace import git_commit, run_hook, ws_from_args

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .workspace import Ws


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
    # 不写编译缓存：Python 只按“文件大小 + 修改时间（精确到秒）”判断缓存过没过期，
    # 一秒内改了用例或不变量、长度又刚好没变，就会跑到旧代码
    r = run(cmd, cwd=ws.auto, env=sub_env({"QA_TARGET_URL": t["base_url"], "QA_TARGET": tname, "PYTHONDONTWRITEBYTECODE": "1"}), timeout=3600)
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
    if not summary["total"]:
        return 1
    return 1 if failed and getattr(args, "exit_code", False) else 0  # Jenkins 靠退出码判断：加 --exit-code 时有失败就返回 1
