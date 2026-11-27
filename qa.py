#!/usr/bin/env python3
"""AI 测试管线：资料 → 知识库 → AI 生成测试点 → 人工审核 → 接口自动化 → 缺陷回流 → 指标；可接多家模型、做选型对比。

    python qa.py status      现在该做什么（AI 每次开工先跑这个）
    python qa.py next        下一件事的详细做法
    python qa.py --help      全部命令

本文件只用 Python 标准库；跑接口用例另需 pytest、httpx，示例服务另需 fastapi、uvicorn，调 Claude 接口另需 anthropic（python qa.py setup）。

代码按功能分在 qakit/ 里（见 docs/开发说明.md）；本文件只是入口。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from qakit import *  # noqa: E402,F401,F403  兼容：import qa 之后 qa.Ws、qa.open_index 这些照常能用
from qakit.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
