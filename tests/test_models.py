"""模型接入和“每一步直接调模型”的细粒度测试：文件块拆分和越权拦截、工具调用的多轮消息格式、临时故障标记。

OpenAI 兼容接口用本机起的假服务测（不花钱、不联网）；Claude 用假的 SDK 测循环逻辑（真 SDK 没装时也能跑）。
"""
import json
import sys
import threading
import types
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import qa_models
from qakit import steps


# ── 文件块 ──

class RootOnly:
    def __init__(self, root: Path):
        self.root = root.resolve()


def test_parse_blocks_keeps_allowed_and_blocks_others(tmp_path):
    ws = RootOnly(tmp_path)
    text = ("前面的话\n===== 文件：wiki/modules/商城.md =====\n# 商城\n来源：x\n===== 文件结束 =====\n"
            "===== 文件：reqs/R001-x/需求.md =====\n偷改\n===== 文件结束 =====\n"
            "===== 文件：../外面.md =====\n越界\n===== 文件结束 =====\n"
            "===== 文件：wiki/log.md =====\n框架自己记\n===== 文件结束 =====\n## 给人看的说明\n改了商城")
    blocks, probs, note = steps.parse_blocks(ws, text, ["wiki/*.md"], ["wiki/log.md"])
    assert blocks == [("wiki/modules/商城.md", "# 商城\n来源：x\n")]
    assert len(probs) == 3 and any("reqs/R001-x" in p for p in probs) and any("工作区以外" in p for p in probs)
    assert "改了商城" in note and "===== 文件" not in note


def test_parse_blocks_reports_missing_format(tmp_path):
    blocks, probs, _ = steps.parse_blocks(RootOnly(tmp_path), "直接写了正文，没有文件块", ["wiki/*.md"], [])
    assert not blocks and probs and "没找到文件块" in probs[0]


# ── OpenAI 兼容接口：本机假服务 ──

class FakeOpenAI(BaseHTTPRequestHandler):
    bodies: list = []
    mode = "once"  # once：第一次要求调工具，第二次给结果；always：一直要求调工具；down：服务端出错

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])).decode("utf-8"))
        FakeOpenAI.bodies.append(body)
        if FakeOpenAI.mode == "down":
            self.send_response(503)
            self.end_headers()
            self.wfile.write("忙".encode("utf-8"))
            return
        has_tool_result = any(m["role"] == "tool" for m in body["messages"])
        if FakeOpenAI.mode == "always" or not has_tool_result:
            msg = {"role": "assistant", "content": "", "tool_calls": [
                {"id": f"c{len(FakeOpenAI.bodies)}", "type": "function", "function": {"name": "search", "arguments": json.dumps({"query": "鱼竿"}, ensure_ascii=False)}}]}
            finish = "tool_calls"
        else:
            tool_text = next(m["content"] for m in body["messages"] if m["role"] == "tool")
            msg, finish = {"role": "assistant", "content": f"最终结果，查到：{tool_text}"}, "stop"
        data = {"model": "假模型", "choices": [{"message": msg, "finish_reason": finish}], "usage": {"prompt_tokens": 100, "completion_tokens": 10}}
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def serve(mode):
    FakeOpenAI.bodies, FakeOpenAI.mode = [], mode
    srv = HTTPServer(("127.0.0.1", 0), FakeOpenAI)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, {"provider": "openai", "model": "假模型", "base_url": f"http://127.0.0.1:{srv.server_address[1]}/v1"}


