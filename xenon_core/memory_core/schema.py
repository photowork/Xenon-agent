"""
schema.py — 常量、字段定义、版本号

零外部依赖，纯 Python 标准库。
"""

import os
from enum import Enum
from pathlib import Path

# ============================================================
# 版本号
# ============================================================
VERSION = "1.0.0"

# ============================================================
# 层级定义
# ============================================================
LEVELS: dict = {
    1: "宇宙",
    2: "星系",
    3: "恒星",
    4: "行星",
    5: "街道",
    6: "物品",
    7: "分子/夸克",
}

LEVEL_NAMES: dict = {
    "meta": 1,
    "galaxy": 2,
    "star": 3,
    "planet": 4,
    "street": 5,
    "item": 6,
    "molecule": 7,
}

# 层级 → 目录名
LEVEL_DIRS: dict = {
    1: "meta",
    2: "galaxy",
    3: "star",
    4: "planet",
    5: "street",
    6: "item",
    7: "molecule",
}

# ============================================================
# 存储根目录
# ============================================================
# 动态计算：始终指向项目根目录的 .memory/，不依赖硬编码路径
# schema.py 位于 xenon_core/memory_core/，向上三级即项目根目录
_STORAGE_CANDIDATE = Path(__file__).resolve().parent.parent.parent / ".memory"
if _STORAGE_CANDIDATE.exists():
    STORAGE_ROOT = _STORAGE_CANDIDATE
else:
    # 兜底：如果当前路径不存在 .memory，尝试环境变量或 CWD
    _env_root = os.environ.get("XENON_PROJECT_ROOT", "")
    if _env_root:
        STORAGE_ROOT = Path(_env_root) / ".memory"
    else:
        STORAGE_ROOT = Path(".") / ".memory"

# ============================================================
# 因果链接类型
# ============================================================
class CausalLinkType(Enum):
    CAUSES = "causes"          # A 导致 B
    LEADS_TO = "leads_to"     # A 引导到 B
    CONTRADICTS = "contradicts"  # A 与 B 矛盾
    SUPPORTS = "supports"     # A 支持 B
    REFINES = "refines"       # A 细化 B
    DEPENDS_ON = "depends_on"  # A 依赖 B
    RELATES_TO = "relates_to"  # A 与 B 相关

# ============================================================
# 知识来源类型
# ============================================================
class SourceType(Enum):
    TOOL_VERIFIED = "tool_verified"      # 通过工具验证
    MEMORY_RETRIEVED = "memory_retrieved"  # 从记忆检索
    INFERRED = "inferred"               # 推理得出
    SPECULATED = "speculated"           # 推测（低置信度）
    USER_STATED = "user_stated"         # 用户告知
    AUTO_GENERATED = "auto_generated"   # 系统自动生成

# ============================================================
# 老化参数
# ============================================================
DEFAULT_MAX_AGE_DAYS = 30
DEFAULT_MIN_ACCESS_COUNT = 2
DEFAULT_PROPAGATION_THRESHOLD = 0.15  # 摘要变化阈值

# ============================================================
# 搜索参数
# ============================================================
DEFAULT_SEARCH_LIMIT = 10
DEFAULT_CONTEXT_DEPTH = 2
