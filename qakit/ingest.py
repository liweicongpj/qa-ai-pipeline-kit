"""导入资料：复制进 raw/ 或接口文档目录、记来源、打码、资料变化检测。"""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from .common import LEAK_PATTERNS, MAX_FILE, QaError, TEXT_EXT, now_iso, out, qa_cmd, read, sha256, write
from .workspace import ws_from_args

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .workspace import Ws


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
    from .access import har_to_markdown  # noqa: E402  上层模块，在函数里导入免得循环
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
