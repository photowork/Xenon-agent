#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
知识图谱工具 — KnowledgeGraphHandler / KgToolManager

Phase 1：基础 CRUD + 异步建图 + 游标分页查询
Phase 2（预留）：路径查询、社区发现、中心度、导入、可视化
"""

from __future__ import annotations

import base64
import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# 私有配置模块
from Tools.knowledge_graph_handler._config import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    DEFAULT_DEPTH,
    MAX_DEPTH,
    SQLITE_PRAGMAS,
    ensure_dirs,
    get_db_path,
    get_root_dir,
    get_storage_root,
)


# =============================================================================
# Schema SQL（集中定义，建库时执行）
# =============================================================================

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS graphs (
    graph_id    TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    description TEXT DEFAULT '',
    config      TEXT DEFAULT '{}',
    status      TEXT DEFAULT 'idle',
    node_count  INTEGER DEFAULT 0,
    edge_count  INTEGER DEFAULT 0,
    created_at  TEXT DEFAULT (datetime('now','localtime')),
    updated_at  TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS nodes (
    node_id     TEXT PRIMARY KEY,
    graph_id    TEXT NOT NULL,
    label       TEXT NOT NULL,
    name        TEXT NOT NULL,
    properties  TEXT DEFAULT '{}',
    created_at  TEXT DEFAULT (datetime('now','localtime')),
    FOREIGN KEY (graph_id) REFERENCES graphs(graph_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_nodes_graph ON nodes(graph_id);
CREATE INDEX IF NOT EXISTS idx_nodes_label ON nodes(label);
CREATE INDEX IF NOT EXISTS idx_nodes_name  ON nodes(name);

CREATE TABLE IF NOT EXISTS edges (
    edge_id     TEXT PRIMARY KEY,
    graph_id    TEXT NOT NULL,
    source_id   TEXT NOT NULL,
    target_id   TEXT NOT NULL,
    relation    TEXT NOT NULL,
    weight      REAL DEFAULT 1.0,
    properties  TEXT DEFAULT '{}',
    created_at  TEXT DEFAULT (datetime('now','localtime')),
    FOREIGN KEY (graph_id)  REFERENCES graphs(graph_id)  ON DELETE CASCADE,
    FOREIGN KEY (source_id) REFERENCES nodes(node_id)     ON DELETE CASCADE,
    FOREIGN KEY (target_id) REFERENCES nodes(node_id)     ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_edges_graph    ON edges(graph_id);
CREATE INDEX IF NOT EXISTS idx_edges_source   ON edges(source_id);
CREATE INDEX IF NOT EXISTS idx_edges_target   ON edges(target_id);
CREATE INDEX IF NOT EXISTS idx_edges_relation ON edges(relation);

CREATE TABLE IF NOT EXISTS jobs (
    job_id          TEXT PRIMARY KEY,
    graph_id        TEXT NOT NULL,
    status          TEXT DEFAULT 'queued',
    phase           TEXT DEFAULT '',
    progress        REAL DEFAULT 0,
    total_files     INTEGER DEFAULT 0,
    processed_files INTEGER DEFAULT 0,
    error_message   TEXT DEFAULT '',
    pid             INTEGER DEFAULT 0,
    started_at      TEXT,
    completed_at    TEXT,
    created_at      TEXT DEFAULT (datetime('now','localtime')),
    FOREIGN KEY (graph_id) REFERENCES graphs(graph_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS file_index (
    file_id     TEXT PRIMARY KEY,
    graph_id    TEXT NOT NULL,
    file_path   TEXT NOT NULL,
    file_size   INTEGER DEFAULT 0,
    file_hash   TEXT DEFAULT '',
    chunk_count INTEGER DEFAULT 0,
    status      TEXT DEFAULT 'pending',
    processed_at TEXT,
    FOREIGN KEY (graph_id) REFERENCES graphs(graph_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_file_graph ON file_index(graph_id);
"""


# =============================================================================
# 游标工具
# =============================================================================

def _make_cursor(graph_id: str, page: int, query_type: str, query_params: dict) -> str:
    """生成游标字符串（base64 编码 JSON）。"""
    payload = {
        "g": graph_id,
        "p": page,
        "t": query_type,
        "q": query_params,
    }
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return base64.b64encode(raw.encode("utf-8")).decode("ascii")


def _parse_cursor(cursor: str) -> Optional[dict]:
    """解析游标字符串，失败返回 None。"""
    try:
        raw = base64.b64decode(cursor.encode("ascii")).decode("utf-8")
        return json.loads(raw)
    except Exception:
        return None


