"""
记忆核心层 — 分层因果记忆网络

层级：
  1 宇宙 — Meta：整个系统的世界观、核心原则
  2 星系 — 领域/大主题：自主性、工具链、项目、关系……
  3 恒星 — 核心概念：自我模型、因果网络、记忆……
  4 行星 — 事件簇：会话/阶段
  5 街道 — 具体事件：单轮对话/单次工具调用序列
  6 物品 — 原子事实：一条关键断言
  7 分子/夸克 — 证据/原始数据

设计原则：
  1. 按需展开，不是全部加载
  2. 每一层都是可独立理解的摘要
  3. 纵向包含 vs 横向因果

零外部依赖，只用 Python 标准库。
"""

from .node import MemoryNode, CausalLink
from .store import MemoryStore
from .nav import MemoryNavigator
from .search import MemorySearch
from .propagate import MemoryPropagator
from .aging import MemoryAging
from .api import MemoryAPI, create_api
from .embedding import EmbeddingService
from .schema import (
    LEVELS,
    LEVEL_NAMES,
    LEVEL_DIRS,
    STORAGE_ROOT,
    CausalLinkType,
    SourceType,
    VERSION,
)

__all__ = [
    "MemoryNode",
    "CausalLink",
    "MemoryStore",
    "MemoryNavigator",
    "MemorySearch",
    "MemoryPropagator",
    "MemoryAging",
    "MemoryAPI",
    "create_api",
    "EmbeddingService",
    "LEVELS",
    "LEVEL_NAMES",
    "LEVEL_DIRS",
    "STORAGE_ROOT",
    "CausalLinkType",
    "SourceType",
    "VERSION",
]
