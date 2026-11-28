"""模型接入：所有模型统一成 call(模型配置, system, user) → Result（正文、token、耗时、费用）。

支持的接入方式（provider）：
- anthropic：Claude，走官方 anthropic SDK（pip install anthropic），流式请求
- openai：OpenAI 兼容接口（OpenAI、DeepSeek、通义千问、Kimi、智谱、本地 Ollama / vLLM 等），走 HTTP
- gemini：Google Gemini，走 HTTP
- cli：命令行 AI 工具（claude、codex 等），把提示词从标准输入喂进去，读它的输出
- mock：假模型，不花钱，用于自检和熟悉流程

模型清单放在框架的 models.local.json（自己建，不进 git），格式见 models.example.json；
工作区 qa.config.json 的 "models" 可以覆盖或补充。密钥只放环境变量，配置里只写环境变量名。

工具（tools）：call(..., tools=[...], run_tool=函数) 时，anthropic、openai、mock 会让模型边查边写——模型要查资料就调工具，
框架执行后把结果还给它，直到它给出最终结果（最多 max_turns 轮）。gemini、cli 不走工具，只用提示词里已经放好的资料。
工具的格式：{"name": "search", "description": "...", "schema": JSON Schema（object）}。
"""
from __future__ import annotations

import hashlib
import json
import re
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


class TransientError(ModelError):
    """临时故障（限流、服务端 5xx、网络断、超时）：过一会儿重试可能就好。"""


TOOL_PROVIDERS = ("anthropic", "openai", "mock")
# 查资料只剩最后一轮时追加的话：光禁止调工具，有的模型（DeepSeek 思考模式实测）会把想调的工具当成文字写进结果
LAST_TURN = "查资料的次数用完了：不要再调工具，用已经查到的资料，直接按任务要求给出最终结果。"


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


def call(m: dict, system: str, user: str, cwd: Path | None = None, tools: list[dict] | None = None,
         run_tool=None, max_turns: int = 10, deadline: float | None = None) -> Result:
    """调一次模型。给了 tools 和 run_tool(名字, 参数) -> 文本 时，支持工具的接入方式会多轮调用，直到模型给出最终结果。
    deadline（time.monotonic() 的时刻）：过了这个时间，下一轮就让模型别再查、直接给结果（防止一步卡太久）。"""
    provider = m.get("provider", "")
    fn = {"anthropic": _anthropic, "openai": _openai, "gemini": _gemini, "cli": _cli, "mock": _mock}.get(provider)
    if fn is None:
        return Result(ok=False, error=f"不认识的 provider：{provider}", provider=provider)
    use_tools = bool(tools and run_tool and provider in TOOL_PROVIDERS and not m.get("no_tools"))
    t0 = time.monotonic()
    try:
        if use_tools:
            r = fn(m, system, user, cwd, tools=tools, run_tool=_safe_tool(run_tool), max_turns=max_turns, deadline=deadline)
        else:
            r = fn(m, system, user, cwd)
    except TransientError as e:
        r = Result(ok=False, error=str(e), extra={"transient": True})
    except ModelError as e:
        r = Result(ok=False, error=str(e))
    except (TimeoutError, ConnectionError) as e:
        r = Result(ok=False, error=f"{type(e).__name__}: {e}", extra={"transient": True})
    except Exception as e:  # noqa: BLE001  网络、解析等各种意外都记成失败，不中断批量对比
        r = Result(ok=False, error=f"{type(e).__name__}: {e}")
    r.seconds = r.seconds or round(time.monotonic() - t0, 2)
    r.provider = provider
    r.model_id = r.model_id or m.get("model", "")
    r.cost_usd = cost_of(m, r)
    r.extra.setdefault("tools", "用了" if use_tools else ("不支持" if tools else "没给"))
    return r


def _late(deadline: float | None) -> bool:
    return deadline is not None and time.monotonic() >= deadline


def _safe_tool(run_tool):
    """工具出错不中断：把错误当成工具结果还给模型，让它换个查法。"""
    def run(name: str, args: dict) -> str:
        try:
            return str(run_tool(name, args or {}))
        except Exception as e:  # noqa: BLE001
            return f"工具出错：{type(e).__name__}: {e}"
    return run


def _add_usage(total: Result, part: Result) -> None:
    for k in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"):
        setattr(total, k, getattr(total, k) + getattr(part, k))


def _key(m: dict, required: bool = True) -> str:
    env = m.get("api_key_env") or ""
    val = os.environ.get(env, "") if env else ""
    if required and env and not val:
        raise ModelError(f"环境变量 {env} 没设（这个模型的密钥放在那里）")
    return val


