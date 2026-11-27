"""模型接入：所有模型统一成 call(模型配置, system, user) → Result（正文、token、耗时、费用）。

支持的接入方式（provider）：
- anthropic：Claude，走官方 anthropic SDK（pip install anthropic），流式请求
- openai：OpenAI 兼容接口（OpenAI、DeepSeek、通义千问、Kimi、智谱、本地 Ollama / vLLM 等），走 HTTP
- gemini：Google Gemini，走 HTTP
- cli：命令行 AI 工具（claude、codex 等），把提示词从标准输入喂进去，读它的输出
- mock：假模型，不花钱，用于自检和熟悉流程

模型清单放在框架的 models.local.json（自己建，不进 git），格式见 models.example.json；
工作区 qa.config.json 的 "models" 可以覆盖或补充。密钥只放环境变量，配置里只写环境变量名。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path

KIT = Path(__file__).resolve().parent


@dataclass
class Result:
    ok: bool
    text: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    seconds: float = 0.0
    cost_usd: float | None = None
    error: str = ""
    provider: str = ""
    model_id: str = ""
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("text")
        return d


class ModelError(Exception):
    pass


# ───────────────────────── 模型清单 ─────────────────────────

def load_registry(ws_cfg: dict | None = None) -> dict:
    reg = {}
    local = KIT / "models.local.json"
    if local.exists():
        reg.update(json.loads(local.read_text(encoding="utf-8")).get("models", {}))
    if ws_cfg:
        reg.update(ws_cfg.get("models", {}))
    reg.setdefault("mock", {"provider": "mock", "note": "假模型，不花钱，用来熟悉流程和自检"})
    return reg


def pick_model(reg: dict, name: str | None, stage: str, ws_cfg: dict | None) -> tuple[str, dict]:
    if not name:
        name = (ws_cfg or {}).get("stage_models", {}).get(stage) or (ws_cfg or {}).get("stage_models", {}).get("default")
    if not name:
        raise ModelError(f"没指定模型：加 --model <名字>，或在 qa.config.json 的 stage_models.{stage} 里配默认模型。可选：{', '.join(reg)}")
    if name not in reg:
        raise ModelError(f"模型清单里没有 {name}。可选：{', '.join(reg)}（清单在 models.local.json，格式见 models.example.json）")
    return name, reg[name]


def cost_of(m: dict, r: Result) -> float | None:
    if r.cost_usd is not None:
        return r.cost_usd
    pin, pout = m.get("price_in"), m.get("price_out")
    if pin is None or pout is None:
        return None
    # 缓存写入按 1.25 倍、缓存读取按 0.1 倍输入价估算（Claude 的计价方式；其他家以各自账单为准）
    tin = r.input_tokens + r.cache_write_tokens * 1.25 + r.cache_read_tokens * 0.1
    return round(tin * pin / 1e6 + r.output_tokens * pout / 1e6, 6)


def call(m: dict, system: str, user: str, cwd: Path | None = None) -> Result:
    provider = m.get("provider", "")
    fn = {"anthropic": _anthropic, "openai": _openai, "gemini": _gemini, "cli": _cli, "mock": _mock}.get(provider)
    if fn is None:
        return Result(ok=False, error=f"不认识的 provider：{provider}", provider=provider)
    t0 = time.monotonic()
    try:
        r = fn(m, system, user, cwd)
    except ModelError as e:
        r = Result(ok=False, error=str(e))
    except Exception as e:  # noqa: BLE001  网络、解析等各种意外都记成失败，不中断批量对比
        r = Result(ok=False, error=f"{type(e).__name__}: {e}")
    r.seconds = r.seconds or round(time.monotonic() - t0, 2)
    r.provider = provider
    r.model_id = r.model_id or m.get("model", "")
    r.cost_usd = cost_of(m, r)
    return r


def _key(m: dict, required: bool = True) -> str:
    env = m.get("api_key_env") or ""
    val = os.environ.get(env, "") if env else ""
    if required and env and not val:
        raise ModelError(f"环境变量 {env} 没设（这个模型的密钥放在那里）")
    return val


# ───────────────────────── Claude（官方 SDK） ─────────────────────────

def _anthropic(m: dict, system: str, user: str, cwd) -> Result:
    try:
        import anthropic
    except ImportError as e:
        raise ModelError("要用 Claude 接口，先安装官方 SDK：pip install anthropic") from e
    env = m.get("api_key_env") or ""
    # 环境变量设了就用它；没设时让 SDK 自己找凭据（ANTHROPIC_API_KEY、ant auth login 的登录信息等）
    client = anthropic.Anthropic(api_key=os.environ[env]) if env and os.environ.get(env) else anthropic.Anthropic()
    kwargs = {
        "model": m.get("model", "claude-opus-5-5"),
        "max_tokens": int(m.get("max_tokens", 64000)),
        "system": system,
        "messages": [{"role": "user", "content": user}],
        "cache_control": {"type": "ephemeral"},  # 同一份知识库反复用时省钱
    }
    if m.get("effort"):
        kwargs["output_config"] = {"effort": m["effort"]}
    fallbacks = m.get("fallbacks")
    if fallbacks:
        # 被安全分类器拒绝时，服务端自动换模型重跑（Opus 5.5 / Sonnet 5.5 / Fable 系列默认开）
        stream = client.beta.messages.stream(betas=["server-side-fallback-2026-07-01"], fallbacks=fallbacks, **kwargs)
    else:
        stream = client.messages.stream(**kwargs)
    try:
        with stream as s:
            msg = s.get_final_message()
    except anthropic.AuthenticationError as e:
        raise ModelError("Claude 密钥无效或没配：设 ANTHROPIC_API_KEY，或运行 ant auth login") from e
    except anthropic.RateLimitError as e:
        raise ModelError(f"Claude 限流了，稍后再试：{e.message}") from e
    except anthropic.APIStatusError as e:
        raise ModelError(f"Claude 接口报错 {e.status_code}：{e.message}") from e
    except anthropic.APIConnectionError as e:
        raise ModelError("连不上 Claude 接口，检查网络或代理") from e
    u = msg.usage
    r = Result(ok=True, model_id=msg.model,
               text="".join(b.text for b in msg.content if getattr(b, "type", "") == "text"),
               input_tokens=getattr(u, "input_tokens", 0) or 0, output_tokens=getattr(u, "output_tokens", 0) or 0,
               cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
               cache_write_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0)
    if msg.stop_reason == "refusal":
        cat = getattr(getattr(msg, "stop_details", None), "category", None)
        r.ok, r.error = False, f"模型拒绝回答（类别 {cat}）"
    elif msg.stop_reason == "max_tokens":
        r.extra["truncated"] = True
    return r


# ───────────────────────── OpenAI 兼容接口 ─────────────────────────

def _post_json(url: str, body: dict, headers: dict, timeout: int) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:500]
        raise ModelError(f"接口报错 {e.code}：{detail}") from e
    except urllib.error.URLError as e:
        raise ModelError(f"连不上 {url}：{e.reason}") from e


def _openai(m: dict, system: str, user: str, cwd) -> Result:
    base = (m.get("base_url") or "https://api.openai.com/v1").rstrip("/")
    key = _key(m, required=bool(m.get("api_key_env")))
    body = {"model": m["model"], "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    if m.get("max_tokens"):
        body[m.get("max_tokens_param", "max_tokens")] = int(m["max_tokens"])
    body.update(m.get("extra_body", {}))
    data = _post_json(base + "/chat/completions", body, {"Authorization": f"Bearer {key}"} if key else {},
                      int(m.get("timeout", 600)))
    choice = (data.get("choices") or [{}])[0]
    u = data.get("usage") or {}
    cached = (u.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0
    r = Result(ok=True, model_id=data.get("model", m["model"]), text=(choice.get("message") or {}).get("content") or "",
               input_tokens=(u.get("prompt_tokens", 0) or 0) - cached, output_tokens=u.get("completion_tokens", 0) or 0,
               cache_read_tokens=cached)
    if choice.get("finish_reason") == "length":
        r.extra["truncated"] = True
    return r


# ───────────────────────── Gemini ─────────────────────────

def _gemini(m: dict, system: str, user: str, cwd) -> Result:
    key = _key(m)
    base = (m.get("base_url") or "https://generativelanguage.googleapis.com/v1beta").rstrip("/")
    body = {"systemInstruction": {"parts": [{"text": system}]}, "contents": [{"role": "user", "parts": [{"text": user}]}]}
    if m.get("max_tokens"):
        body["generationConfig"] = {"maxOutputTokens": int(m["max_tokens"])}
    data = _post_json(f"{base}/models/{m['model']}:generateContent", body, {"x-goog-api-key": key}, int(m.get("timeout", 600)))
    cand = (data.get("candidates") or [{}])[0]
    u = data.get("usageMetadata") or {}
    text = "".join(p.get("text", "") for p in (cand.get("content") or {}).get("parts", []) if not p.get("thought"))
    return Result(ok=bool(text), text=text, error="" if text else f"没有返回内容：{json.dumps(cand, ensure_ascii=False)[:300]}",
                  input_tokens=u.get("promptTokenCount", 0) or 0,
                  output_tokens=(u.get("candidatesTokenCount", 0) or 0) + (u.get("thoughtsTokenCount", 0) or 0),
                  cache_read_tokens=u.get("cachedContentTokenCount", 0) or 0)


# ───────────────────────── 命令行 AI 工具 ─────────────────────────

def _cli(m: dict, system: str, user: str, cwd) -> Result:
    cmd = m.get("command")
    if not cmd:
        raise ModelError("cli 模型要配 command，例如 claude -p --output-format json")
    prompt = f"{system}\n\n{user}"
    # 默认在空的临时文件夹里运行：否则 claude / codex 会把当前文件夹的 AGENTS.md、CLAUDE.md 当成自己的指令读进去
    run_cwd = m.get("cwd") or tempfile.mkdtemp(prefix="qa-cli-")
    t0 = time.monotonic()
    try:
        p = subprocess.run(cmd, shell=True, input=prompt, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           cwd=run_cwd, timeout=int(m.get("timeout", 1800)))
    finally:
        if not m.get("cwd"):
            shutil.rmtree(run_cwd, ignore_errors=True)
    secs = round(time.monotonic() - t0, 2)
    if p.returncode != 0 and not p.stdout.strip():
        raise ModelError(f"命令退出码 {p.returncode}：{(p.stderr or '').strip()[-500:]}")
    parser = m.get("parser", "text")
    if parser == "claude-json":
        # claude -p --output-format json：{"result": 正文, "usage": {...}, "total_cost_usd": ...}
        d = json.loads(p.stdout)
        u = d.get("usage") or {}
        models = list((d.get("modelUsage") or {}).keys())
        return Result(ok=not d.get("is_error"), text=d.get("result", ""), seconds=secs,
                      error=d.get("result", "") if d.get("is_error") else "",
                      model_id=",".join(models) or m.get("model", ""),
                      input_tokens=u.get("input_tokens", 0) or 0, output_tokens=u.get("output_tokens", 0) or 0,
                      cache_read_tokens=u.get("cache_read_input_tokens", 0) or 0,
                      cache_write_tokens=u.get("cache_creation_input_tokens", 0) or 0,
                      cost_usd=d.get("total_cost_usd"), extra={"turns": d.get("num_turns")})
    if parser == "codex-jsonl":
        # codex exec --json：逐行事件；正文在 agent_message，用量在 turn.completed
        text, usage = [], {}
        for line in p.stdout.splitlines():
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            item = ev.get("item") or {}
            if ev.get("type") == "item.completed" and item.get("type") in ("agent_message", "assistant_message"):
                text.append(item.get("text", ""))
            if ev.get("type") == "turn.completed":
                usage = ev.get("usage") or {}
        cached = usage.get("cached_input_tokens", 0) or 0
        return Result(ok=bool(text), text="\n".join(text), seconds=secs, error="" if text else (p.stderr or p.stdout)[-500:],
                      input_tokens=(usage.get("input_tokens", 0) or 0) - cached, output_tokens=usage.get("output_tokens", 0) or 0,
                      cache_read_tokens=cached)
    return Result(ok=True, text=p.stdout, seconds=secs)


# ───────────────────────── 假模型 ─────────────────────────

def _mock(m: dict, system: str, user: str, cwd) -> Result:
    """按提示词内容生成固定的假结果；不同的 seed 给出不同条数，方便试对比流程。"""
    seed = int(hashlib.sha256((m.get("seed", "") + user[:200]).encode()).hexdigest(), 16)
    if "测试点" in user:
        n = 4 + seed % 4
        rows = ["| 编号 | 测试点 | 风险等级 | 类型 | 依据 | 怎么验证 |", "|---|---|---|---|---|---|"]
        samples = ["同一凭据重复提交只生效一次", "两个请求并发只成交一笔", "回包丢失后用原凭据重试", "跨 0 点的次数归属",
                   "金额只认服务端，客户端传值无效", "次数用完后被拒且不扣次数", "越权使用别人的编号", "配置边界值"]
        for i in range(n):
            rows.append(f"| {i + 1} | {samples[(seed + i) % len(samples)]}（假模型{m.get('seed', '')}） | 高 | 异常 | wiki/risks/风险清单.md | 对账 |")
        text = "\n".join(rows) + "\n\n## 需要和策划确认的问题\n- （假模型不会提问题）\n"
    else:
        text = "（假模型的回答）\n"
    return Result(ok=True, text=text, input_tokens=len(system + user) // 2, output_tokens=len(text) // 2,
                  seconds=round(0.01 + (seed % 100) / 1000, 3), model_id=f"mock{m.get('seed', '')}")
