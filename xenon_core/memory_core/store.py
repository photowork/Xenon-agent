"""
store.py — 文件系统持久化

纯文件系统存储，不依赖任何工具模块。
只要磁盘不坏、路径不变，记忆就在。
"""

import json
import os
from pathlib import Path
from typing import Optional, List

from .node import MemoryNode
from .schema import LEVEL_DIRS, STORAGE_ROOT


class MemoryStore:
    """
    基于文件系统的记忆持久化层。

    每个 MemoryNode 存储为 .memory/{level_dir}/{node_id}.json
    """

    def __init__(self, storage_root: Path = None):
        self.storage_root = storage_root or STORAGE_ROOT
        self._ensure_directories()

    # ======== 目录管理 ========

    def _ensure_directories(self):
        """确保所有层级目录存在"""
        for dir_name in LEVEL_DIRS.values():
            dir_path = self.storage_root / dir_name
            dir_path.mkdir(parents=True, exist_ok=True)
        # cache 目录用于临时/中间层数据
        (self.storage_root / "cache").mkdir(parents=True, exist_ok=True)

    def _get_file_path(self, node_id: str, level: int = None) -> Optional[Path]:
        """根据 node_id 前缀和 level 推断文件路径"""
        if level is not None:
            dir_name = LEVEL_DIRS.get(level, "cache")
            return self.storage_root / dir_name / f"{node_id}.json"

        # 没给 level 时，遍历所有目录查找
        for dir_name in list(LEVEL_DIRS.values()) + ["cache"]:
            file_path = self.storage_root / dir_name / f"{node_id}.json"
            if file_path.exists():
                return file_path
        return None

    # ======== CRUD ========

    def save(self, node: MemoryNode) -> str:
        """
        写入节点到对应层级目录。
        返回 node_id。
        """
        file_path = self._get_file_path(node.node_id, node.level)
        if file_path is None:
            file_path = self.storage_root / "cache" / f"{node.node_id}.json"

        # 确保父目录存在
        file_path.parent.mkdir(parents=True, exist_ok=True)

        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(node.to_dict(), f, ensure_ascii=False, indent=2)

        return node.node_id

    def load(self, node_id: str) -> Optional[MemoryNode]:
        """
        跨所有层级目录查找并加载节点。
        找不到返回 None。
        """
        file_path = self._get_file_path(node_id)
        if file_path is None or not file_path.exists():
            return None

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return MemoryNode.from_dict(data)
        except (json.JSONDecodeError, KeyError) as e:
            print(f"[MemoryStore] 加载 {node_id} 失败: {e}")
            return None

    def delete(self, node_id: str) -> bool:
        """删除节点文件。成功返回 True。"""
        file_path = self._get_file_path(node_id)
        if file_path is None or not file_path.exists():
            return False

        file_path.unlink()
        return True

    def exists(self, node_id: str) -> bool:
        """检查节点是否存在"""
        file_path = self._get_file_path(node_id)
        return file_path is not None and file_path.exists()

    # ======== 查询 ========

    def list_by_level(self, level: int) -> List[MemoryNode]:
        """列出某层级的所有节点"""
        dir_name = LEVEL_DIRS.get(level, "cache")
        dir_path = self.storage_root / dir_name

        if not dir_path.exists():
            return []

        nodes = []
        for f in sorted(dir_path.glob("*.json")):
            try:
                with open(f, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                nodes.append(MemoryNode.from_dict(data))
            except (json.JSONDecodeError, KeyError):
                continue

        return nodes

    def list_all(self) -> List[MemoryNode]:
        """列出所有层级的节点"""
        all_nodes = []
        for level in range(1, 8):
            all_nodes.extend(self.list_by_level(level))
        return all_nodes

    def count_by_level(self) -> dict:
        """统计每层节点数量"""
        counts = {}
        for level in range(1, 8):
            dir_name = LEVEL_DIRS.get(level, "cache")
            dir_path = self.storage_root / dir_name
            if dir_path.exists():
                counts[level] = len(list(dir_path.glob("*.json")))
            else:
                counts[level] = 0
        return counts

    def get_statistics(self) -> dict:
        """获取存储统计信息"""
        total_nodes = 0
        total_size = 0
        for level in range(1, 8):
            dir_name = LEVEL_DIRS.get(level, "cache")
            dir_path = self.storage_root / dir_name
            if dir_path.exists():
                for f in dir_path.glob("*.json"):
                    total_nodes += 1
                    total_size += f.stat().st_size

        return {
            "total_nodes": total_nodes,
            "total_size_bytes": total_size,
            "total_size_mb": round(total_size / (1024 * 1024), 2),
            "by_level": self.count_by_level(),
        }
