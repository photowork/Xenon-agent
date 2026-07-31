"""
propagate.py — 摘要传播 & 向量传播

当底层节点发生变化时，自动向上传播：
1. 文本摘要更新（自底向上拼接压缩）
2. 语义向量聚合（父向量 = 子向量均值）
3. 向量变化触发跨分支关联重检（场景 C 核心闭环）
"""

from typing import List, Optional

import numpy as np

from .node import MemoryNode
from .store import MemoryStore
from .nav import MemoryNavigator
from .schema import DEFAULT_PROPAGATION_THRESHOLD


class MemoryPropagator:
    """摘要传播器：自底向上更新父节点摘要 + 语义向量"""

    def __init__(self, store: MemoryStore, nav: MemoryNavigator):
        self.store = store
        self.nav = nav

    # ======== 向上传播 ========

    def propagate(self, node_id: str, min_change_ratio: float = DEFAULT_PROPAGATION_THRESHOLD):
        """
        从当前节点向上传播摘要更新。
        如果父节点摘要变化超过阈值，继续递归向上传播。
        """
        node = self.store.load(node_id)
        if not node:
            return

        if not node.parent_id:
            return  # 顶层节点，无法再向上

        parent = self.store.load(node.parent_id)
        if not parent:
            return

        # 收集所有子节点的摘要
        children = self.nav.get_children(parent.node_id)
        child_summaries = [c.summary for c in children if c.summary]

        if not child_summaries:
            return

        # 生成新的父摘要
        new_summary = self._compress(child_summaries, parent.summary)

        # 如果变化超过阈值，保存并递归上传
        if self._changed(parent.summary, new_summary, min_change_ratio):
            parent.summary = new_summary
            parent.version += 1
            self.store.save(parent)
            # 递归向上
            self.propagate(parent.node_id, min_change_ratio)

    # ======== 向下传播（摘要→内容，不常用） ========

    def propagate_down(self, node_id: str):
        """
        当顶层摘要发生重大变化时，向下标记子节点需要更新。
        这不直接修改子节点，而是添加一个"需同步"的标记。
        """
        node = self.store.load(node_id)
        if not node:
            return

        children = self.nav.get_children(node_id)
        for child in children:
            if not child.tags:
                child.tags = []
            if "needs_sync" not in child.tags:
                child.tags.append("needs_sync")
                self.store.save(child)
            # 递归向下
            self.propagate_down(child.node_id)

    # ======== 构建顶层摘要 ========

    def build_top_level_summary(self) -> Optional[str]:
        """
        生成宇宙层（level=1）摘要。
        从星系层收集所有节点摘要，压缩成全局状态描述。
        """
        galaxies = self.store.list_by_level(2)  # 星系层
        if not galaxies:
            return None

        meta_nodes = self.store.list_by_level(1)  # 宇宙层
        root = meta_nodes[0] if meta_nodes else None

        # 收集星系摘要
        galaxy_summaries = [g.summary or g.title for g in galaxies]
        new_summary = self._compress(galaxy_summaries, root.summary if root else "")

        if root:
            root.summary = new_summary
            self.store.save(root)
        else:
            # 创建根节点
            from .node import MemoryNode
            root = MemoryNode(
                node_id="meta_root",
                level=1,
                title="Xenon 记忆网络根节点",
                summary=new_summary,
                content="整个分层因果记忆网络的顶层摘要。每个星系节点的摘要在此汇聚。",
                children_ids=[g.node_id for g in galaxies],
                tags=["root", "meta"],
            )
            self.store.save(root)

        return new_summary

    # ======== 内部压缩算法 ========

    def _compress(self, child_summaries: List[str], old_summary: str) -> str:
        """
        内置摘要压缩，不依赖任何外部工具或 LLM。

        策略：
        1. 去重保序
        2. 拼接
        3. 截断到原长度的一定比例
        """
        if not child_summaries:
            return old_summary

        # 去重保序
        unique = list(dict.fromkeys(child_summaries))

        # 拼接
        combined = " | ".join(unique)

        # 截断：取原摘要长度的 120% 或最小 200 字符
        max_len = max(int(len(old_summary) * 1.2), 200) if old_summary else 500
        if len(combined) > max_len:
            combined = combined[:max_len] + "..."

        return combined

    def _changed(self, old: str, new: str, ratio: float) -> bool:
        """
        判断新旧摘要是否有显著变化。
        简单的逐字符差异比例 + 长度差异。
        """
        if not old:
            return True
        if not new:
            return False

        # 逐字符比较
        diff = sum(1 for a, b in zip(old, new) if a != b)
        # 长度差异
        diff += abs(len(old) - len(new))

        return (diff / max(len(new), 1)) > ratio

    # ======== 向量传播（场景 C 核心） ========

    def propagate_vectors(
        self,
        node_id: str,
        api,  # MemoryAPI 实例（用于跨分支重检）
        min_change_threshold: float = 0.05,
    ):
        """
        从当前节点向上传播语义向量更新。

        流程：
        1. 从 node_id 找到父节点
        2. 收集父节点的所有子节点（必须有向量）
        3. 父向量 = 子向量均值（归一化）
        4. 如果父向量变化超过阈值 → 保存，递归向上
        5. 同时触发跨分支关联重检（同层节点之间）
        """
        node = self.store.load(node_id)
        if not node:
            return

        if not node.parent_id:
            return  # 顶层，无父节点

        parent = self.store.load(node.parent_id)
        if not parent:
            return

        # 需要 embedder 才能操作向量
        embedder = getattr(api, '_embedder', None)
        if embedder is None:
            return

        # 收集所有子节点的向量
        children = self.nav.get_children(parent.node_id)
        child_vecs = []
        for child in children:
            if child.has_embedding:
                child_vecs.append(child.get_embedding_vec())
            else:
                # 尝试现场计算
                try:
                    c_text = f"{child.title} {child.summary}"
                    c_vec = embedder.encode(c_text)
                    child.set_embedding_vec(c_vec)
                    self.store.save(child)
                    child_vecs.append(c_vec)
                except Exception:
                    pass

        if not child_vecs:
            return  # 所有子节点都没有向量，无法聚合

        # 计算父向量 = 子向量均值
        new_vec = embedder.mean_vector(child_vecs)
        old_vec = parent.get_embedding_vec()

        # 判断向量是否有显著变化
        changed = True
        if old_vec is not None:
            sim = float(np.dot(old_vec, new_vec))
            # 相似度 > 0.95 说明几乎没变，跳过
            if sim > (1.0 - min_change_threshold):
                changed = False

        if changed:
            parent.set_embedding_vec(new_vec)
            parent.version += 1
            self.store.save(parent)

            # ── 场景 C 核心：跨分支关联重检 ──
            try:
                new_links = api.cross_branch_link_check(parent.node_id)
                if new_links > 0:
                    print(f"[VectorPropagate] 跨分支发现 {new_links} 条新关联 (节点: {parent.node_id})")
            except Exception as e:
                print(f"[VectorPropagate] 跨分支重检失败: {e}")

            # 递归向上传播
            self.propagate_vectors(parent.node_id, api, min_change_threshold)
