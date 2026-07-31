"""
aging.py — 老化与合并机制

自动维护记忆健康：
- 底层节点（level >= 6）超过 max_age_days 且低访问 → 合并后删除
- 高层节点（level <= 3）不自动删除，只标记"陈旧"
"""

from datetime import datetime, timedelta
from typing import List, Optional

from .node import MemoryNode
from .store import MemoryStore
from .nav import MemoryNavigator
from .propagate import MemoryPropagator
from .schema import DEFAULT_MAX_AGE_DAYS, DEFAULT_MIN_ACCESS_COUNT


class MemoryAging:
    """记忆老化管理器"""

    def __init__(self, store: MemoryStore, nav: MemoryNavigator,
                 propagator: MemoryPropagator):
        self.store = store
        self.nav = nav
        self.propagator = propagator

    # ======== 定期老化检查 ========

    def age(self, max_age_days: int = DEFAULT_MAX_AGE_DAYS,
            min_access_count: int = DEFAULT_MIN_ACCESS_COUNT) -> int:
        """
        执行一轮老化清理。

        策略：
        - 底层节点（level >= 6）：超期且低访问 → 向上传播摘要后删除
        - 中层节点（level 4-5）：超期且低访问 → 标记为 'stale'
        - 高层节点（level <= 3）：不做自动删除，仅标记陈旧

        Returns:
            清理的节点数量
        """
        now = datetime.now()
        cleaned = 0

        # 从最底层开始
        for level in [7, 6, 5, 4]:
            for node in self.store.list_by_level(level):
                try:
                    last_access = datetime.fromisoformat(node.last_accessed)
                except (ValueError, TypeError):
                    last_access = datetime.min

                age_days = (now - last_access).days

                if age_days <= max_age_days:
                    continue

                if node.access_count >= min_access_count:
                    continue

                if level >= 6:
                    # 向上传播摘要后删除
                    self.propagator.propagate(node.node_id)
                    self.store.delete(node.node_id)
                    cleaned += 1
                else:
                    # 标记陈旧
                    if "stale" not in node.tags:
                        node.tags.append("stale")
                        self.store.save(node)
                        cleaned += 1

        return cleaned

    # ======== 检查特定节点是否陈旧 ========

    def is_stale(self, node_id: str, max_age_days: int = DEFAULT_MAX_AGE_DAYS) -> bool:
        """检查节点是否陈旧"""
        node = self.store.load(node_id)
        if not node:
            return False

        try:
            last_access = datetime.fromisoformat(node.last_accessed)
        except (ValueError, TypeError):
            return True

        age_days = (datetime.now() - last_access).days
        return age_days > max_age_days

    # ======== 强制合并底层节点 ========

    def merge_children(self, parent_id: str) -> Optional[MemoryNode]:
        """
        将某节点的所有子节点合并到父节点中。
        适用于：底层子节点过多时手动触发合并。

        Args:
            parent_id: 父节点 ID

        Returns:
            更新后的父节点
        """
        parent = self.store.load(parent_id)
        if not parent:
            return None

        children = self.nav.get_children(parent_id)
        if not children:
            return parent

        # 收集子节点内容
        merged_content_parts = [parent.content] if parent.content else []
        merged_summaries = [parent.summary] if parent.summary else []

        for child in children:
            if child.summary:
                merged_summaries.append(f"[{child.title}] {child.summary}")
            if child.content:
                merged_content_parts.append(f"--- {child.title} ---\n{child.content}")

            # 继承子节点的因果链接
            for link in child.links:
                parent.add_link(link.target_id, link.link_type,
                              link.weight, link.description)

            # 删除子节点
            self.store.delete(child.node_id)

        # 更新父节点
        parent.summary = " | ".join(merged_summaries)
        parent.content = "\n\n".join(merged_content_parts)
        parent.children_ids = []
        parent.version += 1
        self.store.save(parent)

        # 向上传播
        self.propagator.propagate(parent.node_id)
        return parent

    # ======== 获取统计信息 ========

    def get_stale_nodes(self, max_age_days: int = DEFAULT_MAX_AGE_DAYS) -> List[MemoryNode]:
        """获取所有陈旧节点"""
        stale = []
        now = datetime.now()

        for node in self.store.list_all():
            try:
                last_access = datetime.fromisoformat(node.last_accessed)
            except (ValueError, TypeError):
                stale.append(node)
                continue

            if (now - last_access).days > max_age_days:
                stale.append(node)

        return stale
