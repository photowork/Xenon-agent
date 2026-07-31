"""
nav.py — 层级导航

在分层记忆网络中上下遍历。
支持：父→子、子→父、路径到根、子树展开。
"""

from typing import Optional, List

from .node import MemoryNode
from .store import MemoryStore


class MemoryNavigator:
    """记忆网络层级导航器"""

    def __init__(self, store: MemoryStore):
        self.store = store

    # ======== 基本导航 ========

    def get_children(self, node_id: str) -> List[MemoryNode]:
        """
        获取某节点的所有直接子节点。
        通过扫描文件系统中所有 parent_id == node_id 的节点实现。
        """
        children = []
        # 只查当前节点 level 的下一层
        parent_node = self.store.load(node_id)
        if not parent_node:
            return []

        # 扫描下一层级
        for level in range(parent_node.level + 1, 8):
            for candidate in self.store.list_by_level(level):
                if candidate.parent_id == node_id:
                    children.append(candidate)

        return children

    def get_parent(self, node_id: str) -> Optional[MemoryNode]:
        """获取某节点的父节点"""
        node = self.store.load(node_id)
        if not node or not node.parent_id:
            return None
        return self.store.load(node.parent_id)

    def get_siblings(self, node_id: str) -> List[MemoryNode]:
        """获取某节点的兄弟节点（同父同层）"""
        node = self.store.load(node_id)
        if not node or not node.parent_id:
            return []
        return [
            c for c in self.get_children(node.parent_id)
            if c.node_id != node_id
        ]

    # ======== 路径导航 ========

    def get_path_to_root(self, node_id: str) -> List[MemoryNode]:
        """从当前节点到根节点的完整路径 [底层 → ... → 顶层]"""
        path = []
        current_id = node_id
        visited = set()

        while current_id:
            if current_id in visited:
                break  # 防止环形引用
            visited.add(current_id)

            node = self.store.load(current_id)
            if not node:
                break
            path.append(node)
            current_id = node.parent_id

        return path

    def get_path_from_root(self, node_id: str) -> List[MemoryNode]:
        """从根节点到当前节点的路径 [顶层 → ... → 底层]"""
        return list(reversed(self.get_path_to_root(node_id)))

    def get_root(self, node_id: str) -> Optional[MemoryNode]:
        """找到节点所属的根节点"""
        path = self.get_path_to_root(node_id)
        return path[-1] if path else None

    # ======== 子树展开 ========

    def get_subtree(self, node_id: str, max_depth: int = 2) -> List[MemoryNode]:
        """
        获取以 node_id 为根的子树（广度优先，限制深度）。
        返回包含根节点在内的所有节点列表。
        """
        result = []
        seen = set()
        queue = [(node_id, 0)]

        while queue:
            current_id, depth = queue.pop(0)

            if current_id in seen:
                continue
            if depth > max_depth:
                continue

            node = self.store.load(current_id)
            if not node:
                continue

            seen.add(current_id)
            result.append(node)

            if depth < max_depth:
                for child in self.get_children(current_id):
                    if child.node_id not in seen:
                        queue.append((child.node_id, depth + 1))

        return result

    # ======== 构建/维护 ========

    def build_initial_hierarchy(self):
        """
        首次构建层级关系。
        扫描所有现有节点，按以下规则自动建立父子关系：
        - 同一主题关键词的节点 → 尝试按时间/抽象度归入层级
        - 缺少父节点的节点 → 尝试匹配最相关的上层节点

        这是一个启发式方法，后续可由人（Xenon）手动调整。
        """
        self.repair_orphans(dry_run=False, verbose=True)

    def repair_orphans(self, dry_run: bool = False, verbose: bool = True) -> dict:
        """
        修复所有孤儿节点（无 parent_id 的节点）。
        
        对每个无父节点的节点，向上逐层搜索（level-1, level-2...直到 level 1），
        使用改进的 _find_best_parent 匹配。
        
        Args:
            dry_run: True 时只报告不修改
            verbose: True 时打印详细信息
        
        Returns:
            {"fixed": 修复数, "skipped": 跳过数, "total_orphans": 孤儿总数}
        """
        fixed = 0
        skipped = 0
        total_orphans = 0
        
        all_nodes = {n.node_id: n for n in self.store.list_all()}
        
        for node in all_nodes.values():
            if node.parent_id and node.parent_id in all_nodes:
                continue  # 已有有效父节点
            
            if node.level <= 1:
                continue  # 宇宙层不需要父节点
            
            total_orphans += 1
            
            # 向上逐层搜索（不仅是 level-1）
            best_parent = None
            best_score = 0.0
            
            for parent_level in range(node.level - 1, 0, -1):
                candidates = self.store.list_by_level(parent_level)
                if not candidates:
                    continue
                
                parent = self._find_best_parent(node, candidates)
                if parent:
                    # 重新计算 score 用于多层级比较
                    node_text = _build_match_text(node)
                    node_tokens = _tokenize_for_match(node_text)
                    cand_text = _build_match_text(parent)
                    cand_tokens = _tokenize_for_match(cand_text)
                    if node_tokens and cand_tokens:
                        intersection = len(node_tokens & cand_tokens)
                        union = len(node_tokens | cand_tokens)
                        score = intersection / union if union > 0 else 0.0
                        if score > best_score:
                            best_score = score
                            best_parent = parent
            
            if best_parent:
                if not dry_run:
                    node.parent_id = best_parent.node_id
                    best_parent.add_child(node.node_id)
                    self.store.save(node)
                    self.store.save(best_parent)
                if verbose:
                    print(f"  🔗 {node.node_id} → {best_parent.node_id} "
                          f"(L{node.level}→L{best_parent.level}, score={best_score:.3f})")
                fixed += 1
            else:
                if verbose:
                    print(f"  ⚠️  {node.node_id} (L{node.level}): 无匹配父节点")
                skipped += 1
        
        result = {
            "fixed": fixed,
            "skipped": skipped,
            "total_orphans": total_orphans,
        }
        if verbose:
            print(f"\n📊 孤儿修复完成: {fixed} 修复, {skipped} 跳过, 共 {total_orphans} 孤儿")
        return result

    def _find_best_parent(self, node: MemoryNode,
                          candidates: List[MemoryNode]) -> Optional[MemoryNode]:
        """启发式匹配最佳父节点。
        
        改进版（v2）：中文 2-gram 切分 + 摘要参与 + Jaccard 相似度。
        """
        node_text = _build_match_text(node)
        node_tokens = _tokenize_for_match(node_text)

        if not node_tokens:
            return None

        best_score = 0.0
        best_parent = None

        for candidate in candidates:
            cand_text = _build_match_text(candidate)
            cand_tokens = _tokenize_for_match(cand_text)

            if not cand_tokens:
                continue

            # Jaccard 相似度：交/并
            intersection = len(node_tokens & cand_tokens)
            union = len(node_tokens | cand_tokens)
            score = intersection / union if union > 0 else 0.0

            if score > best_score:
                best_score = score
                best_parent = candidate

        # 极低阈值：至少需要有一个 2-gram 重合
        return best_parent if best_score > 0.005 else None


