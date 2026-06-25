#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
knowledge_graph_handler 配置模块（私有，不以 _ 开头可防框架自动加载）

路径解析与默认配置。所有路径基于项目根目录动态解析，不硬编码盘符或绝对路径。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


# =============================================================================
# 路径解析
# =============================================================================

def get_root_dir() -> str:
    """从当前文件位置反推项目根目录。

    当前文件: Tools/knowledge_graph_handler/_config.py
    上三层 → 项目根目录
    """
    return os.path.dirname(
        os.path.dirname(
            os.path.dirname(os.path.abspath(__file__))
        )
    )


def get_storage_root(custom_path: Optional[str] = None) -> str:
    """获取存储根目录，支持自定义覆盖。

    Args:
        custom_path: 用户自定义存储路径，优先级最高

    Returns:
        存储根目录的绝对路径
    """
    if custom_path:
        return custom_path
    return os.path.join(get_root_dir(), "Memory", "kg")


def ensure_dirs(storage_root: Optional[str] = None) -> dict:
    """确保所有子目录存在并返回子目录路径字典。

    Args:
        storage_root: 存储根目录，不传则使用默认值

    Returns:
        {"graphs": path, "reports": path, "exports": path, "cache": path}
    """
    if storage_root is None:
        storage_root = get_storage_root()

    dirs = {
        "graphs":  os.path.join(storage_root, "graphs"),
        "reports": os.path.join(storage_root, "reports"),
        "exports": os.path.join(storage_root, "exports"),
        "cache":   os.path.join(storage_root, "cache"),
    }
    for d in dirs.values():
        os.makedirs(d, exist_ok=True)
    return dirs


def get_db_path(graph_id: str, storage_root: Optional[str] = None) -> str:
    """获取指定图谱的 SQLite 数据库路径。

    Args:
        graph_id: 图谱 ID
        storage_root: 存储根目录

    Returns:
        数据库文件完整路径
    """
    if storage_root is None:
        storage_root = get_storage_root()
    graphs_dir = os.path.join(storage_root, "graphs")
    os.makedirs(graphs_dir, exist_ok=True)
    return os.path.join(graphs_dir, f"{graph_id}.db")


# =============================================================================
# 默认配置
# =============================================================================

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200
DEFAULT_DEPTH = 1
MAX_DEPTH = 3

# SQLite PRAGMA 配置（建图读多写多场景优化）
SQLITE_PRAGMAS = [
    "PRAGMA journal_mode=WAL;",
    "PRAGMA synchronous=NORMAL;",
    "PRAGMA cache_size=-8000;",
    "PRAGMA temp_store=MEMORY;",
    "PRAGMA mmap_size=268435456;",
    "PRAGMA foreign_keys=ON;",
]