# ───────────────────────── Claude（官方 SDK） ─────────────────────────

def _anthropic(m: dict, system: str, user: str, cwd, tools=None, run_tool=None, max_turns: int = 10, deadline=None) -> Result:
    try:
        import anthropic
    except ImportError as e:
        raise ModelError("要用 Claude 接口，先安装官方 SDK：pip install anthropic") from e
    env = m.get("api_key_env") or ""
    # 环境变量设了就用它；没设时让 SDK 自己找凭据（ANTHROPIC_API_KEY、ant auth login 的登录信息等）
    client = anthropic.Anthropic(api_key=os.environ[env]) if env and os.environ.get(env) else anthropic.Anthropic()
    msgs: list[dict] = [{"role": "user", "content": user}]
    base = {
        "model": m.get("model", "claude-opus-5-5"),
        "max_tokens": int(m.get("max_tokens", 64000)),
        "system": system,
        "cache_control": {"type": "ephemeral"},  # 同一份知识库反复用时省钱；多轮查资料时前面的内容也走缓存
    }
    if m.get("effort"):
        base["output_config"] = {"effort": m["effort"]}
    if tools:
        base["tools"] = [{"name": t["name"], "description": t["description"], "input_schema": t["schema"]} for t in tools]
    fallbacks = m.get("fallbacks")
    total, calls, nudged = Result(ok=True), [], False
    for turn in range(max_turns + 1):
        final = turn == max_turns or nudged
        kwargs = {**base, "messages": msgs}
        if tools and final:
            kwargs["tool_choice"] = {"type": "none"}  # 查的次数用完了：不许再调工具，直接给结果
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
            raise TransientError(f"Claude 限流了，稍后再试：{e.message}") from e
        except anthropic.APIStatusError as e:
            err = TransientError if e.status_code >= 500 or e.status_code == 429 else ModelError
            raise err(f"Claude 接口报错 {e.status_code}：{e.message}") from e
        except anthropic.APIConnectionError as e:
            raise TransientError("连不上 Claude 接口，检查网络或代理") from e
        u = msg.usage
        _add_usage(total, Result(ok=True, input_tokens=getattr(u, "input_tokens", 0) or 0, output_tokens=getattr(u, "output_tokens", 0) or 0,
                                 cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
                                 cache_write_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0))
        total.model_id = msg.model
        uses = [b for b in msg.content if getattr(b, "type", "") == "tool_use"]
        if msg.stop_reason == "tool_use" and uses and run_tool and not final:
            msgs.append({"role": "assistant", "content": msg.content})
            results = []
            for b in uses:
                res = run_tool(b.name, dict(b.input or {}))
                calls.append({"name": b.name, "args": dict(b.input or {}), "chars": len(res)})
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": res})
            if turn + 1 == max_turns or _late(deadline):
                results.append({"type": "text", "text": LAST_TURN})
                nudged = True
            msgs.append({"role": "user", "content": results})
            continue
        total.text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        if msg.stop_reason == "refusal":
            cat = getattr(getattr(msg, "stop_details", None), "category", None)
            total.ok, total.error = False, f"模型拒绝回答（类别 {cat}）"
        elif msg.stop_reason == "max_tokens":
            total.extra["truncated"] = True
        break
    if calls:
        total.extra["tool_calls"] = calls
    return total


# ───────────────────────── OpenAI 兼容接口 ─────────────────────────

def _post_json(url: str, body: dict, headers: dict, timeout: int) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:500]
        err = TransientError if e.code == 429 or e.code >= 500 else ModelError
        raise err(f"接口报错 {e.code}：{detail}") from e
    except urllib.error.URLError as e:
        raise TransientError(f"连不上 {url}：{e.reason}") from e
    except TimeoutError as e:
        raise TransientError(f"{url} 超过 {timeout} 秒没响应") from e


