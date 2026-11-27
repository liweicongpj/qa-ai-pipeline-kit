"""让测试能 import 仓库根目录下的 qakit、qa_index。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
