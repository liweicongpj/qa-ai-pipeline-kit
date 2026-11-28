"""AI 测试管线的代码：qa.py 是入口，各功能按模块放在这里（每个模块放什么见 docs/开发说明.md）。"""
import sys
from pathlib import Path

_KIT = Path(__file__).resolve().parent.parent
if str(_KIT) not in sys.path:  # qa_models.py、qa_index.py 在仓库根目录
    sys.path.insert(0, str(_KIT))

from .common import *  # noqa: F401,F403
from .workspace import *  # noqa: F401,F403
from .ingest import *  # noqa: F401,F403
from .corpus import *  # noqa: F401,F403
from .wiki import *  # noqa: F401,F403
from .reqs import *  # noqa: F401,F403
from .prompts import *  # noqa: F401,F403
from .gen import *  # noqa: F401,F403
from .steps import *  # noqa: F401,F403
from .access import *  # noqa: F401,F403
from .autotest import *  # noqa: F401,F403
from .metrics import *  # noqa: F401,F403
from .regress import *  # noqa: F401,F403
from .status import *  # noqa: F401,F403
from .start import *  # noqa: F401,F403
from .selftest import *  # noqa: F401,F403
from .cli import *  # noqa: F401,F403