def _openai(m: dict, system: str, user: str, cwd, tools=None, run_tool=None, max_turns: int = 10, deadline=None) -> Result:
    base = (m.get("base_url") or "https://api.openai.com/v1").rstrip("/")
    key = _key(m, required=bool(m.get("api_key_env")))
    msgs: list[dict] = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    total, calls, nudged = Result(ok=True, model_id=m["model"]), [], False
    for turn in range(max_turns + 1):
        final = turn == max_turns or nudged
        body = {"model": m["model"], "messages": msgs}
        if m.get("max_tokens"):
            body[m.get("max_tokens_param", "max_tokens")] = int(m["max_tokens"])
        if tools:
            body["tools"] = [{"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["schema"]}}
                             for t in tools]
            if final:
                body["tool_choice"] = "none"  # 查的次数或时间用完了：直接给结果
        body.update(m.get("extra_body", {}))
        data = _post_json(base + "/chat/completions", body, {"Authorization": f"Bearer {key}"} if key else {},
                          int(m.get("timeout", 600)))
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        u = data.get("usage") or {}
        cached = (u.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0
        _add_usage(total, Result(ok=True, input_tokens=(u.get("prompt_tokens", 0) or 0) - cached,
                                 output_tokens=u.get("completion_tokens", 0) or 0, cache_read_tokens=cached))
        total.model_id = data.get("model", m["model"])
        if msg.get("tool_calls") and run_tool and not final:
            back = {"role": "assistant", "content": msg.get("content") or "", "tool_calls": msg["tool_calls"]}
            if msg.get("reasoning_content"):  # DeepSeek 思考模式：同一轮里要把思考内容带回去
                back["reasoning_content"] = msg["reasoning_content"]
            msgs.append(back)
            for tc in msg["tool_calls"]:
                fn = tc.get("function") or {}
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except ValueError:
                    args = {}
                res = run_tool(fn.get("name", ""), args)
                calls.append({"name": fn.get("name", ""), "args": args, "chars": len(res)})
                msgs.append({"role": "tool", "tool_call_id": tc.get("id", ""), "content": res})
            if turn + 1 == max_turns or _late(deadline):
                msgs.append({"role": "user", "content": LAST_TURN})
                nudged = True
            continue
        # Qwen3、DeepSeek 开源版等本地部署时，思考过程可能以 <think>…</think> 混在正文前面：去掉，只留结果
        total.text = re.sub(r"^\s*<think>.*?</think>\s*", "", msg.get("content") or "", flags=re.S)
        if choice.get("finish_reason") == "length":
            total.extra["truncated"] = True
        break
    if calls:
        total.extra["tool_calls"] = calls
    return total


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

_MOCK_COUNT: dict = {}


def _mock(m: dict, system: str, user: str, cwd, tools=None, run_tool=None, max_turns: int = 10, deadline=None) -> Result:
    """按提示词内容生成固定的假结果；不同的 seed 给出不同条数，方便试对比流程。

    自检用的写法（按提示词里的“任务代号：xxx”认步骤）：
    - "replies": {"步骤": "回答" 或 ["第 1 次的回答", "第 2 次的回答", ...]}
    - "errors": {"步骤": ["transient", "error", "ok", ...]}：第几次调用先报临时故障 / 普通错误
    - "tool_calls": {"步骤": [{"name": "search", "args": {...}}]}：先调这些工具，结果的第一行填进回答里的 {{tool:序号}}
    """
    import re
    step_m = re.search(r"任务代号：([\w\-]+)", system + user)
    step = step_m.group(1) if step_m else ""
    key = (m.get("seed", ""), step)
    n_call = _MOCK_COUNT.get(key, 0)
    _MOCK_COUNT[key] = n_call + 1
    errs = (m.get("errors") or {}).get(step) or []
    if n_call < len(errs) and errs[n_call] == "transient":
        raise TransientError("假模型：临时故障（模拟限流）")
    if n_call < len(errs) and errs[n_call] == "error":
        raise ModelError("假模型：调用失败（模拟密钥不对）")
    calls = []
    if tools and run_tool:
        for tc in ((m.get("tool_calls") or {}).get(step) or [])[:max_turns]:
            res = run_tool(tc["name"], tc.get("args") or {})
            calls.append({"name": tc["name"], "args": tc.get("args") or {}, "chars": len(res), "first": next((x for x in res.splitlines() if x.strip()), "")})
    replies = (m.get("replies") or {}).get(step)
    if replies is not None:
        text = replies[min(n_call - len([e for e in errs[:n_call] if e != "ok"]), len(replies) - 1)] if isinstance(replies, list) else replies
        for i, c in enumerate(calls):
            text = text.replace("{{tool:%d}}" % i, c["first"])
        r = Result(ok=True, text=text, input_tokens=len(system + user) // 2, output_tokens=len(text) // 2, seconds=0.01, model_id=f"mock{m.get('seed', '')}")
        if calls:
            r.extra["tool_calls"] = [{k: v for k, v in c.items() if k != "first"} for c in calls]
        return r
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
