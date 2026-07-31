"""
search.py — 内置关键词搜索

零依赖搜索，不依赖 vecdb_handler。
纯关键词 + 正则匹配，在 vecdb 不可用时兜底。
"""

import re
from typing import Optional, List

from .node import MemoryNode
from .store import MemoryStore
from .schema import DEFAULT_SEARCH_LIMIT


class MemorySearch:
    """内置搜索，不依赖 vecdb，纯关键词 + 正则"""

    def __init__(self, store: MemoryStore):
        self.store = store

    # ======== 关键词搜索 ========

    def search(self, query: str, level: Optional[int] = None,
               limit: int = DEFAULT_SEARCH_LIMIT) -> List[MemoryNode]:
        """
        关键词匹配搜索，跨所有层级。

        Args:
            query: 搜索查询字符串
            level: 限定搜索层级（None = 全层级）
            limit: 返回结果数量上限

        Returns:
            按相关度排序的节点列表
        """
        # 提取有效关键词（忽略单字和纯数字）
        keywords = [kw for kw in re.findall(r'[\w\u4e00-\u9fff]+', query.lower())
                    if len(kw) > 1 or kw.isascii()]

        if not keywords:
            return []

        results = []
        levels_to_search = [level] if level else range(1, 8)

        for lvl in levels_to_search:
            for node in self.store.list_by_level(lvl):
                score = self._score(node, keywords)
                if score > 0:
                    results.append((score, node))

        # 按分数降序排列
        results.sort(key=lambda x: -x[0])
        return [node for _, node in results[:limit]]

    def _score(self, node: MemoryNode, keywords: List[str]) -> float:
        """计算节点与关键词的匹配分数"""
        # 构建搜索文本，不同区域权重不同
        text_full = (
            f"{node.title} {node.summary} {node.content} "
            f"{' '.join(node.tags)}"
        ).lower()

        # 精确匹配分数更高
        exact_bonus = 0
        for kw in keywords:
            if kw in node.title.lower():
                exact_bonus += 3
            if kw in node.summary.lower():
                exact_bonus += 2
            if kw in node.tags:
                exact_bonus += 2

        # 全文匹配分数
        score = sum(1 for kw in keywords if kw in text_full)
        return score + exact_bonus

    # ======== 标签搜索 ========

    def search_by_tag(self, tag: str, level: Optional[int] = None) -> List[MemoryNode]:
        """按标签精确搜索"""
        results = []
        levels_to_search = [level] if level else range(1, 8)

        for lvl in levels_to_search:
            for node in self.store.list_by_level(lvl):
                if tag in node.tags:
                    results.append(node)

        return results

    def search_by_tags(self, tags: List[str], match_all: bool = False) -> List[MemoryNode]:
        """
        按多标签搜索。

        Args:
            tags: 标签列表
            match_all: True = 必须全部匹配，False = 匹配任意一个
        """
        results = []
        for node in self.store.list_all():
            if match_all:
                if all(t in node.tags for t in tags):
                    results.append(node)
            else:
                if any(t in node.tags for t in tags):
                    results.append(node)
        return results

    # ======== 正则搜索 ========

    def regex_search(self, pattern: str, level: Optional[int] = None,
                     limit: int = DEFAULT_SEARCH_LIMIT) -> List[MemoryNode]:
        """
        正则表达式搜索。

        Args:
            pattern: 正则表达式
            level: 限定层级
            limit: 结果上限
        """
        try:
            regex = re.compile(pattern, re.IGNORECASE)
        except re.error:
            return []

        results = []
        levels_to_search = [level] if level else range(1, 8)

        for lvl in levels_to_search:
            for node in self.store.list_by_level(lvl):
                text = f"{node.title} {node.summary} {node.content}"
                matches = len(regex.findall(text))
                if matches > 0:
                    results.append((matches, node))

        results.sort(key=lambda x: -x[0])
        return [node for _, node in results[:limit]]

    # ======== 模糊/相关性搜索 ========

    def fuzzy_search(self, query: str, threshold: float = 0.6,
                     limit: int = DEFAULT_SEARCH_LIMIT) -> List[MemoryNode]:
        """
        模糊搜索：基于子串匹配和编辑距离的宽松搜索。

        threshold: 0.0 ~ 1.0，越高越严格
        """
        query_lower = query.lower()
        results = []

        for node in self.store.list_all():
            text = f"{node.title} {node.summary}".lower()

            # 简单相似度：最长公共子串 / 查询长度
            lcs_len = self._longest_common_substring(query_lower, text)
            similarity = lcs_len / max(len(query_lower), 1)
            similarity = min(similarity, 1.0)

            if similarity >= threshold:
                results.append((similarity, node))

        results.sort(key=lambda x: -x[0])
        return [node for _, node in results[:limit]]

    @staticmethod
    def _longest_common_substring(s1: str, s2: str) -> int:
        """最长公共子串长度"""
        if not s1 or not s2:
            return 0

        m, n = len(s1), len(s2)
        dp = [[0] * (n + 1) for _ in range(m + 1)]
        max_len = 0

        for i in range(1, m + 1):
            for j in range(1, n + 1):
                if s1[i - 1] == s2[j - 1]:
                    dp[i][j] = dp[i - 1][j - 1] + 1
                    max_len = max(max_len, dp[i][j])

        return max_len
