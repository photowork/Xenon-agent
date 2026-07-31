"""
api.py — MemoryAPI Facade（接口层）

统一记忆接口，屏蔽底层工具变化。
- 工具在 → 用工具加速/增强
- 工具不在 → 用 Core 自身能力兜底

对外提供与现有 memory_query_handler 工具兼容的接口（Dict[str, Any]），
内部委托给分层记忆网络 Core。

设计原则：
  1. 数据在 .memory/ 文件中，接口层替换不影响数据
  2. 写入自动触发摘要传播
  3. 搜索 vecdb 优先，Core 兜底
  4. 因果查询 KG 优先，links 字段兜底
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np

from .node import MemoryNode, CausalLink
from .store import MemoryStore
from .nav import MemoryNavigator
from .search import MemorySearch
from .propagate import MemoryPropagator
from .aging import MemoryAging
from .schema import (
    LEVELS,
    LEVEL_DIRS,
    STORAGE_ROOT,
    CausalLinkType,
    SourceType,
    DEFAULT_SEARCH_LIMIT,
    DEFAULT_CONTEXT_DEPTH,
    DEFAULT_MAX_AGE_DAYS,
    DEFAULT_MIN_ACCESS_COUNT,
)


# ── node_id 前缀映射 ──────────────────────────────────────────
LEVEL_PREFIX = {
    1: "meta",
    2: "galaxy",
    3: "star",
    4: "planet",
    5: "street",
    6: "item",
    7: "molecule",
}


def _make_node_id(level: int, title: str = "", ts: float = None) -> str:
    """生成层级化 node_id，如 'galaxy_20260725_a1b2c3d4'"""
    ts = ts or time.time()
    date_str = datetime.fromtimestamp(ts).strftime("%Y%m%d")
    prefix = LEVEL_PREFIX.get(level, "node")
    # 用标题+时间戳的哈希作为短标识
    raw = f"{title}_{ts}"
    short_hash = hashlib.md5(raw.encode()).hexdigest()[:8]
    return f"{prefix}_{date_str}_{short_hash}"


# ── 关键词 → 层级推测 ──────────────────────────────────────────
# 用于自动判断新写入应该放在哪个层级
TOPIC_LEVEL_HINTS: Dict[str, int] = {
    # 宇宙层 (1) — 全局原则（极其稀缺，只有真正的顶层抽象才放这里）
    "最高原则": 1, "根本使命": 1, "存在意义": 1,
    # 星系层 (2) — 大领域（必须是领域名，不能是通用词）
    "自主性": 2, "工具链": 2, "记忆系统": 2,
    "元认知": 2, "自我模型": 2, "因果网络": 2,
    "知识图谱": 2, "向量数据库": 2, "上下文网络": 2,
    "跨域映射": 2, "灵魂引擎": 2,
    # 恒星层 (3) — 核心概念/方法论
    "递归": 3, "吸引子": 3, "认知突破": 3, "洞见": 3,
    "身份锚点": 3, "关系锚点": 3, "边界状态": 3,
    "定时任务": 3, "自主探索": 3, "摘要传播": 3,
    "层级导航": 3, "按需展开": 3, "有损压缩": 3,
    # 行星层 (4) — 事件簇/子项目
    "升级": 4, "重构": 4, "发布": 4, "迁移": 4,
    "测试": 4, "实验": 4, "调试": 4, "修复": 4,
    "审查": 4, "审查报告": 4, "交叉审查": 4,
    # 街道层 (5) — 单次会话/具体事件（默认）
    "对话": 5, "会话": 5, "讨论": 5, "提问": 5,
    "回答": 5, "指令": 5,
}

DEFAULT_NEW_NODE_LEVEL = 5


def _infer_level(title: str, summary: str = "", content: str = "") -> int:
    """根据内容推测节点应该处于哪个层级。
    
    规则：
    1. 匹配 TOPIC_LEVEL_HINTS 关键词，统计各层级命中次数
    2. 得分最高的层级当选
    3. 得分相同时，选更深（数字更大）的层级 —— 防止宽泛词压倒具体词
    4. 无匹配时默认街道层(5)
    """
    text = f"{title} {summary} {content}".lower()
    scores = {}
    for topic, level in TOPIC_LEVEL_HINTS.items():
        if topic in text:
            scores[level] = scores.get(level, 0) + 1
    if scores:
        # 同分选更深层级: 按 (得分, 层级数字) 排序，数字大的优先
        return max(scores, key=lambda k: (scores[k], k))
    return DEFAULT_NEW_NODE_LEVEL


# =================================================================
# MemoryAPI
# =================================================================

class MemoryAPI:
    """
    统一记忆接口 — 分层因果记忆网络的唯一入口。

    用法:
        api = MemoryAPI()
        api.write("这是一条记忆", summary="测试", tags=["test"])
        results = api.search("测试")
        context = api.get_context("planet_20260725_a1b2c3d4")
    """

    def __init__(self, storage_root: Path = None, embedding_service=None):
        # ── Core 模块（永远可用） ──
        self.store = MemoryStore(storage_root or STORAGE_ROOT)
        self.nav = MemoryNavigator(self.store)
        self.search_engine = MemorySearch(self.store)
        self.propagator = MemoryPropagator(self.store, self.nav)
        self.aging_engine = MemoryAging(self.store, self.nav, self.propagator)

        # ── 可选增强（初始为 None） ──
        self._vecdb = None
        self._kg = None
        self._embedder = embedding_service  # EmbeddingService 实例

    # ================================================================
    # 可选增强挂载
    # ================================================================

    def map_vecdb(self, vecdb_instance):
        """挂载 vecdb 增强（可选）。挂载后搜索自动使用语义搜索增强。"""
        self._vecdb = vecdb_instance

    def map_kg(self, kg_instance):
        """挂载知识图谱增强（可选）。挂载后因果查询使用图遍历增强。"""
        self._kg = kg_instance

    @property
    def has_vecdb(self) -> bool:
        return self._vecdb is not None

    @property
    def has_kg(self) -> bool:
        return self._kg is not None

    @property
    def has_embedder(self) -> bool:
        return self._embedder is not None

    # ================================================================
    # 写入
    # ================================================================

    def write(
        self,
        content: str,
        summary: str = None,
        tags: List[str] = None,
        level: int = None,
        parent_id: str = None,
        source_type: str = None,
        title: str = None,
        auto_link: bool = True,
    ) -> Dict[str, Any]:
        """
        写入一条记忆到分层网络。

        Args:
            content: 完整内容
            summary: 摘要（不提供则自动截取 content 前 200 字）
            tags: 标签列表
            level: 目标层级（不提供则自动推测）
            parent_id: 父节点 ID（不提供则自动匹配或留空）
            source_type: 来源类型（tool_verified / user_stated / inferred / auto_generated）
            title: 节点标题（不提供则用 summary 或截取 content）
            auto_link: 是否自动根据语义关联已有节点（默认开启）

        Returns:
            {"success": True/False, "node_id": "...", "level": N, ...}
        """
        try:
            tags = tags or []
            source_type = source_type or SourceType.USER_STATED.value
            summary = summary or (content[:200] + "..." if len(content) > 200 else content)
            title = title or summary[:80]

            # 推测层级
            if level is None:
                level = _infer_level(title, summary, content)

            # 生成 node_id
            node_id = _make_node_id(level, title)

            # 自动匹配父节点（如果没指定但有同层上级候选）
            if parent_id is None and level > 1:
                parent_id = self._auto_match_parent(title, summary, tags, level)

            # 构建节点
            node = MemoryNode(
                node_id=node_id,
                level=level,
                title=title,
                summary=summary,
                content=content,
                parent_id=parent_id,
                source_type=source_type,
                tags=tags,
            )

            # 持久化
            self.store.save(node)

            # 计算语义向量（如果 embedder 可用）
            if self._embedder:
                try:
                    embed_text = f"{title} {summary}"
                    vec = self._embedder.encode(embed_text)
                    node.set_embedding_vec(vec)
                    self.store.save(node)  # 持久化向量
                except Exception as e:
                    print(f"[MemoryAPI] embedding 计算失败: {e}")

            # 更新父节点的 children_ids
            if parent_id:
                parent = self.store.load(parent_id)
                if parent:
                    parent.add_child(node_id)
                    self.store.save(parent)

            # 自动语义关联（横向边）——让新记忆像在线学习一样融入已有网络
            link_count = 0
            if auto_link:
                try:
                    link_count = self._auto_link_new_node(node)
                except Exception as e:
                    print(f"[MemoryAPI] auto_link 失败: {e}")
                    import traceback
                    traceback.print_exc()
                    # 关联失败不影响写入主流程

            # 触发摘要向上传播（文本摘要）
            self.propagator.propagate(node_id)

            # 触发向量向上传播（如果 embedder 可用）
            if self._embedder and node.has_embedding:
                try:
                    self.propagator.propagate_vectors(node_id, self)
                except Exception as e:
                    print(f"[MemoryAPI] vector_propagate 失败: {e}")

            return {
                "success": True,
                "node_id": node_id,
                "level": level,
                "level_name": LEVELS.get(level, f"层级{level}"),
                "parent_id": parent_id,
                "title": title,
                "auto_links_created": link_count,
                "message": f"记忆已写入: {node_id} (层级 {level} — {LEVELS.get(level, '')})",
            }

        except Exception as e:
            return {"success": False, "error": f"写入记忆失败: {str(e)}"}

    def write_execution(
        self,
        goal: str,
        phase: str,
        tool_name: str,
        success: bool,
        blockage_reason: str = None,
        lesson: str = "",
        summary: str = "",
        next_actions: List[str] = None,
    ) -> Dict[str, Any]:
        """
        写入一条执行记录（物品层/分子层）。
        对应原有的 write_execution_memory。
        """
        tags = ["execution_log", f"tool:{tool_name}", f"phase:{phase}",
                "success" if success else "failure"]
        if blockage_reason:
            tags.append("blocked")

        content_lines = [
            f"目标: {goal}",
            f"阶段: {phase}",
            f"工具: {tool_name}",
            f"成功: {success}",
        ]
        if blockage_reason:
            content_lines.append(f"阻塞原因: {blockage_reason}")
        if lesson:
            content_lines.append(f"教训: {lesson}")
        if next_actions:
            content_lines.append(f"后续行动: {', '.join(next_actions)}")

        exec_summary = summary or f"[{'成功' if success else '失败'}] {tool_name}: {goal[:60]}"

        return self.write(
            content="\n".join(content_lines),
            summary=exec_summary,
            tags=tags,
            level=6,  # 物品层
            source_type=SourceType.AUTO_GENERATED.value,
            title=exec_summary[:80],
        )

    def append_to_existing(self, node_id: str, content: str) -> Dict[str, Any]:
        """追加内容到已有节点（对应 append_to_memory）"""
        try:
            node = self.store.load(node_id)
            if not node:
                return {"success": False, "error": f"节点不存在: {node_id}"}

            node.content += f"\n{content}"
            node.version += 1
            node.touch()
            self.store.save(node)

            return {
                "success": True,
                "node_id": node_id,
                "message": f"已追加到 {node_id}",
            }
        except Exception as e:
            return {"success": False, "error": f"追加失败: {str(e)}"}

    # ================================================================
    # 读取
    # ================================================================

    def read(self, node_id: str) -> Dict[str, Any]:
        """读取一条记忆（对应 read_memory）"""
        try:
            node = self.store.load(node_id)
            if not node:
                return {"success": False, "error": f"节点不存在: {node_id}"}

            node.touch()
            self.store.save(node)  # 更新访问时间

            return {
                "success": True,
                "node_id": node.node_id,
                "level": node.level,
                "level_name": LEVELS.get(node.level, ""),
                "title": node.title,
                "summary": node.summary,
                "content": node.content,
                "parent_id": node.parent_id,
                "children_ids": node.children_ids,
                "tags": node.tags,
                "source_type": node.source_type,
                "created_at": node.created_at,
                "last_accessed": node.last_accessed,
                "access_count": node.access_count,
                "links": [{"target_id": l.target_id, "type": l.link_type,
                           "weight": l.weight, "description": l.description}
                          for l in node.links],
            }

        except Exception as e:
            return {"success": False, "error": f"读取记忆失败: {str(e)}"}

    def get_latest(self) -> Dict[str, Any]:
        """获取最新写入的节点（对应 get_latest_memory）"""
        try:
            all_nodes = self.store.list_all()
            if not all_nodes:
                return {"success": False, "error": "记忆库为空"}

            latest = max(all_nodes, key=lambda n: n.created_at)
            return self.read(latest.node_id)
        except Exception as e:
            return {"success": False, "error": str(e)}

    def get_by_date(self, date_str: str) -> Dict[str, Any]:
        """按日期查找记忆（对应 get_memory_by_date）"""
        try:
            all_nodes = self.store.list_all()
            matches = [
                n for n in all_nodes
                if n.created_at.startswith(date_str)
            ]
            matches.sort(key=lambda n: n.created_at)

            return {
                "success": True,
                "count": len(matches),
                "results": [
                    {
                        "node_id": n.node_id,
                        "title": n.title,
                        "summary": n.summary,
                        "level": n.level,
                        "created_at": n.created_at,
                        "tags": n.tags,
                    }
                    for n in matches
                ],
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ================================================================
    # 搜索
    # ================================================================

    def search(
        self,
        keyword: str,
        limit: int = DEFAULT_SEARCH_LIMIT,
        case_sensitive: bool = False,
        tags: List[str] = None,
        level: int = None,
        mode: str = "auto",
    ) -> Dict[str, Any]:
        """
        搜索记忆（对应 search_memories）。

        Args:
            mode: 'auto' — 自动选择（embedder 优先 → vecdb → 关键词兜底）
                  'semantic' — 强制语义搜索（需要 embedder，否则降级）
                  'keyword' — 强制关键词搜索
        """
        try:
            # ── 语义搜索路径（embedder 优先） ──
            if mode in ("auto", "semantic") and self._embedder:
                try:
                    semantic_results = self._semantic_search(keyword, limit, level)
                    if semantic_results:
                        return {
                            "success": True,
                            "count": len(semantic_results),
                            "source": "embedder",
                            "mode": "semantic",
                            "results": semantic_results,
                        }
                except Exception:
                    if mode == "semantic":
                        raise  # 强制语义模式失败就报错
                    pass  # auto 模式降级

            # ── vecdb 语义搜索（次优先） ──
            if mode != "keyword" and self._vecdb:
                try:
                    vecdb_results = self._vecdb_query(keyword, limit)
                    if vecdb_results:
                        return {
                            "success": True,
                            "count": len(vecdb_results),
                            "source": "vecdb",
                            "results": vecdb_results,
                        }
                except Exception:
                    pass  # 降级到 Core 搜索

            # ── Core 关键词搜索（兜底） ──
            core_results = self.search_engine.search(
                query=keyword,
                level=level,
                limit=limit,
            )

            # 标签过滤
            if tags and core_results:
                core_results = [
                    n for n in core_results
                    if any(t in n.tags for t in tags)
                ]

            # case_sensitive 过滤
            if case_sensitive and core_results:
                core_results = [
                    n for n in core_results
                    if keyword in n.title or keyword in n.content
                ]

            return {
                "success": True,
                "count": len(core_results),
                "source": "core",
                "mode": "keyword",
                "results": [
                    {
                        "node_id": n.node_id,
                        "title": n.title,
                        "summary": n.summary,
                        "level": n.level,
                        "level_name": LEVELS.get(n.level, ""),
                        "tags": n.tags,
                        "created_at": n.created_at,
                        "score": getattr(n, "_search_score", 0),
                    }
                    for n in core_results
                ],
            }

        except Exception as e:
            return {"success": False, "error": f"搜索失败: {str(e)}"}

    def _semantic_search(
        self,
        query: str,
        limit: int = DEFAULT_SEARCH_LIMIT,
        level: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """
        使用 embedder（bge-small-zh-v1.5）做语义搜索。
        计算查询向量 → 与所有有向量的节点算余弦相似度 → 返回 top-k。
        """
        if not self._embedder:
            return []

        # 编码查询
        query_vec = self._embedder.encode(query)
        if query_vec is None:
            return []

        # 收集候选节点
        if level:
            candidates = self.store.list_by_level(level)
        else:
            candidates = self.store.list_all()

        scored = []
        for node in candidates:
            if not node.has_embedding:
                # 现场计算
                try:
                    c_text = f"{node.title} {node.summary}"
                    c_vec = self._embedder.encode(c_text)
                    node.set_embedding_vec(c_vec)
                    self.store.save(node)
                except Exception:
                    continue

            node_vec = node.get_embedding_vec()
            if node_vec is None:
                continue

            sim = float(np.dot(query_vec, node_vec))
            if sim < 0.3:  # 低阈值过滤噪声
                continue

            scored.append((sim, node))

        # 按相似度降序
        scored.sort(key=lambda x: -x[0])
        results = []
        for sim, node in scored[:limit]:
            results.append({
                "node_id": node.node_id,
                "title": node.title,
                "summary": node.summary,
                "level": node.level,
                "level_name": LEVELS.get(node.level, ""),
                "tags": node.tags,
                "created_at": node.created_at,
                "score": round(sim, 4),
            })

        return results

    def search_by_tags(
        self,
        tags: List[str],
        match_all: bool = False,
        limit: int = 20,
        sort_by: str = "newest",
    ) -> Dict[str, Any]:
        """按标签搜索（对应 get_memory_with_tags）"""
        try:
            results = self.search_engine.search_by_tags(tags, match_all=match_all)

            if sort_by == "newest":
                results.sort(key=lambda n: n.created_at, reverse=True)
            elif sort_by == "oldest":
                results.sort(key=lambda n: n.created_at)

            results = results[:limit]

            return {
                "success": True,
                "count": len(results),
                "results": [
                    {
                        "node_id": n.node_id,
                        "title": n.title,
                        "summary": n.summary,
                        "tags": n.tags,
                        "created_at": n.created_at,
                    }
                    for n in results
                ],
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    def get_tag_stats(self) -> Dict[str, Any]:
        """获取标签统计（对应 get_tag_stats）"""
        try:
            all_nodes = self.store.list_all()
            tag_counts: Dict[str, int] = {}
            for node in all_nodes:
                for tag in node.tags:
                    tag_counts[tag] = tag_counts.get(tag, 0) + 1

            sorted_tags = sorted(tag_counts.items(), key=lambda x: -x[1])

            return {
                "success": True,
                "total_unique_tags": len(sorted_tags),
                "tags": [{"tag": t, "count": c} for t, c in sorted_tags],
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ================================================================
    # 列表
    # ================================================================

    def list_nodes(
        self,
        limit: int = 20,
        sort_by: str = "newest",
        level: int = None,
    ) -> Dict[str, Any]:
        """列出节点（对应 list_memories）"""
        try:
            if level:
                nodes = self.store.list_by_level(level)
            else:
                nodes = self.store.list_all()

            if sort_by == "newest":
                nodes.sort(key=lambda n: n.created_at, reverse=True)
            elif sort_by == "oldest":
                nodes.sort(key=lambda n: n.created_at)
            elif sort_by == "accessed":
                nodes.sort(key=lambda n: n.last_accessed, reverse=True)
            elif sort_by == "level":
                nodes.sort(key=lambda n: (n.level, n.created_at), reverse=True)

            nodes = nodes[:limit]

            return {
                "success": True,
                "count": len(nodes),
                "results": [
                    {
                        "node_id": n.node_id,
                        "title": n.title,
                        "summary": n.summary,
                        "level": n.level,
                        "level_name": LEVELS.get(n.level, ""),
                        "tags": n.tags,
                        "created_at": n.created_at,
                        "last_accessed": n.last_accessed,
                        "access_count": n.access_count,
                    }
                    for n in nodes
                ],
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    def list_cognitive_nodes(
        self,
        cognitive_type: str = None,
        state: str = "active",
        limit: int = 20,
    ) -> Dict[str, Any]:
        """
        列出认知类记忆（对应 list_cognitive_memories）。
        在 Core 中，认知类记忆通过 tags 中的 "cognitive:" 前缀识别。
        """
        try:
            all_nodes = self.store.list_all()

            # 筛选认知类标签
            def is_cognitive(node: MemoryNode) -> bool:
                cog_tags = [t for t in node.tags if t.startswith("cognitive:")]
                if not cog_tags:
                    return False
                if cognitive_type:
                    return any(cognitive_type in t for t in cog_tags)
                return True

            results = [n for n in all_nodes if is_cognitive(n)]
            results.sort(key=lambda n: n.created_at, reverse=True)
            results = results[:limit]

            return {
                "success": True,
                "count": len(results),
                "results": [
                    {
                        "node_id": n.node_id,
                        "title": n.title,
                        "summary": n.summary,
                        "tags": n.tags,
                        "created_at": n.created_at,
                    }
                    for n in results
                ],
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    def get_summary(self, limit: int = 5) -> Dict[str, Any]:
        """获取记忆摘要（对应 get_memory_summary）"""
        try:
            # 先取顶层节点（宇宙/星系/恒星）
            top_nodes = []
            for lvl in [1, 2, 3]:
                top_nodes.extend(self.store.list_by_level(lvl))

            if len(top_nodes) < limit:
                # 不够则补充行星层
                top_nodes.extend(self.store.list_by_level(4))

            top_nodes.sort(key=lambda n: n.created_at, reverse=True)
            top_nodes = top_nodes[:limit]

            return {
                "success": True,
                "count": len(top_nodes),
                "summary": [
                    {
                        "node_id": n.node_id,
                        "title": n.title,
                        "summary": n.summary,
                        "level": n.level,
                        "level_name": LEVELS.get(n.level, ""),
                    }
                    for n in top_nodes
                ],
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ================================================================
    # 上下文/按需展开（核心算法）
    # ================================================================

    def get_context(
        self,
        query: str = "",
        node_id: str = None,
        max_nodes: int = 20,
        depth: int = DEFAULT_CONTEXT_DEPTH,
    ) -> Dict[str, Any]:
        """
        按需展开获取上下文 —— 这是整个层级记忆网络的核心查询算法。

        1. 如果给了 node_id → 沿路径展开（顶层→该节点→子树）
        2. 如果给了 query → 搜索相关节点 → 沿路径展开
        3. 都未给 → 返回顶层摘要

        对应方案第九章的 load_relevant_memory。
        """
        try:
            context_nodes = []
            seen = set()

            if node_id:
                # ── 模式 1：从指定节点展开 ──
                node = self.store.load(node_id)
                if not node:
                    return {"success": False, "error": f"节点不存在: {node_id}"}

                # 2a. 获取从顶到当前节点的路径
                path = self.nav.get_path_to_root(node_id)  # [底层→顶层]
                for p_node in reversed(path):  # [顶层→底层]
                    if p_node.node_id not in seen:
                        context_nodes.append(p_node)
                        seen.add(p_node.node_id)

                # 2b. 展开子树
                subtree = self.nav.get_subtree(node_id, max_depth=depth)
                for child in subtree:
                    if child.node_id not in seen:
                        context_nodes.append(child)
                        seen.add(child.node_id)

                context_nodes = context_nodes[:max_nodes]

            elif query:
                # ── 模式 2：搜索 → 展开 ──
                search_results = self.search_engine.search(query=query, limit=10)

                if not search_results:
                    # 未找到：返回顶层摘要作为默认上下文
                    context_nodes = self.store.list_by_level(1)  # 宇宙层
                    if len(context_nodes) < 5:
                        context_nodes += self.store.list_by_level(2)  # 星系层
                else:
                    for node in search_results:
                        if len(context_nodes) >= max_nodes:
                            break
                        if node.node_id in seen:
                            continue

                        # 先加入搜索到的节点本身
                        context_nodes.append(node)
                        seen.add(node.node_id)

                        # 获取路径并展开（祖先节点）
                        path = self.nav.get_path_to_root(node.node_id)
                        for p_node in reversed(path):
                            if p_node.node_id not in seen:
                                context_nodes.append(p_node)
                                seen.add(p_node.node_id)

                        # 展开一层子节点
                        children = self.nav.get_children(node.node_id)
                        for child in children:
                            if child.node_id not in seen and len(context_nodes) < max_nodes:
                                context_nodes.append(child)
                                seen.add(child.node_id)

            else:
                # ── 模式 3：返回顶层 ──
                context_nodes = self.store.list_by_level(1)
                context_nodes += self.store.list_by_level(2)[:5]

            # 格式化返回
            return {
                "success": True,
                "count": len(context_nodes),
                "context": [
                    {
                        "node_id": n.node_id,
                        "level": n.level,
                        "level_name": LEVELS.get(n.level, ""),
                        "title": n.title,
                        "summary": n.summary,
                        "parent_id": n.parent_id,
                        "tags": n.tags,
                    }
                    for n in context_nodes
                ],
            }

        except Exception as e:
            return {"success": False, "error": f"获取上下文失败: {str(e)}"}

    def get_causal_chain(
        self,
        node_id: str,
        direction: str = "both",
        max_depth: int = 3,
    ) -> Dict[str, Any]:
        """
        获取因果链（KG 优先，Core links 兜底）。
        对应方案第六章的 get_causal_chain。
        """
        try:
            node = self.store.load(node_id)
            if not node:
                return {"success": False, "error": f"节点不存在: {node_id}"}

            # ── 尝试 KG 增强 ──
            if self._kg:
                try:
                    kg_results = self._kg_query_related(
                        node_id, rel_type="causes",
                        direction=direction, depth=max_depth,
                    )
                    if kg_results:
                        return {
                            "success": True,
                            "source": "kg",
                            "chain": kg_results,
                        }
                except Exception:
                    pass

            # ── Core links 降级 ──
            chain = [{
                "node_id": node.node_id,
                "title": node.title,
                "summary": node.summary,
                "level": node.level,
            }]

            current = node
            visited = {node.node_id}
            for _ in range(max_depth):
                if not current.links:
                    break
                # 取权重最高的因果边
                best_link = max(current.links, key=lambda l: l.weight)
                if best_link.target_id in visited:
                    break
                next_node = self.store.load(best_link.target_id)
                if not next_node:
                    break
                visited.add(next_node.node_id)
                chain.append({
                    "node_id": next_node.node_id,
                    "title": next_node.title,
                    "summary": next_node.summary,
                    "level": next_node.level,
                    "relation": best_link.link_type,
                    "weight": best_link.weight,
                })
                current = next_node

            return {
                "success": True,
                "source": "core_links",
                "chain_length": len(chain),
                "chain": chain,
            }

        except Exception as e:
            return {"success": False, "error": str(e)}

    # ================================================================
    # 认知网络集成
    # ================================================================

    def get_cognitive_state(
        self,
        current_query: str = "",
        limit: int = 10,
    ) -> Dict[str, Any]:
        """
        构建认知网络状态摘要（对应 get_cognitive_state）。
        从 Core 提取宇宙层 + 相关星系/恒星层的压缩视图。
        """
        try:
            # 宇宙层节点（全局原则）
            meta_nodes = self.store.list_by_level(1)

            # 如果当前有查询，搜索相关的高层节点
            relevant_nodes = []
            if current_query:
                relevant_nodes = self.search_engine.search(
                    current_query, level=None, limit=limit,
                )
                # 只保留高层节点（level <= 3）
                relevant_nodes = [n for n in relevant_nodes if n.level <= 3]

            # 否则取星系层最新节点
            if not relevant_nodes:
                galaxy_nodes = self.store.list_by_level(2)
                galaxy_nodes.sort(key=lambda n: n.created_at, reverse=True)
                relevant_nodes = galaxy_nodes[:limit]

            return {
                "success": True,
                "meta": [
                    {"node_id": n.node_id, "title": n.title, "summary": n.summary}
                    for n in meta_nodes
                ],
                "relevant": [
                    {
                        "node_id": n.node_id,
                        "title": n.title,
                        "summary": n.summary,
                        "level": n.level,
                        "level_name": LEVELS.get(n.level, ""),
                        "tags": n.tags,
                    }
                    for n in relevant_nodes
                ],
            }

        except Exception as e:
            return {"success": False, "error": str(e)}

    def build_phase_summary(
        self,
        current_query: str = "",
        current_phase: str = "",
        current_intent: str = "",
        limit: int = 8,
        recent_failures: List[str] = None,
    ) -> Dict[str, Any]:
        """
        构建阶段摘要（对应 build_phase_summary）。
        组合宇宙层摘要 + 当前意图相关的记忆路径。
        """
        try:
            # 基础上下文：宇宙层 + 星系层
            base_context = self.store.list_by_level(1) + self.store.list_by_level(2)

            # 当前查询相关节点
            relevant = []
            if current_query:
                relevant = self.search_engine.search(query=current_query, limit=limit)
            elif current_intent:
                relevant = self.search_engine.search(query=current_intent, limit=limit)

            # 失败相关
            failure_context = []
            if recent_failures:
                for failure in recent_failures:
                    fail_nodes = self.search_engine.search(query=failure, limit=3)
                    failure_context.extend(fail_nodes)

            # 按层级组织
            summary_parts = []
            for node in base_context[:3]:
                summary_parts.append(f"[{LEVELS.get(node.level, '')}] {node.summary}")

            for node in relevant[:limit]:
                if node not in base_context:
                    summary_parts.append(f"[{LEVELS.get(node.level, '')}] {node.summary}")

            return {
                "success": True,
                "phase": current_phase or "unknown",
                "summary": " | ".join(summary_parts) if summary_parts else "记忆网络就绪",
                "relevant_node_count": len(relevant),
            }

        except Exception as e:
            return {"success": False, "error": str(e)}

    def get_activation_set(
        self,
        current_query: str = "",
        current_phase: str = "",
        current_intent: str = "",
        limit: int = 5,
        recent_failures: List[str] = None,
    ) -> Dict[str, Any]:
        """
        获取激活节点集合（对应 get_activation_set）。
        用于运行时上下文注入。
        """
        try:
            activated = []

            # 按意图/查询搜索
            search_text = current_query or current_intent or current_phase
            if search_text:
                results = self.search_engine.search(query=search_text, limit=limit)
                activated = results

            # 补充最近失败的教训
            if recent_failures and len(activated) < limit:
                for failure in recent_failures[:3]:
                    fail_nodes = self.search_engine.search(query=failure, limit=2)
                    for fn in fail_nodes:
                        if fn not in activated:
                            activated.append(fn)
                            if len(activated) >= limit:
                                break

            # 默认补充最近的星系层节点
            if len(activated) < limit:
                galaxy = self.store.list_by_level(2)
                galaxy.sort(key=lambda n: n.created_at, reverse=True)
                for gn in galaxy:
                    if gn not in activated:
                        activated.append(gn)
                        if len(activated) >= limit:
                            break

            return {
                "success": True,
                "count": len(activated),
                "activated": [
                    {
                        "node_id": n.node_id,
                        "title": n.title,
                        "summary": n.summary,
                        "level": n.level,
                        "level_name": LEVELS.get(n.level, ""),
                        "tags": n.tags,
                    }
                    for n in activated
                ],
            }

        except Exception as e:
            return {"success": False, "error": str(e)}

    # ================================================================
    # 金字塔下钻（层级探索）
    # ================================================================

    def pyramid_drill_down(
        self,
        query: str = "",
        node_id: str = "",
        mode: str = "overview",
        level: int = None,
        max_results: int = 20,
    ) -> Dict[str, Any]:
        """
        沿金字塔层级下钻探索记忆网络。

        Mode "overview" (默认):
            返回金字塔全局结构：每层节点数、顶层节点摘要。
            无需任何参数即可使用。

        Mode "explore":
            从指定 node_id 展开其子节点（下一层）。
            必须提供 node_id。

        Mode "search":
            搜索关键词，按层级分组展示，附带每层的路径上下文。
            必须提供 query。

        Mode "drill":
            先搜索匹配的顶层节点，然后自动往下钻一层。
            必须提供 query。
        """
        try:
            if mode == "overview":
                return self._drill_overview(max_results)
            elif mode == "explore":
                return self._drill_explore(node_id, max_results)
            elif mode == "search":
                return self._drill_search(query, level, max_results)
            elif mode == "drill":
                return self._drill_auto(query, max_results)
            else:
                return {"success": False, "error": f"未知模式: {mode}，可选: overview / explore / search / drill"}
        except Exception as e:
            return {"success": False, "error": f"金字塔下钻失败: {str(e)}"}

    def repair_orphans(self, dry_run: bool = False) -> Dict[str, Any]:
        """
        修复所有孤儿节点（无 parent_id 的节点）。
        使用改进的 2-gram 中文分词 + 向上多层搜索。
        
        Args:
            dry_run: True 时只报告不修改
        Returns:
            {"success": True, "fixed": N, "skipped": N, "total_orphans": N}
        """
        try:
            result = self.nav.repair_orphans(dry_run=dry_run, verbose=False)
            return {
                "success": True,
                **result,
            }
        except Exception as e:
            return {"success": False, "error": f"修复孤儿节点失败: {str(e)}"}

    def _drill_overview(self, max_results: int) -> Dict[str, Any]:
        """模式 overview：金字塔全局结构"""
        counts = self.store.count_by_level()
        top_nodes = []
        for lvl in [1, 2, 3]:
            nodes = self.store.list_by_level(lvl)
            nodes.sort(key=lambda n: n.created_at, reverse=True)
            top_nodes.extend(nodes[:max_results // 3])

        return {
            "success": True,
            "mode": "overview",
            "pyramid": {
                "total_nodes": sum(counts.values()),
                "levels": [
                    {
                        "level": lvl,
                        "name": LEVELS.get(lvl, f"层级{lvl}"),
                        "count": counts.get(lvl, 0),
                    }
                    for lvl in sorted(counts.keys())
                ],
            },
            "top_nodes": [
                {
                    "node_id": n.node_id,
                    "title": n.title,
                    "summary": n.summary[:120] + "..." if len(n.summary) > 120 else n.summary,
                    "level": n.level,
                    "level_name": LEVELS.get(n.level, ""),
                    "tags": n.tags,
                }
                for n in top_nodes[:max_results]
            ],
        }

    def _drill_explore(self, node_id: str, max_results: int) -> Dict[str, Any]:
        """模式 explore：从节点展开子节点"""
        if not node_id:
            return {"success": False, "error": "explore 模式需要提供 node_id"}

        node = self.store.load(node_id)
        if not node:
            return {"success": False, "error": f"节点不存在: {node_id}"}

        children = self.nav.get_children(node_id)

        # 父路径（从顶到当前节点）
        path = self.nav.get_path_from_root(node_id)

        return {
            "success": True,
            "mode": "explore",
            "current_node": {
                "node_id": node.node_id,
                "title": node.title,
                "summary": node.summary[:200] + "..." if len(node.summary) > 200 else node.summary,
                "level": node.level,
                "level_name": LEVELS.get(node.level, ""),
                "tags": node.tags,
                "parent_id": node.parent_id,
                "children_count": len(children),
            },
            "path_from_root": [
                {
                    "node_id": p.node_id,
                    "title": p.title,
                    "level": p.level,
                    "level_name": LEVELS.get(p.level, ""),
                }
                for p in path
            ],
            "children": [
                {
                    "node_id": c.node_id,
                    "title": c.title,
                    "summary": c.summary[:150] + "..." if len(c.summary) > 150 else c.summary,
                    "level": c.level,
                    "level_name": LEVELS.get(c.level, ""),
                    "tags": c.tags,
                    "access_count": c.access_count,
                }
                for c in children[:max_results]
            ],
            "total_children": len(children),
        }

    def _drill_search(self, query: str, level: int, max_results: int) -> Dict[str, Any]:
        """模式 search：搜索并按层级分组"""
        if not query:
            return {"success": False, "error": "search 模式需要提供 query"}

        results = self.search_engine.search(query=query, level=level, limit=max_results * 3)

        # 按层级分组
        grouped = {}
        for node in results:
            lvl = node.level
            if lvl not in grouped:
                grouped[lvl] = []
            grouped[lvl].append(node)

        # 格式化
        levels_output = []
        for lvl in sorted(grouped.keys(), reverse=True):
            nodes = grouped[lvl][:max_results]
            levels_output.append({
                "level": lvl,
                "level_name": LEVELS.get(lvl, f"层级{lvl}"),
                "count": len(grouped[lvl]),
                "nodes": [
                    {
                        "node_id": n.node_id,
                        "title": n.title,
                        "summary": n.summary[:150] + "..." if len(n.summary) > 150 else n.summary,
                        "tags": n.tags,
                        "parent_id": n.parent_id,
                    }
                    for n in nodes
                ],
            })

        return {
            "success": True,
            "mode": "search",
            "query": query,
            "total_matches": len(results),
            "grouped_by_level": levels_output,
        }

    def _drill_auto(self, query: str, max_results: int) -> Dict[str, Any]:
        """模式 drill：搜索顶层节点 → 自动下钻一层"""
        if not query:
            return {"success": False, "error": "drill 模式需要提供 query"}

        # 先搜高优先级层级（1-3层为主）
        top_results = self.search_engine.search(query=query, limit=max_results)

        if not top_results:
            return {"success": False, "error": f"未找到与 '{query}' 相关的节点", "mode": "drill"}

        # 取最佳匹配节点
        best_node = top_results[0]

        # 获取从顶层到该节点的路径
        path = self.nav.get_path_from_root(best_node.node_id)

        # 获取子节点（下钻一层）
        children = self.nav.get_children(best_node.node_id)

        # 获取兄弟节点（同层相关）
        siblings = self.nav.get_siblings(best_node.node_id)

        return {
            "success": True,
            "mode": "drill",
            "query": query,
            "matched_node": {
                "node_id": best_node.node_id,
                "title": best_node.title,
                "summary": best_node.summary[:200] + "..." if len(best_node.summary) > 200 else best_node.summary,
                "level": best_node.level,
                "level_name": LEVELS.get(best_node.level, ""),
                "tags": best_node.tags,
                "parent_id": best_node.parent_id,
            },
            "path_from_root": [
                {
                    "node_id": p.node_id,
                    "title": p.title,
                    "level": p.level,
                    "level_name": LEVELS.get(p.level, ""),
                }
                for p in path
            ],
            "drill_down": [
                {
                    "node_id": c.node_id,
                    "title": c.title,
                    "summary": c.summary[:150] + "..." if len(c.summary) > 150 else c.summary,
                    "level": c.level,
                    "level_name": LEVELS.get(c.level, ""),
                    "tags": c.tags,
                }
                for c in children[:max_results]
            ],
            "total_children": len(children),
            "siblings": [
                {
                    "node_id": s.node_id,
                    "title": s.title,
                    "summary": s.summary[:100] + "..." if len(s.summary) > 100 else s.summary,
                }
                for s in siblings[:5]
            ] if siblings else [],
            "other_matches": [
                {
                    "node_id": n.node_id,
                    "title": n.title,
                    "level": n.level,
                    "level_name": LEVELS.get(n.level, ""),
                    "summary": n.summary[:100] + "..." if len(n.summary) > 100 else n.summary,
                }
                for n in top_results[1:6]
            ],
        }

    # ================================================================
    # 删除 & 清理
    # ================================================================

    def delete(self, node_id: str) -> Dict[str, Any]:
        """删除节点（对应 delete_memory）"""
        try:
            node = self.store.load(node_id)
            if not node:
                return {"success": False, "error": f"节点不存在: {node_id}"}

            # 从父节点移除引用
            if node.parent_id:
                parent = self.store.load(node.parent_id)
                if parent:
                    parent.remove_child(node_id)
                    self.store.save(parent)

            self.store.delete(node_id)
            return {"success": True, "message": f"已删除: {node_id}"}

        except Exception as e:
            return {"success": False, "error": str(e)}

    def batch_delete(self, node_ids: List[str]) -> Dict[str, Any]:
        """批量删除（对应 batch_delete_memories）"""
        results = []
        for nid in node_ids:
            results.append(self.delete(nid))
        return {
            "success": True,
            "deleted": sum(1 for r in results if r["success"]),
            "failed": sum(1 for r in results if not r["success"]),
        }

    def cleanup(
        self,
        days_old: int = DEFAULT_MAX_AGE_DAYS,
        min_importance: float = 0.3,
    ) -> Dict[str, Any]:
        """
        清理过期记忆（对应 cleanup_memories）。
        底层节点老化后向上传播摘要再删除。
        """
        try:
            cleaned = self.aging_engine.age(
                max_age_days=days_old,
                min_access_count=DEFAULT_MIN_ACCESS_COUNT,
            )
            return {
                "success": True,
                "cleaned": cleaned,
                "message": f"清理了 {cleaned} 条过期记忆",
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ================================================================
    # 导出 & 统计
    # ================================================================

    def export(
        self,
        format: str = "json",
        include_content: bool = True,
    ) -> Dict[str, Any]:
        """导出记忆（对应 export_memories）"""
        try:
            all_nodes = self.store.list_all()

            if format == "json":
                export_data = []
                for node in all_nodes:
                    item = {
                        "node_id": node.node_id,
                        "level": node.level,
                        "title": node.title,
                        "summary": node.summary,
                        "tags": node.tags,
                        "created_at": node.created_at,
                        "parent_id": node.parent_id,
                        "children_ids": node.children_ids,
                    }
                    if include_content:
                        item["content"] = node.content
                    export_data.append(item)

                return {
                    "success": True,
                    "format": "json",
                    "count": len(export_data),
                    "data": export_data,
                }
            else:
                return {"success": False, "error": f"不支持的导出格式: {format}"}

        except Exception as e:
            return {"success": False, "error": str(e)}

    def get_statistics(self) -> Dict[str, Any]:
        """获取存储统计（对应 store.get_statistics）"""
        try:
            stats = self.store.get_statistics()
            return {
                "success": True,
                **stats,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ================================================================
    # 内部辅助方法
    # ================================================================

    def _auto_link_new_node(self, node: MemoryNode, max_links: int = 5) -> int:
        """
        为新节点自动创建语义关联链接（横向边）。

        策略：
        1. 如果有向量模型 → 用向量余弦相似度精确匹配
        2. 否则降级为关键词重叠（旧逻辑）

        Args:
            node: 刚写入的新节点
            max_links: 最多创建多少条关联链接

        Returns:
            实际创建的链接数量
        """
        # ── 向量模型路径（优先） ──
        if self._embedder and node.has_embedding:
            return self._auto_link_by_vector(node, max_links)

        # ── 关键词路径（兜底） ──
        return self._auto_link_by_keyword(node, max_links)

    def _auto_link_by_vector(self, node: MemoryNode, max_links: int = 5) -> int:
        """使用向量余弦相似度自动创建关联边"""
        import math

        node_vec = node.get_embedding_vec()
        if node_vec is None:
            return 0

        # 收集所有有向量的候选节点（跨层级）
        all_nodes = self.store.list_all()
        scored = []

        for candidate in all_nodes:
            if candidate.node_id == node.node_id:
                continue
            if not candidate.has_embedding:
                # 候选节点没有向量 → 尝试现场计算
                if self._embedder:
                    try:
                        c_text = f"{candidate.title} {candidate.summary}"
                        c_vec = self._embedder.encode(c_text)
                        candidate.set_embedding_vec(c_vec)
                        self.store.save(candidate)
                    except Exception:
                        continue
                else:
                    continue

            c_vec = candidate.get_embedding_vec()
            if c_vec is None:
                continue

            # 跳过已经是父子关系的
            if candidate.parent_id == node.node_id or node.parent_id == candidate.node_id:
                continue

            sim = float(np.dot(node_vec, c_vec))

            # 相似度阈值：低于 0.5 的不建边
            if sim < 0.5:
                continue

            scored.append((sim, candidate))

        # 按相似度降序，取 top max_links
        scored.sort(key=lambda x: -x[0])
        link_count = 0

        for sim, candidate in scored[:max_links]:
            link_type, weight = self._infer_link_type_by_sim(node, candidate, sim)
            if weight < 0.3:
                continue

            desc = f"语义相似度 {sim:.2f}: {candidate.title[:60]}"
            if link_type == CausalLinkType.SUPPORTS.value:
                desc = f"强支持 (sim={sim:.2f}): {candidate.title[:60]}"
            elif link_type == CausalLinkType.REFINES.value:
                desc = f"细化 (sim={sim:.2f}): {candidate.title[:60]}"

            # 双向建边
            node.add_link(
                target_id=candidate.node_id,
                link_type=link_type,
                weight=weight,
                description=desc,
            )
            candidate.add_link(
                target_id=node.node_id,
                link_type=link_type,
                weight=weight,
                description=f"语义匹配 (sim={sim:.2f}): {node.title[:60]}",
            )
            self.store.save(candidate)
            link_count += 1

        if link_count > 0:
            self.store.save(node)

        return link_count

    def _infer_link_type_by_sim(
        self, source: MemoryNode, target: MemoryNode, sim: float
    ) -> tuple:
        """
        基于向量相似度 + 标签重叠推断关系类型和权重。

        返回: (link_type, weight)
        """
        shared_tags = set(source.tags) & set(target.tags)
        tag_overlap = len(shared_tags)

        # 权重 = 相似度 * 0.7 + 标签重叠 * 0.15（上限 1.0）
        weight = min(sim * 0.70 + tag_overlap * 0.15, 1.0)

        if sim >= 0.80 or tag_overlap >= 2:
            return CausalLinkType.SUPPORTS.value, round(weight, 2)
        elif sim >= 0.65 or tag_overlap >= 1:
            return CausalLinkType.REFINES.value, round(weight, 2)
        else:
            return CausalLinkType.RELATES_TO.value, round(weight, 2)

    def _auto_link_by_keyword(self, node: MemoryNode, max_links: int = 5) -> int:
        """关键词重叠方式创建关联边（旧逻辑，兜底用）"""
        query_parts = [node.title, node.summary]
        query_parts.extend(node.tags)
        query = " ".join(p for p in query_parts if p)
        if not query.strip():
            return 0

        candidates = self.search_engine.search(
            query=query,
            level=None,
            limit=max_links * 3,
        )

        candidates = [n for n in candidates if n.node_id != node.node_id]

        link_count = 0
        for candidate in candidates[:max_links]:
            if candidate.parent_id == node.node_id or node.parent_id == candidate.node_id:
                continue

            link_type, weight = self._infer_link_type(node, candidate)
            if weight < 0.3:
                continue

            desc = f"语义匹配: {candidate.title[:60]}"
            if link_type == CausalLinkType.SUPPORTS.value:
                desc = f"支持: {candidate.title[:60]}"
            elif link_type == CausalLinkType.REFINES.value:
                desc = f"细化: {candidate.title[:60]}"

            node.add_link(
                target_id=candidate.node_id,
                link_type=link_type,
                weight=weight,
                description=desc,
            )
            candidate.add_link(
                target_id=node.node_id,
                link_type=link_type,
                weight=weight,
                description=f"语义匹配: {node.title[:60]}",
            )
            self.store.save(candidate)
            link_count += 1

        if link_count > 0:
            self.store.save(node)

        return link_count

    def _infer_link_type(
        self, source: MemoryNode, target: MemoryNode
    ) -> tuple:
        """
        推断两个节点之间的关系类型和权重。

        规则：
        - 共享标签 ≥ 2 或关键词重叠 ≥ 5 → supports（强支持）
        - 共享标签 ≥ 1 或关键词重叠 ≥ 3 → refines（细化/补充）
        - 否则 → relates_to（弱相关）
        - 权重 = 0.3 ~ 1.0，基于标签和关键词重叠程度
        """
        # 标签重叠
        shared_tags = set(source.tags) & set(target.tags)
        tag_overlap = len(shared_tags)

        # 关键词重叠（从标题+摘要提取有意义的词）
        def extract_keywords(text: str) -> set:
            words = set()
            for part in text.lower().split():
                part = part.strip("，。！？、；：""''（）()[]【】")
                if len(part) > 1:  # 忽略单字
                    words.add(part)
            return words

        source_words = extract_keywords(f"{source.title} {source.summary}")
        target_words = extract_keywords(f"{target.title} {target.summary}")
        word_overlap = len(source_words & target_words)

        # 权重计算: 标签重叠权重更高
        weight = min(0.3 + (tag_overlap * 0.15) + (word_overlap * 0.04), 1.0)

        # 类型推断
        if tag_overlap >= 2 or word_overlap >= 5:
            return CausalLinkType.SUPPORTS.value, round(weight, 2)
        elif tag_overlap >= 1 or word_overlap >= 3:
            return CausalLinkType.REFINES.value, round(weight, 2)
        else:
            return CausalLinkType.RELATES_TO.value, round(weight, 2)

    # ================================================================
    # 跨分支向量重检（场景 C 核心）
    # ================================================================

    def cross_branch_link_check(self, node_id: str, sim_threshold: float = 0.55) -> int:
        """
        当父节点向量因子节点变化而被重新聚合后，检查该父节点与同层其他节点
        是否产生了新的语义关联。

        这就是场景 C 的"传播 → 自动建边"闭环：
        底层写入 → 向量沿父子链向上聚合 → 同层向量相似度重算 → 新关联边

        Args:
            node_id: 向量被更新的节点 ID
            sim_threshold: 相似度阈值

        Returns:
            新创建的链接数量
        """
        if not self._embedder:
            return 0

        node = self.store.load(node_id)
        if not node or not node.has_embedding:
            return 0

        node_vec = node.get_embedding_vec()
        if node_vec is None:
            return 0

        # 找同层其他有向量的节点
        siblings = self.store.list_by_level(node.level)
        new_link_count = 0

        for sibling in siblings:
            if sibling.node_id == node_id:
                continue

            # 检查是否已经有链接
            existing_targets = {l.target_id for l in node.links}
            if sibling.node_id in existing_targets:
                continue

            if not sibling.has_embedding:
                if self._embedder:
                    try:
                        s_text = f"{sibling.title} {sibling.summary}"
                        s_vec = self._embedder.encode(s_text)
                        sibling.set_embedding_vec(s_vec)
                        self.store.save(sibling)
                    except Exception:
                        continue
                else:
                    continue

            s_vec = sibling.get_embedding_vec()
            if s_vec is None:
                continue

            sim = float(np.dot(node_vec, s_vec))
            if sim < sim_threshold:
                continue

            # 通过了——建双向边
            link_type, weight = self._infer_link_type_by_sim(node, sibling, sim)
            if weight < 0.3:
                continue

            desc = f"向量传播发现 (sim={sim:.2f}): {sibling.title[:60]}"

            node.add_link(
                target_id=sibling.node_id,
                link_type=link_type,
                weight=weight,
                description=desc,
            )
            sibling.add_link(
                target_id=node.node_id,
                link_type=link_type,
                weight=weight,
                description=f"向量传播发现 (sim={sim:.2f}): {node.title[:60]}",
            )
            self.store.save(sibling)
            new_link_count += 1

        if new_link_count > 0:
            self.store.save(node)

        return new_link_count

    def _auto_match_parent(
        self,
        title: str,
        summary: str,
        tags: List[str],
        level: int,
    ) -> Optional[str]:
        """自动匹配父节点（改进版 v2：向上多层搜索 + 2-gram 匹配）"""
        if level <= 1:
            return None

        # 构建临时节点用于匹配
        temp_node = MemoryNode(
            node_id="_temp",
            level=level,
            title=title,
            summary=summary,
            tags=tags or [],
        )

        # 向上逐层搜索，选最佳匹配
        best_parent_id = None
        best_score = 0.0

        from .nav import _build_match_text, _tokenize_for_match
        node_tokens = _tokenize_for_match(_build_match_text(temp_node))
        if not node_tokens:
            return None

        for parent_level in range(level - 1, 0, -1):
            candidates = self.store.list_by_level(parent_level)
            if not candidates:
                continue

            for candidate in candidates:
                cand_tokens = _tokenize_for_match(_build_match_text(candidate))
                if not cand_tokens:
                    continue
                intersection = len(node_tokens & cand_tokens)
                union = len(node_tokens | cand_tokens)
                score = intersection / union if union > 0 else 0.0
                if score > best_score:
                    best_score = score
                    best_parent_id = candidate.node_id

        return best_parent_id if best_score > 0.005 else None

    def _vecdb_query(self, query: str, limit: int) -> List[Dict[str, Any]]:
        """调用 vecdb 进行语义搜索（如果已挂载）"""
        if not self._vecdb:
            return []
        try:
            results = self._vecdb.query(query, top_k=limit)
            formatted = []
            for r in results:
                node = self.store.load(r.get("id", r.get("node_id", "")))
                if node:
                    formatted.append({
                        "node_id": node.node_id,
                        "title": node.title,
                        "summary": node.summary,
                        "level": node.level,
                        "level_name": LEVELS.get(node.level, ""),
                        "tags": node.tags,
                        "created_at": node.created_at,
                        "score": r.get("score", 0),
                    })
            return formatted
        except Exception:
            return []

    def _kg_query_related(
        self,
        node_id: str,
        rel_type: str = "causes",
        direction: str = "both",
        depth: int = 3,
    ) -> List[Dict[str, Any]]:
        """调用 KG 进行因果链查询（如果已挂载）"""
        if not self._kg:
            return []
        try:
            results = self._kg.query_related(
                node_id, rel_type=rel_type,
                direction=direction, depth=depth,
            )
            return [
                {
                    "node_id": r.get("id", ""),
                    "title": r.get("title", ""),
                    "summary": r.get("summary", ""),
                    "relation": r.get("relation", rel_type),
                }
                for r in results
            ]
        except Exception:
            return []


# =================================================================
# 便捷工厂函数
# =================================================================

def create_api(storage_root: Path = None) -> MemoryAPI:
    """创建 MemoryAPI 实例的便捷函数"""
    return MemoryAPI(storage_root=storage_root)