# ═══════════════════════════════════════════════════════════════
# 辅助函数：文本分词（模块级，供 _find_best_parent 和外部使用）
# ═══════════════════════════════════════════════════════════════

def _build_match_text(node: MemoryNode) -> str:
    """构建用于匹配的文本：标题 + 摘要 + 标签 + 内容前 300 字"""
    parts = [
        node.title or "",
        (node.summary or "")[:200],
        " ".join(node.tags or []),
        (node.content or "")[:300],
    ]
    return " ".join(parts)


def _tokenize_for_match(text: str) -> set:
    """将文本切分为语义 token 集合。
    
    策略：
    - 英文/数字：按单词边界切分（保留完整词）
    - 中文：2-gram 字符切分 + 单字（覆盖短词）
    - 所有 token 转小写
    """
    import re
    tokens = set()

    # 1. 英文/数字词
    for word in re.findall(r'[a-zA-Z0-9_]{2,}', text):
        tokens.add(word.lower())

    # 2. 中文 2-gram
    cjk_pattern = re.compile(r'[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff]{2,}')
    for segment in cjk_pattern.findall(text):
        for i in range(len(segment) - 1):
            tokens.add(segment[i:i + 2])
        # 单字也加入（覆盖短词如"引擎"、"认知"等）
        for ch in segment:
            tokens.add(ch)

    # 3. 中文标签（tags 中可能是完整中文词，直接加入）
    for tag in re.findall(r'[\u4e00-\u9fff]+', text):
        if len(tag) >= 2:
            tokens.add(tag)

    return tokens