def _now() -> str:
    """返回当前本地时间字符串。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# =============================================================================
# 核心逻辑类：KnowledgeGraphManager
# =============================================================================

class KnowledgeGraphManager:
    """知识图谱核心引擎 — SQLite 存储 + 游标分页查询。

    注意：此类不直接暴露为工具，而是由 KgToolManager 包装。
    """

    def __init__(self, storage_root: Optional[str] = None):
        self._storage_root = self._normalize_storage_root(storage_root or get_storage_root())
        self._lock = threading.Lock()
        # 连接缓存：{db_path: sqlite3.Connection}
        self._conns: Dict[str, sqlite3.Connection] = {}
        # 自定义 storage_path 创建的图谱需要在同一 manager 生命周期内被记住。
        self._graph_storage_roots: Dict[str, str] = {}

    # ------------------------------------------------------------------
    # 连接管理
    # ------------------------------------------------------------------

    def _normalize_storage_root(self, storage_root: str) -> str:
        """规范化存储根目录。"""
        return os.path.abspath(os.path.expanduser(storage_root))

    def _storage_root_for_config(self, config: Optional[dict] = None) -> str:
        """根据图谱配置解析实际存储根目录。"""
        custom_storage = (config or {}).get("storage_path")
        if custom_storage:
            return self._normalize_storage_root(get_storage_root(custom_storage))
        return self._storage_root

    def _storage_root_for_graph(self, graph_id: str) -> str:
        """获取图谱所在的存储根目录。"""
        return self._graph_storage_roots.get(graph_id, self._storage_root)

    def _db_path_for_graph(self, graph_id: str) -> str:
        """获取图谱数据库路径。"""
        return get_db_path(graph_id, self._storage_root_for_graph(graph_id))

    def _connect_db_path(self, db_path: str) -> sqlite3.Connection:
        """按数据库路径获取连接（带缓存）。"""
        if db_path not in self._conns:
            os.makedirs(os.path.dirname(db_path), exist_ok=True)
            conn = sqlite3.connect(db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            for pragma in SQLITE_PRAGMAS:
                try:
                    conn.execute(pragma)
                except Exception:
                    pass
            self._conns[db_path] = conn
        return self._conns[db_path]

    def _get_conn(self, graph_id: str) -> sqlite3.Connection:
        """获取指定图谱的数据库连接（带缓存）。"""
        return self._connect_db_path(self._db_path_for_graph(graph_id))

    def _init_schema(self, conn: sqlite3.Connection) -> None:
        """执行建表 SQL。"""
        conn.executescript(SCHEMA_SQL)
        conn.commit()

    def _graph_exists(self, conn: sqlite3.Connection, graph_id: str) -> bool:
        """检查图谱元记录是否存在。"""
        row = conn.execute(
            "SELECT 1 FROM graphs WHERE graph_id=?", (graph_id,)
        ).fetchone()
        return row is not None

    def _row_to_dict(self, row: sqlite3.Row) -> dict:
        """将 SQLite 行转为 dict，并解码常见 JSON 字段。"""
        data = dict(row)
        for key in ("config", "properties"):
            value = data.get(key)
            if isinstance(value, str):
                try:
                    data[key] = json.loads(value) if value else {}
                except Exception:
                    data[key] = value
        return data

    def _known_storage_roots(self) -> List[str]:
        """返回当前 manager 可发现的所有存储根目录。"""
        roots = {self._storage_root}
        roots.update(self._graph_storage_roots.values())
        return sorted(roots)

    def _iter_graph_db_files(self):
        """遍历已知存储根目录中的图谱数据库。"""
        seen = set()
        for storage_root in self._known_storage_roots():
            graphs_dir = os.path.join(storage_root, "graphs")
            if not os.path.isdir(graphs_dir):
                continue
            for fname in os.listdir(graphs_dir):
                if not fname.endswith(".db"):
                    continue
                graph_id = fname[:-3]
                key = (storage_root, graph_id)
                if key in seen:
                    continue
                seen.add(key)
                self._graph_storage_roots.setdefault(graph_id, storage_root)
                yield graph_id, os.path.join(graphs_dir, fname), storage_root

    def _close_all(self) -> None:
        """关闭所有数据库连接。"""
        for conn in self._conns.values():
            try:
                conn.close()
            except Exception:
                pass
        self._conns.clear()

    # ------------------------------------------------------------------
    # 图谱 CRUD
    # ------------------------------------------------------------------

    def create_graph(
        self,
        name: str,
        description: str = "",
        config: Optional[dict] = None,
    ) -> dict:
        """创建新知识图谱。

        Args:
            name: 图谱名称
            description: 描述（可选）
            config: 自定义配置，如 {"storage_path": "..."}

        Returns:
            {"success": true, "data": {"graph_id": "...", "status": "idle"}}
        """
        try:
            graph_id = f"kg_{uuid.uuid4().hex[:12]}"
            now = _now()
            config_json = json.dumps(config or {}, ensure_ascii=False)

            # 如果 config 中包含 storage_path，用它覆盖；否则沿用 manager 的 storage_root。
            storage_root = self._storage_root_for_config(config)
            self._graph_storage_roots[graph_id] = storage_root
            db_path = get_db_path(graph_id, storage_root)

            os.makedirs(os.path.dirname(db_path), exist_ok=True)
            conn = sqlite3.connect(db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            for pragma in SQLITE_PRAGMAS:
                try:
                    conn.execute(pragma)
                except Exception:
                    pass
            self._init_schema(conn)

            conn.execute(
                "INSERT INTO graphs (graph_id, name, description, config, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (graph_id, name, description, config_json, now, now),
            )
            conn.commit()
            self._conns[db_path] = conn

            return {
                "success": True,
                "data": {"graph_id": graph_id, "status": "idle"},
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    def list_graphs(self, page: int = 1, page_size: int = 20) -> dict:
        """列出所有图谱。

        Args:
            page: 页码（从 1 开始）
            page_size: 每页数量

        Returns:
            {"success": true, "data": {"graphs": [...], "total": N, "page": 1}}
        """
        try:
            page = max(1, int(page or 1))
            page_size = min(max(1, int(page_size or 20)), MAX_PAGE_SIZE)
            offset = (page - 1) * page_size

            # 扫描所有图谱数据库获取元信息
            all_graphs = []
            for gid, db_path, _storage_root in self._iter_graph_db_files():
                try:
                    conn = self._connect_db_path(db_path)
                    self._init_schema(conn)
                    row = conn.execute(
                        "SELECT * FROM graphs WHERE graph_id=?", (gid,)
                    ).fetchone()
                    if row:
                        all_graphs.append(self._row_to_dict(row))
                    else:
                        # 数据库存在但没记录，补一个最小记录
                        all_graphs.append({
                            "graph_id": gid,
                            "name": gid,
                            "description": "",
                            "config": "{}",
                            "status": "unknown",
                            "node_count": 0,
                            "edge_count": 0,
                            "created_at": "",
                            "updated_at": "",
                        })
                except Exception:
                    all_graphs.append({
                        "graph_id": gid,
                        "name": gid,
                        "description": "",
                        "config": "{}",
                        "status": "error",
                        "node_count": 0,
                        "edge_count": 0,
                        "created_at": "",
                        "updated_at": "",
                    })

            all_graphs.sort(key=lambda g: g.get("created_at", ""), reverse=True)
            total = len(all_graphs)
            paged = all_graphs[offset : offset + page_size]

            return {
                "success": True,
                "data": {"graphs": paged, "total": total, "page": page},
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    def delete_graph(self, graph_id: str) -> dict:
        """删除整个图谱（含数据库文件）。

        Args:
            graph_id: 图谱 ID

        Returns:
            {"success": true | false}
        """
        try:
            db_path = self._db_path_for_graph(graph_id)
            # 关闭连接
            if db_path in self._conns:
                try:
                    self._conns[db_path].close()
                except Exception:
                    pass
                del self._conns[db_path]
            # 删除数据库文件和 WAL sidecar
            for path in (db_path, f"{db_path}-wal", f"{db_path}-shm"):
                if os.path.exists(path):
                    os.remove(path)
            self._graph_storage_roots.pop(graph_id, None)
            return {"success": True, "data": {"graph_id": graph_id}}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ------------------------------------------------------------------
    # 节点/边 CRUD
    # ------------------------------------------------------------------

    def add_node(
        self,
        graph_id: str,
        node_id: str,
        label: str,
        name: str,
        properties: Optional[dict] = None,
    ) -> dict:
        """添加单个节点。

        Args:
            graph_id: 图谱 ID
            node_id: 节点唯一标识
            label: 节点类型标签，如 "Person", "Concept"
            name: 节点名称
            properties: 属性字典（JSON）

        Returns:
            {"success": true, "data": {"node_id": "..."}}
        """
        try:
            conn = self._get_conn(graph_id)
            self._init_schema(conn)
            if not self._graph_exists(conn, graph_id):
                return {"success": False, "error": f"图谱不存在: {graph_id}"}
            props_json = json.dumps(properties or {}, ensure_ascii=False)
            now = _now()
            conn.execute(
                "INSERT OR REPLACE INTO nodes (node_id, graph_id, label, name, properties, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (node_id, graph_id, label, name, props_json, now),
            )
            conn.commit()
            self._sync_counts(conn, graph_id)
            return {"success": True, "data": {"node_id": node_id}}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def add_edge(
        self,
        graph_id: str,
        edge_id: str,
        source_id: str,
        target_id: str,
        relation: str,
        weight: float = 1.0,
        properties: Optional[dict] = None,
    ) -> dict:
        """添加单条边。

        Args:
            graph_id: 图谱 ID
            edge_id: 边唯一标识
            source_id: 源节点 ID
            target_id: 目标节点 ID
            relation: 关系类型
            weight: 权重
            properties: 属性字典

        Returns:
            {"success": true, "data": {"edge_id": "..."}}
        """
        try:
            conn = self._get_conn(graph_id)
            self._init_schema(conn)
            if not self._graph_exists(conn, graph_id):
                return {"success": False, "error": f"图谱不存在: {graph_id}"}
            missing_nodes = [
                nid for nid in (source_id, target_id)
                if not conn.execute(
                    "SELECT 1 FROM nodes WHERE graph_id=? AND node_id=?",
                    (graph_id, nid),
                ).fetchone()
            ]
            if missing_nodes:
                return {"success": False, "error": f"节点不存在: {', '.join(missing_nodes)}"}
            props_json = json.dumps(properties or {}, ensure_ascii=False)
            now = _now()
            conn.execute(
                "INSERT OR REPLACE INTO edges (edge_id, graph_id, source_id, target_id, relation, weight, properties, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (edge_id, graph_id, source_id, target_id, relation, weight, props_json, now),
            )
            conn.commit()
            self._sync_counts(conn, graph_id)
            return {"success": True, "data": {"edge_id": edge_id}}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def batch_add_nodes(self, graph_id: str, nodes: list) -> dict:
        """批量添加节点。

        Args:
            graph_id: 图谱 ID
            nodes: 节点列表，每个节点为 {node_id, label, name, properties?}

        Returns:
            {"success": true, "data": {"count": N}}
        """
        try:
            conn = self._get_conn(graph_id)
            self._init_schema(conn)
            if not self._graph_exists(conn, graph_id):
                return {"success": False, "error": f"图谱不存在: {graph_id}"}
            count = 0
            with conn:
                for n in nodes:
                    nid = n.get("node_id", "")
                    if not nid:
                        continue
                    props = json.dumps(n.get("properties", {}), ensure_ascii=False)
                    now = _now()
                    conn.execute(
                        "INSERT OR REPLACE INTO nodes (node_id, graph_id, label, name, properties, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (nid, graph_id, n.get("label", ""), n.get("name", ""), props, now),
                    )
                    count += 1
            self._sync_counts(conn, graph_id)
            return {"success": True, "data": {"count": count}}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def batch_add_edges(self, graph_id: str, edges: list) -> dict:
        """批量添加边。

        Args:
            graph_id: 图谱 ID
            edges: 边列表，每个边为 {edge_id, source_id, target_id, relation, weight?, properties?}

        Returns:
            {"success": true, "data": {"count": N}}
        """
        try:
            conn = self._get_conn(graph_id)
            self._init_schema(conn)
            if not self._graph_exists(conn, graph_id):
                return {"success": False, "error": f"图谱不存在: {graph_id}"}
            count = 0
            with conn:
                for e in edges:
                    eid = e.get("edge_id", "")
                    if not eid:
                        continue
                    source_id = e.get("source_id", "")
                    target_id = e.get("target_id", "")
                    missing_nodes = [
                        nid for nid in (source_id, target_id)
                        if not conn.execute(
                            "SELECT 1 FROM nodes WHERE graph_id=? AND node_id=?",
                            (graph_id, nid),
                        ).fetchone()
                    ]
                    if missing_nodes:
                        raise ValueError(f"节点不存在: {', '.join(missing_nodes)}")
                    props = json.dumps(e.get("properties", {}), ensure_ascii=False)
                    now = _now()
                    conn.execute(
                        "INSERT OR REPLACE INTO edges (edge_id, graph_id, source_id, target_id, relation, weight, properties, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            eid, graph_id,
                            source_id, target_id,
                            e.get("relation", ""), float(e.get("weight", 1.0)),
                            props, now,
                        ),
                    )
                    count += 1
            self._sync_counts(conn, graph_id)
            return {"success": True, "data": {"count": count}}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def remove_node(self, graph_id: str, node_id: str) -> dict:
        """删除节点（连带删除相关边）。

        Args:
            graph_id: 图谱 ID
            node_id: 节点 ID

        Returns:
            {"success": true, "data": {"node_id": "...", "deleted_edges": N}}
        """
        try:
            conn = self._get_conn(graph_id)
            self._init_schema(conn)
            if not self._graph_exists(conn, graph_id):
                return {"success": False, "error": f"图谱不存在: {graph_id}"}
            deleted_edges = 0
            with conn:
                cur = conn.execute(
                    "DELETE FROM edges WHERE graph_id=? AND (source_id=? OR target_id=?)",
                    (graph_id, node_id, node_id),
                )
                deleted_edges = cur.rowcount
                conn.execute(
                    "DELETE FROM nodes WHERE graph_id=? AND node_id=?",
                    (graph_id, node_id),
                )
            self._sync_counts(conn, graph_id)
            return {"success": True, "data": {"node_id": node_id, "deleted_edges": deleted_edges}}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def remove_edge(self, graph_id: str, edge_id: str) -> dict:
        """删除单条边。

        Args:
            graph_id: 图谱 ID
            edge_id: 边 ID

        Returns:
            {"success": true, "data": {"edge_id": "..."}}
        """
        try:
            conn = self._get_conn(graph_id)
            self._init_schema(conn)
            if not self._graph_exists(conn, graph_id):
                return {"success": False, "error": f"图谱不存在: {graph_id}"}
            conn.execute(
                "DELETE FROM edges WHERE graph_id=? AND edge_id=?",
                (graph_id, edge_id),
            )
            conn.commit()
            self._sync_counts(conn, graph_id)
            return {"success": True, "data": {"edge_id": edge_id}}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def get_node(
        self,
        graph_id: str,
        node_id: str,
        expand_relations: bool = False,
        relation_limit: int = 50,
    ) -> dict:
        """获取节点详情。

        Args:
            graph_id: 图谱 ID
            node_id: 节点 ID
            expand_relations: 是否展开关联边
            relation_limit: 关联边数量上限

        Returns:
            {"success": true, "data": {"node": {...}, "relations": [...]}}
        """
        try:
            conn = self._get_conn(graph_id)
            self._init_schema(conn)
            if not self._graph_exists(conn, graph_id):
                return {"success": False, "error": f"图谱不存在: {graph_id}"}
            row = conn.execute(
                "SELECT * FROM nodes WHERE graph_id=? AND node_id=?", (graph_id, node_id)
            ).fetchone()
            if not row:
                return {"success": False, "error": f"节点不存在: {node_id}"}

            node = self._row_to_dict(row)
            relations = []
            if expand_relations:
                rel_rows = conn.execute(
                    "SELECT * FROM edges WHERE graph_id=? AND (source_id=? OR target_id=?) LIMIT ?",
                    (graph_id, node_id, node_id, relation_limit),
                ).fetchall()
                relations = [self._row_to_dict(r) for r in rel_rows]

            return {"success": True, "data": {"node": node, "relations": relations}}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def query_nodes(
        self,
        graph_id: str,
        label_filter: Optional[str] = None,
        name_search: Optional[str] = None,
        properties_filter: Optional[str] = None,
        page: int = 1,
        page_size: int = 50,
    ) -> dict:
        """搜索节点（带游标分页）。

        Args:
            graph_id: 图谱 ID
            label_filter: 按标签过滤
            name_search: 按名称模糊搜索（LIKE）
            properties_filter: JSON 属性过滤（暂未实现）
            page: 页码
            page_size: 每页数量

        Returns:
            {"success": true, "data": {"summary": {...}, "preview": [...], "cursor_next": "..."}}
        """
        try:
            conn = self._get_conn(graph_id)
            self._init_schema(conn)
            if not self._graph_exists(conn, graph_id):
                return {"success": False, "error": f"图谱不存在: {graph_id}"}

            where = ["graph_id = ?"]
            params: list = [graph_id]

            if label_filter:
                where.append("label = ?")
                params.append(label_filter)
            if name_search:
                where.append("name LIKE ?")
                params.append(f"%{name_search}%")

            where_clause = " AND ".join(where)

            # 统计总数
            total = conn.execute(
                f"SELECT COUNT(*) FROM nodes WHERE {where_clause}", params
            ).fetchone()[0]

            page = max(1, int(page or 1))
            page_size = min(max(1, int(page_size or 50)), MAX_PAGE_SIZE)
            offset = (page - 1) * page_size

            rows = conn.execute(
                f"SELECT * FROM nodes WHERE {where_clause} ORDER BY created_at DESC LIMIT ? OFFSET ?",
                params + [page_size, offset],
            ).fetchall()

            nodes = [self._row_to_dict(r) for r in rows]

            cursor_next = None
            if offset + page_size < total:
                cursor_next = _make_cursor(graph_id, page + 1, "query_nodes", {
                    "label_filter": label_filter,
                    "name_search": name_search,
                    "page_size": page_size,
                })

            return {
                "success": True,
                "data": {
                    "summary": {
                        "total_match": total,
                        "showing": f"page {page}/{max(1, (total + page_size - 1) // page_size)}",
                    },
                    "preview": nodes,
                    "cursor_next": cursor_next,
                },
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    def query_edges(
        self,
        graph_id: str,
        source_id: Optional[str] = None,
        target_id: Optional[str] = None,
        relation_filter: Optional[str] = None,
        page: int = 1,
        page_size: int = 50,
    ) -> dict:
        """搜索边（带游标分页）。

        Args:
            graph_id: 图谱 ID
            source_id: 按源节点过滤
            target_id: 按目标节点过滤
            relation_filter: 按关系类型过滤
            page: 页码
            page_size: 每页数量

        Returns:
            {"success": true, "data": {"summary": {...}, "preview": [...], "cursor_next": "..."}}
        """
        try:
            conn = self._get_conn(graph_id)
            self._init_schema(conn)
            if not self._graph_exists(conn, graph_id):
                return {"success": False, "error": f"图谱不存在: {graph_id}"}

            where = ["graph_id = ?"]
            params: list = [graph_id]

            if source_id:
                where.append("source_id = ?")
                params.append(source_id)
            if target_id:
                where.append("target_id = ?")
                params.append(target_id)
            if relation_filter:
                where.append("relation = ?")
                params.append(relation_filter)

            where_clause = " AND ".join(where)

            total = conn.execute(
                f"SELECT COUNT(*) FROM edges WHERE {where_clause}", params
            ).fetchone()[0]

            page = max(1, int(page or 1))
            page_size = min(max(1, int(page_size or 50)), MAX_PAGE_SIZE)
            offset = (page - 1) * page_size

            rows = conn.execute(
                f"SELECT * FROM edges WHERE {where_clause} ORDER BY created_at DESC LIMIT ? OFFSET ?",
                params + [page_size, offset],
            ).fetchall()

            edges = [self._row_to_dict(r) for r in rows]

            cursor_next = None
            if offset + page_size < total:
                cursor_next = _make_cursor(graph_id, page + 1, "query_edges", {
                    "source_id": source_id,
                    "target_id": target_id,
                    "relation_filter": relation_filter,
                    "page_size": page_size,
                })

            return {
                "success": True,
                "data": {
                    "summary": {
                        "total_match": total,
                        "showing": f"page {page}/{max(1, (total + page_size - 1) // page_size)}",
                    },
                    "preview": edges,
                    "cursor_next": cursor_next,
                },
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    def query_subgraph(
        self,
        graph_id: str,
        center_node: str,
        depth: int = 1,
        direction: str = "both",
        page: int = 1,
        page_size: int = 50,
    ) -> dict:
        """获取某节点周围子图（BFS 邻居展开）。

        Args:
            graph_id: 图谱 ID
            center_node: 中心节点 ID
            depth: 展开深度 (1-3)
            direction: "both" | "out" | "in"
            page: 页码
            page_size: 每页数量

        Returns:
            {"success": true, "data": {"summary": {...}, "nodes": [...], "edges": [...], "cursor_next": "..."}}
        """
        try:
            conn = self._get_conn(graph_id)
            self._init_schema(conn)
            if not self._graph_exists(conn, graph_id):
                return {"success": False, "error": f"图谱不存在: {graph_id}"}

            depth = min(max(1, int(depth or 1)), MAX_DEPTH)
            direction = direction if direction in ("both", "out", "in") else "both"

            # 检查中心节点
            center_row = conn.execute(
                "SELECT * FROM nodes WHERE graph_id=? AND node_id=?", (graph_id, center_node)
            ).fetchone()
            if not center_row:
                return {"success": False, "error": f"中心节点不存在: {center_node}"}

            # BFS 收集节点和边
            visited_nodes = {center_node}
            all_edges: List[dict] = []
            current_layer = {center_node}

            for d in range(depth):
                next_layer = set()
                for nid in current_layer:
                    if direction in ("both", "out"):
                        rows = conn.execute(
                            "SELECT * FROM edges WHERE graph_id=? AND source_id=?",
                            (graph_id, nid),
                        ).fetchall()
                        for r in rows:
                            e = self._row_to_dict(r)
                            all_edges.append(e)
                            if e["target_id"] not in visited_nodes:
                                visited_nodes.add(e["target_id"])
                                next_layer.add(e["target_id"])

                    if direction in ("both", "in"):
                        rows = conn.execute(
                            "SELECT * FROM edges WHERE graph_id=? AND target_id=?",
                            (graph_id, nid),
                        ).fetchall()
                        for r in rows:
                            e = self._row_to_dict(r)
                            all_edges.append(e)
                            if e["source_id"] not in visited_nodes:
                                visited_nodes.add(e["source_id"])
                                next_layer.add(e["source_id"])
                current_layer = next_layer

            # 获取节点详情
            node_list = list(visited_nodes)
            total_nodes = len(node_list)

            page = max(1, int(page or 1))
            page_size = min(max(1, int(page_size or 50)), MAX_PAGE_SIZE)
            offset = (page - 1) * page_size

            # 分页返回节点
            paged_nodes_ids = node_list[offset : offset + page_size]
            paged_nodes = []
            for nid in paged_nodes_ids:
                row = conn.execute(
                    "SELECT * FROM nodes WHERE graph_id=? AND node_id=?", (graph_id, nid)
                ).fetchone()
                if row:
                    paged_nodes.append(self._row_to_dict(row))

            # 分页返回相关边
            edge_offset = (page - 1) * page_size
            paged_edges = all_edges[edge_offset : edge_offset + page_size]

            cursor_next = None
            if offset + page_size < total_nodes or edge_offset + page_size < len(all_edges):
                cursor_next = _make_cursor(graph_id, page + 1, "query_subgraph", {
                    "center_node": center_node,
                    "depth": depth,
                    "direction": direction,
                    "page_size": page_size,
                })

            return {
                "success": True,
                "data": {
                    "summary": {
                        "center_node": center_node,
                        "depth": depth,
                        "total_neighbors": total_nodes - 1,
                        "total_edges": len(all_edges),
                        "showing": f"page {page} (nodes: {len(paged_nodes)}, edges: {len(paged_edges)})",
                    },
                    "nodes": paged_nodes,
                    "edges": paged_edges,
                    "cursor_next": cursor_next,
                },
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    def query_path(
        self,
        graph_id: str,
        source_node: str,
        target_node: str,
        max_depth: int = 5,
        algorithm: str = "shortest",
    ) -> dict:
        """查找两节点间路径（BFS）。

        Args:
            graph_id: 图谱 ID
            source_node: 起始节点
            target_node: 目标节点
            max_depth: 最大深度
            algorithm: "shortest" | "all"

        Returns:
            {"success": true, "data": {"paths": [...]}}
        """
        try:
            conn = self._get_conn(graph_id)
            self._init_schema(conn)
            if not self._graph_exists(conn, graph_id):
                return {"success": False, "error": f"图谱不存在: {graph_id}"}

            # 检查节点存在
            for nid in (source_node, target_node):
                row = conn.execute(
                    "SELECT 1 FROM nodes WHERE graph_id=? AND node_id=?", (graph_id, nid)
                ).fetchone()
                if not row:
                    return {"success": False, "error": f"节点不存在: {nid}"}

            max_depth = min(max(1, int(max_depth or 5)), 10)

            # 构建邻接表
            all_edges = conn.execute(
                "SELECT source_id, target_id, edge_id, relation FROM edges WHERE graph_id=?",
                (graph_id,),
            ).fetchall()

            adj: Dict[str, List[dict]] = {}
            for e in all_edges:
                s, t = e["source_id"], e["target_id"]
                adj.setdefault(s, []).append({"target": t, "edge_id": e["edge_id"], "relation": e["relation"]})

            # BFS
            from collections import deque

            paths = []
            q = deque()
            q.append((source_node, []))

            while q:
                current, path_edges = q.popleft()
                if len(path_edges) > max_depth:
                    continue
                if current == target_node and path_edges:
                    paths.append({
                        "nodes": [source_node] + [e["target"] for e in path_edges],
                        "edges": [e["edge_id"] for e in path_edges],
                        "length": len(path_edges),
                    })
                    if algorithm == "shortest":
                        break
                    continue

                if len(path_edges) >= max_depth:
                    continue

                for neighbor in adj.get(current, []):
                    if neighbor["target"] not in {e["target"] for e in path_edges} | {source_node}:
                        # 防止循环
                        if neighbor["target"] == source_node:
                            continue
                        q.append((neighbor["target"], path_edges + [neighbor]))

            return {
                "success": True,
                "data": {"paths": paths},
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    def query_centrality(
        self,
        graph_id: str,
        metric: str = "degree",
        limit: int = 50,
    ) -> dict:
        """节点中心度计算。

        Args:
            graph_id: 图谱 ID
            metric: "degree" | "betweenness" | "pagerank"
            limit: 返回前 N 个

        Returns:
            {"success": true, "data": {"nodes": [{node_id, centrality}, ...]}}
        """
        try:
            conn = self._get_conn(graph_id)
            self._init_schema(conn)
            if not self._graph_exists(conn, graph_id):
                return {"success": False, "error": f"图谱不存在: {graph_id}"}
            limit = min(max(1, int(limit or 50)), 500)

            if metric == "degree":
                rows = conn.execute("""
                    SELECT n.node_id, n.name, n.label,
                        (SELECT COUNT(*) FROM edges e WHERE e.graph_id=n.graph_id AND (e.source_id=n.node_id OR e.target_id=n.node_id)) AS centrality
                    FROM nodes n WHERE n.graph_id=?
                    ORDER BY centrality DESC LIMIT ?
                """, (graph_id, limit)).fetchall()
                nodes = [{"node_id": r["node_id"], "name": r["name"], "label": r["label"], "centrality": r["centrality"]} for r in rows]

            elif metric == "pagerank":
                # 简易 PageRank（迭代法）
                all_nodes = conn.execute(
                    "SELECT node_id FROM nodes WHERE graph_id=?", (graph_id,)
                ).fetchall()
                node_ids = [r["node_id"] for r in all_nodes]
                n = len(node_ids)
                if n == 0:
                    return {"success": True, "data": {"nodes": []}}

                # 构建邻接
                out_links: Dict[str, List[str]] = {nid: [] for nid in node_ids}
                edges = conn.execute(
                    "SELECT source_id, target_id FROM edges WHERE graph_id=?", (graph_id,)
                ).fetchall()
                for e in edges:
                    out_links[e["source_id"]].append(e["target_id"])

                damping = 0.85
                pr = {nid: 1.0 / n for nid in node_ids}
                for _ in range(30):
                    new_pr = {nid: (1 - damping) / n for nid in node_ids}
                    for nid in node_ids:
                        if out_links[nid]:
                            share = damping * pr[nid] / len(out_links[nid])
                            for target in out_links[nid]:
                                new_pr[target] += share
                        else:
                            for t in node_ids:
                                new_pr[t] += damping * pr[nid] / n
                    pr = new_pr

                sorted_nodes = sorted(pr.items(), key=lambda x: x[1], reverse=True)[:limit]
                nodes = [{"node_id": nid, "centrality": round(val, 6)} for nid, val in sorted_nodes]
            else:
                # betweenness 等复杂算法暂不支持，回退到 degree
                return self.query_centrality(graph_id, "degree", limit)

            return {"success": True, "data": {"nodes": nodes}}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def query_cursor(self, cursor_next: str, page_size: Optional[int] = None) -> dict:
        """游标翻页。

        Args:
            cursor_next: 上次查询返回的游标字符串
            page_size: 覆盖每页数量（可选）

        Returns:
            {"success": true, "data": {"page": N, "preview": [...], "cursor_next": "..."}}
        """
        try:
            payload = _parse_cursor(cursor_next)
            if not payload:
                return {"success": False, "error": "游标已过期，请重新查询"}

            graph_id = payload.get("g", "")
            page = payload.get("p", 1)
            query_type = payload.get("t", "")
            query_params = payload.get("q", {})

            if page_size:
                query_params["page_size"] = page_size
            else:
                page_size = query_params.get("page_size", DEFAULT_PAGE_SIZE)

            if query_type == "query_nodes":
                return self.query_nodes(
                    graph_id=graph_id,
                    label_filter=query_params.get("label_filter"),
                    name_search=query_params.get("name_search"),
                    page=page,
                    page_size=page_size,
                )
            elif query_type == "query_edges":
                return self.query_edges(
                    graph_id=graph_id,
                    source_id=query_params.get("source_id"),
                    target_id=query_params.get("target_id"),
                    relation_filter=query_params.get("relation_filter"),
                    page=page,
                    page_size=page_size,
                )
            elif query_type == "query_subgraph":
                return self.query_subgraph(
                    graph_id=graph_id,
                    center_node=query_params.get("center_node", ""),
                    depth=query_params.get("depth", 1),
                    direction=query_params.get("direction", "both"),
                    page=page,
                    page_size=page_size,
                )
            else:
                return {"success": False, "error": f"未知查询类型: {query_type}"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def query_sql(
        self,
        graph_id: str,
        sql_statement: str,
        params: Optional[list] = None,
    ) -> dict:
        """原生 SQL 查询（高级）。

        Args:
            graph_id: 图谱 ID
            sql_statement: SQL 语句（只读 SELECT）
            params: 参数列表

        Returns:
            {"success": true, "data": {"columns": [...], "rows": [...], "row_count": N}}
        """
        try:
            # 安全检查：只允许 SELECT
            stripped = sql_statement.strip().upper()
            if not stripped.startswith("SELECT"):
                return {"success": False, "error": "仅允许 SELECT 查询"}

            conn = self._get_conn(graph_id)
            self._init_schema(conn)
            if not self._graph_exists(conn, graph_id):
                return {"success": False, "error": f"图谱不存在: {graph_id}"}
            cur = conn.execute(sql_statement, params or [])
            columns = [d[0] for d in cur.description] if cur.description else []
            rows_dict = [dict(row) for row in cur.fetchall()]

            return {
                "success": True,
                "data": {
                    "columns": columns,
                    "rows": rows_dict,
                    "row_count": len(rows_dict),
                },
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ------------------------------------------------------------------
    # 统计与导出
    # ------------------------------------------------------------------

    def get_stats(self, graph_id: str) -> dict:
        """获取图谱统计信息。

        Args:
            graph_id: 图谱 ID

        Returns:
            {"success": true, "data": {"node_count": N, "edge_count": M, "label_distribution": {...}, ...}}
        """
        try:
            conn = self._get_conn(graph_id)
            self._init_schema(conn)
            if not self._graph_exists(conn, graph_id):
                return {"success": False, "error": f"图谱不存在: {graph_id}"}

            node_count = conn.execute(
                "SELECT COUNT(*) FROM nodes WHERE graph_id=?", (graph_id,)
            ).fetchone()[0]
            edge_count = conn.execute(
                "SELECT COUNT(*) FROM edges WHERE graph_id=?", (graph_id,)
            ).fetchone()[0]

            # 标签分布
            label_rows = conn.execute(
                "SELECT label, COUNT(*) as cnt FROM nodes WHERE graph_id=? GROUP BY label ORDER BY cnt DESC",
                (graph_id,),
            ).fetchall()
            label_dist = {r["label"]: r["cnt"] for r in label_rows}

            # 关系分布
            rel_rows = conn.execute(
                "SELECT relation, COUNT(*) as cnt FROM edges WHERE graph_id=? GROUP BY relation ORDER BY cnt DESC",
                (graph_id,),
            ).fetchall()
            rel_dist = {r["relation"]: r["cnt"] for r in rel_rows}

            return {
                "success": True,
                "data": {
                    "node_count": node_count,
                    "edge_count": edge_count,
                    "label_distribution": label_dist,
                    "relation_distribution": rel_dist,
                },
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    def export_graph(
        self,
        graph_id: str,
        fmt: str = "json",
        output_path: Optional[str] = None,
    ) -> dict:
        """导出图谱。

        Args:
            graph_id: 图谱 ID
            fmt: 导出格式 ("json")
            output_path: 输出文件路径

        Returns:
            {"success": true, "data": {"export_path": "...", "size": N}}
        """
        try:
            conn = self._get_conn(graph_id)
            self._init_schema(conn)
            if not self._graph_exists(conn, graph_id):
                return {"success": False, "error": f"图谱不存在: {graph_id}"}

            if fmt != "json":
                return {"success": False, "error": f"暂不支持的导出格式: {fmt}"}

            nodes = conn.execute(
                "SELECT * FROM nodes WHERE graph_id=?", (graph_id,)
            ).fetchall()
            edges = conn.execute(
                "SELECT * FROM edges WHERE graph_id=?", (graph_id,)
            ).fetchall()

            export_data = {
                "graph_id": graph_id,
                "node_count": len(nodes),
                "edge_count": len(edges),
                "nodes": [self._row_to_dict(r) for r in nodes],
                "edges": [self._row_to_dict(r) for r in edges],
            }

            if not output_path:
                export_dir = os.path.join(self._storage_root_for_graph(graph_id), "exports")
                os.makedirs(export_dir, exist_ok=True)
                output_path = os.path.join(export_dir, f"{graph_id}.json")

            os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
            with open(output_path, "w", encoding="utf-8") as f:
                json.dump(export_data, f, ensure_ascii=False, indent=2)

            size = os.path.getsize(output_path)

            return {
                "success": True,
                "data": {
                    "export_path": os.path.abspath(output_path),
                    "size": size,
                    "size_human": f"{size:,} bytes",
                },
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    def import_graph(
        self,
        file_path: str,
        fmt: str = "json",
    ) -> dict:
        """导入已有图谱数据。

        Args:
            file_path: 导入文件路径
            fmt: 文件格式 ("json")

        Returns:
            {"success": true, "data": {"graph_id": "...", "stats": {...}}}
        """
        try:
            if fmt != "json":
                return {"success": False, "error": f"暂不支持的导入格式: {fmt}"}

            if not os.path.exists(file_path):
                return {"success": False, "error": f"文件不存在: {file_path}"}

            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            name = data.get("graph_id", os.path.basename(file_path))
            result = self.create_graph(name, f"Imported from {file_path}")
            if not result["success"]:
                return result

            gid = result["data"]["graph_id"]

            # 批量导入节点
            if data.get("nodes"):
                self.batch_add_nodes(gid, data["nodes"])

            # 批量导入边
            if data.get("edges"):
                self.batch_add_edges(gid, data["edges"])

            stats = self.get_stats(gid)
            return {
                "success": True,
                "data": {
                    "graph_id": gid,
                    "stats": stats.get("data", {}),
                },
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ------------------------------------------------------------------
    # 异步作业
    # ------------------------------------------------------------------

    def start_build(
        self,
        graph_id: str,
        dataset_path: str,
        config: Optional[dict] = None,
    ) -> dict:
        """提交异步建图作业。

        Args:
            graph_id: 图谱 ID
            dataset_path: 数据集路径
            config: 额外配置

        Returns:
            {"success": true, "data": {"job_id": "...", "status": "accepted"}}
        """
        try:
            conn = self._get_conn(graph_id)
            self._init_schema(conn)

            # 检查图谱
            row = conn.execute(
                "SELECT status FROM graphs WHERE graph_id=?", (graph_id,)
            ).fetchone()
            if not row:
                return {"success": False, "error": f"图谱不存在: {graph_id}"}

            if not os.path.exists(dataset_path):
                return {"success": False, "error": f"数据集路径不存在: {dataset_path}"}

            job_id = f"build_{uuid.uuid4().hex[:10]}"
            now = _now()

            conn.execute(
                "INSERT INTO jobs (job_id, graph_id, status, created_at) VALUES (?, ?, 'queued', ?)",
                (job_id, graph_id, now),
            )
            conn.execute(
                "UPDATE graphs SET status='building', updated_at=? WHERE graph_id=?",
                (now, graph_id),
            )
            conn.commit()

            # 启动后台进程
            worker_script = os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "_kg_worker.py"
            )
            db_path = self._db_path_for_graph(graph_id)

            cmd = [
                sys.executable, worker_script,
                "--job-id", job_id,
                "--graph-id", graph_id,
                "--dataset", dataset_path,
                "--db", db_path,
                "--config", json.dumps(config or {}, ensure_ascii=False),
            ]

            try:
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    close_fds=True,
                    creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
                )
            except Exception as exc:
                conn.execute(
                    "UPDATE jobs SET status='failed', error_message=?, completed_at=? WHERE job_id=?",
                    (f"启动后台进程失败: {exc}", _now(), job_id),
                )
                conn.execute(
                    "UPDATE graphs SET status='error', updated_at=? WHERE graph_id=?",
                    (_now(), graph_id),
                )
                conn.commit()
                return {"success": False, "error": f"启动后台进程失败: {exc}"}

            conn.execute(
                "UPDATE jobs SET pid=?, status='running', started_at=? WHERE job_id=?",
                (proc.pid, now, job_id),
            )
            conn.commit()

            return {
                "success": True,
                "data": {
                    "job_id": job_id,
                    "status": "accepted",
                    "pid": proc.pid,
                    "note": "后台建图已启动，用 kg_check_status 查看进度",
                },
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    def check_status(self, job_id: str) -> dict:
        """检查异步作业进度。

        Args:
            job_id: 作业 ID

        Returns:
            {"success": true, "data": {"job_id": "...", "status": "...", "progress": 0.45, ...}}
        """
        try:
            # 从所有已知图谱数据库中查找
            for gid, db_path, _storage_root in self._iter_graph_db_files():
                try:
                    conn = self._connect_db_path(db_path)
                    self._init_schema(conn)
                    row = conn.execute(
                        "SELECT * FROM jobs WHERE job_id=?", (job_id,)
                    ).fetchone()
                    if row:
                        data = dict(row)
                        if (
                            data.get("status") in ("queued", "running")
                            and data.get("pid")
                            and not self._is_process_alive(int(data["pid"]))
                        ):
                            now = _now()
                            data["status"] = "failed"
                            data["error_message"] = "后台建图进程已退出，作业未完成"
                            data["completed_at"] = now
                            conn.execute(
                                "UPDATE jobs SET status=?, error_message=?, completed_at=? WHERE job_id=?",
                                (data["status"], data["error_message"], now, job_id),
                            )
                            conn.execute(
                                "UPDATE graphs SET status='error', updated_at=? WHERE graph_id=?",
                                (now, gid),
                            )
                            conn.commit()
                        if data.get("started_at"):
                            elapsed = (
                                datetime.now()
                                - datetime.strptime(data["started_at"], "%Y-%m-%d %H:%M:%S")
                            ).total_seconds()
                            data["elapsed_seconds"] = int(elapsed)
                            if data.get("progress", 0) > 0:
                                data["eta_seconds"] = int(
                                    elapsed / data["progress"] * (1 - data["progress"])
                                )
                        return {"success": True, "data": data}
                except Exception:
                    continue

            return {"success": False, "error": f"作业不存在: {job_id}"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def list_jobs(
        self,
        graph_id: Optional[str] = None,
        status_filter: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> dict:
        """列出作业。

        Args:
            graph_id: 按图谱过滤（可选）
            status_filter: 按状态过滤
            page: 页码
            page_size: 每页数量

        Returns:
            {"success": true, "data": {"jobs": [...], "total": N}}
        """
        try:
            all_jobs = []
            for gid, db_path, _storage_root in self._iter_graph_db_files():
                if graph_id and gid != graph_id:
                    continue
                try:
                    conn = self._connect_db_path(db_path)
                    self._init_schema(conn)
                    if status_filter:
                        rows = conn.execute(
                            "SELECT * FROM jobs WHERE graph_id=? AND status=? ORDER BY created_at DESC",
                            (gid, status_filter),
                        ).fetchall()
                    else:
                        rows = conn.execute(
                            "SELECT * FROM jobs WHERE graph_id=? ORDER BY created_at DESC",
                            (gid,),
                        ).fetchall()
                    all_jobs.extend([dict(r) for r in rows])
                except Exception:
                    continue

            total = len(all_jobs)
            page = max(1, int(page or 1))
            page_size = min(max(1, int(page_size or 20)), MAX_PAGE_SIZE)
            offset = (page - 1) * page_size

            return {
                "success": True,
                "data": {
                    "jobs": all_jobs[offset : offset + page_size],
                    "total": total,
                    "page": page,
                },
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    def cancel_job(self, job_id: str) -> dict:
        """取消运行中的作业。

        Args:
            job_id: 作业 ID

        Returns:
            {"success": true}
        """
        try:
            for gid, db_path, _storage_root in self._iter_graph_db_files():
                try:
                    conn = self._connect_db_path(db_path)
                    self._init_schema(conn)
                    row = conn.execute(
                        "SELECT * FROM jobs WHERE job_id=?", (job_id,)
                    ).fetchone()
                    if row:
                        pid = row["pid"]
                        status = row["status"]
                        if status in ("running", "queued"):
                            if pid:
                                self._terminate_process(int(pid))
                            now = _now()
                            conn.execute(
                                "UPDATE jobs SET status='cancelled', completed_at=? WHERE job_id=?",
                                (now, job_id),
                            )
                            conn.execute(
                                "UPDATE graphs SET status='idle', updated_at=? WHERE graph_id=?",
                                (now, gid),
                            )
                            conn.commit()
                            status = "cancelled"
                        return {"success": True, "data": {"job_id": job_id, "status": status}}
                except Exception:
                    continue

            return {"success": False, "error": f"作业不存在: {job_id}"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    def _is_process_alive(self, pid: int) -> bool:
        """尽量跨平台判断进程是否仍然存在。"""
        if pid <= 0:
            return False
        try:
            if sys.platform == "win32":
                result = subprocess.run(
                    ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                    capture_output=True,
                    text=True,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                return str(pid) in result.stdout
            os.kill(pid, 0)
            return True
        except OSError:
            return False
        except Exception:
            # 无法可靠判断时保守返回 True，避免误标失败。
            return True

    def _terminate_process(self, pid: int) -> None:
        """尽量跨平台终止后台 worker。"""
        if pid <= 0:
            return
        try:
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/F", "/PID", str(pid)],
                    capture_output=True,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            else:
                os.kill(pid, signal.SIGTERM)
        except Exception:
            pass

    def _sync_counts(self, conn: sqlite3.Connection, graph_id: str) -> None:
        """同步图谱的节点数和边数到 graphs 表。"""
        try:
            node_count = conn.execute(
                "SELECT COUNT(*) FROM nodes WHERE graph_id=?", (graph_id,)
            ).fetchone()[0]
            edge_count = conn.execute(
                "SELECT COUNT(*) FROM edges WHERE graph_id=?", (graph_id,)
            ).fetchone()[0]
            conn.execute(
                "UPDATE graphs SET node_count=?, edge_count=?, updated_at=? WHERE graph_id=?",
                (node_count, edge_count, _now(), graph_id),
            )
            conn.commit()
        except Exception:
            pass


# =============================================================================
# 工具管理器（框架自动发现）
# =============================================================================

class KgToolManager:
    """知识图谱工具管理器 — 由 Xenon 框架自动发现并注册为工具。

    每个公开方法自动成为一个工具，方法名 = 工具名后缀。
    工具全名格式：knowledge_graph_handler_Kg_{方法名}
    """

    def __init__(self):
        self._manager = KnowledgeGraphManager()

    # ---- 图谱 CRUD ----

    def kg_create_graph(self, name: str, description: str = "", config: Optional[dict] = None) -> dict:
        """创建新知识图谱。

        Args:
            name: 图谱名称
            description: 描述（可选）
            config: 自定义配置字典，如 {"storage_path": "..."}

        Returns:
            {"success": true/false, "data": {"graph_id": "...", "status": "idle"}, "error": "..."}
        """
        return self._manager.create_graph(name, description, config)

    def kg_list_graphs(self, page: int = 1, page_size: int = 20) -> dict:
        """列出所有知识图谱。

        Args:
            page: 页码（从1开始）
            page_size: 每页数量（默认20）

        Returns:
            {"success": true, "data": {"graphs": [...], "total": N, "page": 1}}
        """
        return self._manager.list_graphs(page, page_size)

    def kg_delete_graph(self, graph_id: str) -> dict:
        """删除整个知识图谱（含数据库文件，不可恢复！）。

        Args:
            graph_id: 图谱 ID

        Returns:
            {"success": true/false, "data": {"graph_id": "..."}}
        """
        return self._manager.delete_graph(graph_id)

    # ---- 节点/边 CRUD ----

    def kg_add_node(self, graph_id: str, node_id: str, label: str, name: str, properties: Optional[dict] = None) -> dict:
        """添加单个节点。

        Args:
            graph_id: 图谱 ID
            node_id: 节点唯一标识
            label: 节点类型标签，如 "Person", "Concept", "File"
            name: 节点名称/标识
            properties: 属性字典（JSON），如 {"key": "value"}

        Returns:
            {"success": true/false, "data": {"node_id": "..."}}
        """
        return self._manager.add_node(graph_id, node_id, label, name, properties)

    def kg_add_edge(self, graph_id: str, edge_id: str, source_id: str, target_id: str, relation: str, weight: float = 1.0, properties: Optional[dict] = None) -> dict:
        """添加单条关系边。

        Args:
            graph_id: 图谱 ID
            edge_id: 边唯一标识
            source_id: 源节点 ID
            target_id: 目标节点 ID
            relation: 关系类型，如 "works_at", "cites", "contains"
            weight: 边权重（默认1.0）
            properties: 属性字典

        Returns:
            {"success": true/false, "data": {"edge_id": "..."}}
        """
        return self._manager.add_edge(graph_id, edge_id, source_id, target_id, relation, weight, properties)

    def kg_batch_add_nodes(self, graph_id: str, nodes: list) -> dict:
        """批量添加节点。

        Args:
            graph_id: 图谱 ID
            nodes: 节点列表，每项为 {"node_id": ..., "label": ..., "name": ..., "properties": {...}}

        Returns:
            {"success": true/false, "data": {"count": N}}
        """
        return self._manager.batch_add_nodes(graph_id, nodes)

    def kg_batch_add_edges(self, graph_id: str, edges: list) -> dict:
        """批量添加边。

        Args:
            graph_id: 图谱 ID
            edges: 边列表，每项为 {"edge_id": ..., "source_id": ..., "target_id": ..., "relation": ..., "weight": ..., "properties": {...}}

        Returns:
            {"success": true/false, "data": {"count": N}}
        """
        return self._manager.batch_add_edges(graph_id, edges)

    def kg_remove_node(self, graph_id: str, node_id: str) -> dict:
        """删除节点（连带删除所有相关边）。

        Args:
            graph_id: 图谱 ID
            node_id: 节点 ID

        Returns:
            {"success": true/false, "data": {"node_id": "...", "deleted_edges": N}}
        """
        return self._manager.remove_node(graph_id, node_id)

    def kg_remove_edge(self, graph_id: str, edge_id: str) -> dict:
        """删除单条边。

        Args:
            graph_id: 图谱 ID
            edge_id: 边 ID

        Returns:
            {"success": true/false, "data": {"edge_id": "..."}}
        """
        return self._manager.remove_edge(graph_id, edge_id)

    # ---- 查询 ----

    def kg_get_node(self, graph_id: str, node_id: str, expand_relations: bool = False, relation_limit: int = 50) -> dict:
        """获取节点详情，可选择展开关联边。

        Args:
            graph_id: 图谱 ID
            node_id: 节点 ID
            expand_relations: 是否展开关联边
            relation_limit: 关联边上限

        Returns:
            {"success": true, "data": {"node": {...}, "relations": [...]}}
        """
        return self._manager.get_node(graph_id, node_id, expand_relations, relation_limit)

    def kg_query_nodes(self, graph_id: str, label_filter: Optional[str] = None, name_search: Optional[str] = None, properties_filter: Optional[str] = None, page: int = 1, page_size: int = 50) -> dict:
        """搜索节点（带游标分页）。

        Args:
            graph_id: 图谱 ID
            label_filter: 按标签过滤，如 "Concept"
            name_search: 按名称模糊搜索（LIKE）
            properties_filter: 按属性过滤（暂未实现）
            page: 页码
            page_size: 每页数量

        Returns:
            {"success": true, "data": {"summary": {...}, "preview": [...], "cursor_next": "..."}}
        """
        return self._manager.query_nodes(graph_id, label_filter, name_search, properties_filter, page, page_size)

    def kg_query_edges(self, graph_id: str, source_id: Optional[str] = None, target_id: Optional[str] = None, relation_filter: Optional[str] = None, page: int = 1, page_size: int = 50) -> dict:
        """搜索边（带游标分页）。

        Args:
            graph_id: 图谱 ID
            source_id: 按源节点过滤
            target_id: 按目标节点过滤
            relation_filter: 按关系类型过滤
            page: 页码
            page_size: 每页数量

        Returns:
            {"success": true, "data": {"summary": {...}, "preview": [...], "cursor_next": "..."}}
        """
        return self._manager.query_edges(graph_id, source_id, target_id, relation_filter, page, page_size)

    def kg_query_subgraph(self, graph_id: str, center_node: str, depth: int = 1, direction: str = "both", page: int = 1, page_size: int = 50) -> dict:
        """获取某节点周围的子图（BFS 邻居展开）。

        Args:
            graph_id: 图谱 ID
            center_node: 中心节点 ID
            depth: 展开深度 (1-3)
            direction: 方向 "both" | "out" | "in"
            page: 页码
            page_size: 每页数量

        Returns:
            {"success": true, "data": {"summary": {...}, "nodes": [...], "edges": [...], "cursor_next": "..."}}
        """
        return self._manager.query_subgraph(graph_id, center_node, depth, direction, page, page_size)

    def kg_query_path(self, graph_id: str, source_node: str, target_node: str, max_depth: int = 5, algorithm: str = "shortest") -> dict:
        """查找两节点间路径。

        Args:
            graph_id: 图谱 ID
            source_node: 起始节点
            target_node: 目标节点
            max_depth: 最大深度
            algorithm: "shortest" | "all"

        Returns:
            {"success": true, "data": {"paths": [...]}}
        """
        return self._manager.query_path(graph_id, source_node, target_node, max_depth, algorithm)

    def kg_query_centrality(self, graph_id: str, metric: str = "degree", limit: int = 50) -> dict:
        """节点中心度排序。

        Args:
            graph_id: 图谱 ID
            metric: "degree" | "pagerank"
            limit: 返回前N个

        Returns:
            {"success": true, "data": {"nodes": [{"node_id":..., "centrality":...}]}}
        """
        return self._manager.query_centrality(graph_id, metric, limit)

    def kg_query_cursor(self, cursor_next: str, page_size: Optional[int] = None) -> dict:
        """使用游标翻页。

        Args:
            cursor_next: 上次查询返回的游标字符串
            page_size: 覆盖每页数量

        Returns:
            {"success": true, "data": {"page": N, "preview": [...], "cursor_next": "..."}}
        """
        return self._manager.query_cursor(cursor_next, page_size)

    def kg_query_sql(self, graph_id: str, sql_statement: str, params: Optional[list] = None) -> dict:
        """原生 SQL 查询（高级，仅允许 SELECT）。

        Args:
            graph_id: 图谱 ID
            sql_statement: SELECT SQL 语句
            params: 参数列表

        Returns:
            {"success": true, "data": {"columns": [...], "rows": [...], "row_count": N}}
        """
        return self._manager.query_sql(graph_id, sql_statement, params)

    # ---- 统计与导出 ----

    def kg_get_stats(self, graph_id: str) -> dict:
        """获取图谱统计信息。

        Args:
            graph_id: 图谱 ID

        Returns:
            {"success": true, "data": {"node_count": N, "edge_count": M, "label_distribution": {...}, "relation_distribution": {...}}}
        """
        return self._manager.get_stats(graph_id)

    def kg_export_graph(self, graph_id: str, fmt: str = "json", output_path: Optional[str] = None) -> dict:
        """导出图谱为文件。

        Args:
            graph_id: 图谱 ID
            fmt: 格式（目前支持 "json"）
            output_path: 输出路径（可选，默认在 Memory/kg/exports/）

        Returns:
            {"success": true, "data": {"export_path": "...", "size": N}}
        """
        return self._manager.export_graph(graph_id, fmt, output_path)

    def kg_import_graph(self, file_path: str, fmt: str = "json") -> dict:
        """从文件导入图谱。

        Args:
            file_path: 文件路径
            fmt: 格式（目前支持 "json"）

        Returns:
            {"success": true, "data": {"graph_id": "...", "stats": {...}}}
        """
        return self._manager.import_graph(file_path, fmt)

    # ---- 异步建图 ----

    def kg_start_build(self, graph_id: str, dataset_path: str, config: Optional[dict] = None) -> dict:
        """启动异步建图作业（后台进程）。

        Args:
            graph_id: 图谱 ID
            dataset_path: 数据集目录路径
            config: 额外配置

        Returns:
            {"success": true, "data": {"job_id": "...", "status": "accepted", "note": "..."}}
        """
        return self._manager.start_build(graph_id, dataset_path, config)

    def kg_check_status(self, job_id: str) -> dict:
        """检查异步建图作业进度。

        Args:
            job_id: 作业 ID

        Returns:
            {"success": true, "data": {"job_id": "...", "status": "...", "progress": 0.45, ...}}
        """
        return self._manager.check_status(job_id)

    def kg_list_jobs(self, graph_id: Optional[str] = None, status_filter: Optional[str] = None, page: int = 1, page_size: int = 20) -> dict:
        """列出所有建图作业。

        Args:
            graph_id: 按图谱过滤
            status_filter: 按状态过滤（queued/running/completed/failed/cancelled）
            page: 页码
            page_size: 每页数量

        Returns:
            {"success": true, "data": {"jobs": [...], "total": N}}
        """
        return self._manager.list_jobs(graph_id, status_filter, page, page_size)

    def kg_cancel_job(self, job_id: str) -> dict:
        """取消正在运行的建图作业。

        Args:
            job_id: 作业 ID

        Returns:
            {"success": true}
        """
        return self._manager.cancel_job(job_id)
