"""框架生成：给接口用例提供被测服务地址（base_url 夹具），并在每个用例结束后检查不变量。

- 地址优先读环境变量 QA_TARGET_URL（qa.py autotest run 会设好），没设就读工作区 qa.config.json 里对应被测服务的 base_url
- 如果 autotest/invariants.py 存在并定义了 check(base_url) -> list[str]，每个用例结束后自动调用；
  返回的每一条都会让该用例判失败。个别用例不想检查时加 @pytest.mark.no_invariants
"""
import importlib.util
import json
import os
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent


def _configured_url() -> str:
    try:
        cfg = json.loads((HERE.parent / "qa.config.json").read_text(encoding="utf-8"))
    except Exception:
        return ""
    targets = cfg.get("targets") or {"default": cfg.get("target", {})}
    name = os.environ.get("QA_TARGET") or ("default" if "default" in targets else next(iter(targets), ""))
    return (targets.get(name) or {}).get("base_url", "")


def _url() -> str:
    return (os.environ.get("QA_TARGET_URL") or _configured_url()).rstrip("/")


@pytest.fixture(scope="session")
def base_url() -> str:
    url = _url()
    if not url:
        pytest.exit("没配被测服务地址：python qa.py target set default --url http://…，或设环境变量 QA_TARGET_URL")
    return url


def pytest_configure(config):
    config.addinivalue_line("markers", "no_invariants: 这个用例结束后不检查不变量")


_INV = HERE / "invariants.py"
if _INV.exists():
    _spec = importlib.util.spec_from_file_location("qa_invariants", _INV)
    _inv = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_inv)

    @pytest.fixture(autouse=True)
    def _qa_invariants(request):
        yield
        if request.node.get_closest_marker("no_invariants") or not hasattr(_inv, "check"):
            return
        problems = _inv.check(_url())
        if problems:
            pytest.fail("不变量被破坏：" + "；".join(problems), pytrace=False)