TOOL = [{"name": "search", "description": "检索", "schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}]


def test_openai_tool_round_trip():
    srv, m = serve("once")
    seen = []
    try:
        r = qa_models.call(m, "系统", "问题", tools=TOOL, run_tool=lambda n, a: seen.append((n, a)) or "鱼竿 3 档，价格见 gear.rod")
    finally:
        srv.shutdown()
    assert r.ok and "鱼竿 3 档" in r.text, r
    assert seen == [("search", {"query": "鱼竿"})]
    second = FakeOpenAI.bodies[1]["messages"]
    assert second[-2]["role"] == "assistant" and second[-2]["tool_calls"][0]["id"] == "c1"
    assert second[-1] == {"role": "tool", "tool_call_id": "c1", "content": "鱼竿 3 档，价格见 gear.rod"}
    assert FakeOpenAI.bodies[0]["tools"][0]["function"]["name"] == "search"
    assert r.input_tokens == 200 and r.output_tokens == 20 and r.extra["tool_calls"][0]["name"] == "search"


def test_openai_stops_calling_tools_at_max_turns():
    srv, m = serve("always")
    try:
        r = qa_models.call(m, "系统", "问题", tools=TOOL, run_tool=lambda n, a: "结果", max_turns=2)
    finally:
        srv.shutdown()
    assert len(FakeOpenAI.bodies) == 3
    assert "tool_choice" not in FakeOpenAI.bodies[0] and FakeOpenAI.bodies[2]["tool_choice"] == "none"
    assert len(r.extra["tool_calls"]) == 2
    # 最后一轮前追加一句“别再查了，直接给结果”：DeepSeek 实测光禁止调工具会把工具调用当文字写进结果
    assert FakeOpenAI.bodies[2]["messages"][-1] == {"role": "user", "content": qa_models.LAST_TURN}
    assert all(m.get("content") != qa_models.LAST_TURN for m in FakeOpenAI.bodies[1]["messages"])


def test_server_error_is_transient():
    srv, m = serve("down")
    try:
        r = qa_models.call(m, "系统", "问题")
    finally:
        srv.shutdown()
    assert not r.ok and r.extra.get("transient") and "503" in r.error


def test_tool_error_goes_back_to_model():
    srv, m = serve("once")
    try:
        r = qa_models.call(m, "系统", "问题", tools=TOOL, run_tool=lambda n, a: 1 / 0)
    finally:
        srv.shutdown()
    assert r.ok and "工具出错：ZeroDivisionError" in r.text


# ── Claude：假的 SDK，测多轮循环 ──

def fake_anthropic(monkeypatch, replies):
    calls = []
    mod = types.ModuleType("anthropic")
    for name in ("AuthenticationError", "RateLimitError", "APIStatusError", "APIConnectionError"):
        setattr(mod, name, type(name, (Exception,), {}))

    class Stream:
        def __init__(self, kw):
            self.kw = kw

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get_final_message(self):
            calls.append(self.kw)
            return replies[len(calls) - 1]

    class Messages:
        def stream(self, **kw):
            return Stream({**kw, "messages": list(kw["messages"])})

    class Client:
        def __init__(self, **kw):
            self.messages = Messages()
            self.beta = types.SimpleNamespace(messages=Messages())

    mod.Anthropic = Client
    monkeypatch.setitem(sys.modules, "anthropic", mod)
    return calls


def msg(stop, blocks):
    usage = types.SimpleNamespace(input_tokens=50, output_tokens=5, cache_read_input_tokens=0, cache_creation_input_tokens=0)
    return types.SimpleNamespace(stop_reason=stop, content=blocks, usage=usage, model="claude-假")


def test_anthropic_tool_round_trip(monkeypatch):
    use = types.SimpleNamespace(type="tool_use", id="tu1", name="cfg_show", input={"table": "物品表", "where": ["id=101"]})
    calls = fake_anthropic(monkeypatch, [msg("tool_use", [use]), msg("end_turn", [types.SimpleNamespace(type="text", text="品质：紫")])])
    r = qa_models.call({"provider": "anthropic", "model": "claude-假"}, "系统", "查商城物品的品质", tools=TOOL,
                       run_tool=lambda n, a: f"{n} {a['table']} 一行：quality=4")
    assert r.ok and r.text == "品质：紫" and r.input_tokens == 100
    assert calls[0]["tools"][0]["input_schema"]["required"] == ["query"] and "tool_choice" not in calls[0]
    second = calls[1]["messages"]
    assert second[1] == {"role": "assistant", "content": [use]}
    assert second[2]["content"][0] == {"type": "tool_result", "tool_use_id": "tu1", "content": "cfg_show 物品表 一行：quality=4"}


def test_anthropic_last_turn_forbids_tools(monkeypatch):
    use = types.SimpleNamespace(type="tool_use", id="t", name="search", input={"query": "x"})
    calls = fake_anthropic(monkeypatch, [msg("tool_use", [use]), msg("end_turn", [types.SimpleNamespace(type="text", text="好")])])
    r = qa_models.call({"provider": "anthropic"}, "系统", "问题", tools=TOOL, run_tool=lambda n, a: "结果", max_turns=1)
    assert r.ok and calls[1]["tool_choice"] == {"type": "none"}
    assert calls[1]["messages"][-1]["content"][-1] == {"type": "text", "text": qa_models.LAST_TURN}


# ── 假模型（自检用） ──

def test_mock_replies_errors_and_tools():
    m = {"provider": "mock", "seed": "单测", "errors": {"x-step": ["transient"]}, "replies": {"x-step": ["第一次：{{tool:0}}", "第二次"]},
         "tool_calls": {"x-step": [{"name": "search", "args": {"query": "q"}}]}}
    r1 = qa_models.call(m, "（任务代号：x-step）", "u", tools=TOOL, run_tool=lambda n, a: "命中一行\n第二行")
    r2 = qa_models.call(m, "（任务代号：x-step）", "u", tools=TOOL, run_tool=lambda n, a: "命中一行")
    r3 = qa_models.call(m, "（任务代号：x-step）", "u")
    assert not r1.ok and r1.extra["transient"]
    assert r2.ok and r2.text == "第一次：命中一行"
    assert r3.text == "第二次"
