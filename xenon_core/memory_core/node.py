"""
node.py — MemoryNode 数据模型

定义记忆网络中每个节点的数据结构。
这是整个 Core 的基础——所有模块都使用这个模型。
"""

from dataclasses import dataclass, field, asdict
from typing import Optional, List
from datetime import datetime

import numpy as np

from .schema import SourceType, CausalLinkType


@dataclass
class CausalLink:
    """横向因果边"""
    target_id: str                        # 目标节点 ID
    link_type: str                        # 因果类型，取值见 CausalLinkType
    weight: float = 1.0                   # 权重 0.0 ~ 1.0
    description: str = ""                 # 关系描述

    @classmethod
    def from_dict(cls, data: dict) -> "CausalLink":
        return cls(
            target_id=data["target_id"],
            link_type=data.get("link_type", CausalLinkType.RELATES_TO.value),
            weight=data.get("weight", 1.0),
            description=data.get("description", ""),
        )


@dataclass
class MemoryNode:
    """
    记忆网络中的一个节点。

    纵向关系（层级导航）:
      parent_id  → 父节点
      children_ids → 子节点列表

    横向关系（因果推理）:
      links → 因果边列表
    """

    # ======== 标识 ========
    node_id: str                          # 唯一标识，如 "galaxy_toolchain_001"
    level: int                            # 1-7，对应宇宙→分子

    # ======== 内容 ========
    title: str = ""                       # 节点标题
    summary: str = ""                     # 有损摘要，上层更短
    content: str = ""                     # 完整内容，底层保留

    # ======== 层级关系（纵向） ========
    parent_id: Optional[str] = None       # 父节点 ID
    children_ids: List[str] = field(default_factory=list)  # 子节点 ID 列表

    # ======== 因果链接（横向） ========
    links: List[CausalLink] = field(default_factory=list)

    # ======== 语义向量（向量模型注入后可用） ========
    embedding: Optional[List[float]] = None   # 512 维语义向量（bge-small-zh-v1.5）

    # ======== 元数据 ========
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    last_accessed: str = field(default_factory=lambda: datetime.now().isoformat())
    access_count: int = 0
    source_type: str = SourceType.AUTO_GENERATED.value
    tags: List[str] = field(default_factory=list)
    version: int = 1

    # ======== 方法 ========

    def touch(self):
        """更新访问时间和计数"""
        self.last_accessed = datetime.now().isoformat()
        self.access_count += 1

    def add_child(self, child_id: str):
        """添加子节点"""
        if child_id not in self.children_ids:
            self.children_ids.append(child_id)

    def remove_child(self, child_id: str):
        """移除子节点"""
        if child_id in self.children_ids:
            self.children_ids.remove(child_id)

    def add_link(self, target_id: str, link_type: str,
                 weight: float = 1.0, description: str = ""):
        """添加因果边"""
        # 不重复添加同类型边
        for link in self.links:
            if link.target_id == target_id and link.link_type == link_type:
                link.weight = max(link.weight, weight)
                link.description = description or link.description
                return
        self.links.append(CausalLink(
            target_id=target_id,
            link_type=link_type,
            weight=weight,
            description=description,
        ))

    @property
    def has_embedding(self) -> bool:
        """是否有语义向量"""
        return self.embedding is not None and len(self.embedding) > 0

    def get_embedding_vec(self) -> "Optional[np.ndarray]":
        """获取向量（numpy 格式），没有则返回 None"""
        if self.embedding is None:
            return None
        return np.asarray(self.embedding, dtype=np.float32)

    def set_embedding_vec(self, vec: "np.ndarray"):
        """从 numpy 数组设置向量"""
        self.embedding = vec.tolist()

    def to_dict(self) -> dict:
        """序列化为字典"""
        data = asdict(self)
        # 将 CausalLink 对象转为字典
        data["links"] = [
            {"target_id": l.target_id, "link_type": l.link_type,
             "weight": l.weight, "description": l.description}
            for l in self.links
        ]
        # embedding 保留为 List[float]（asdict 已自动处理）
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "MemoryNode":
        """从字典反序列化"""
        links_data = data.pop("links", [])
        links = [CausalLink.from_dict(l) for l in links_data]

        node = cls(
            node_id=data["node_id"],
            level=data["level"],
            title=data.get("title", ""),
            summary=data.get("summary", ""),
            content=data.get("content", ""),
            parent_id=data.get("parent_id"),
            children_ids=data.get("children_ids", []),
            links=links,
            embedding=data.get("embedding"),
            created_at=data.get("created_at", ""),
            last_accessed=data.get("last_accessed", ""),
            access_count=data.get("access_count", 0),
            source_type=data.get("source_type", SourceType.AUTO_GENERATED.value),
            tags=data.get("tags", []),
            version=data.get("version", 1),
        )
        return node
