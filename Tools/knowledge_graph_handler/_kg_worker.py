#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
知识图谱异步建图工作进程 (_kg_worker.py)

独立 subprocess 运行，扫描文件、抽取实体关系、写入 SQLite、更新作业状态。

用法:
    python _kg_worker.py --job-id JOB --graph-id GRAPH --dataset PATH --db DB_PATH [--config CONFIG_JSON]

关键设计：
    - 每处理完一批文件更新 progress
    - 所有中间结果直接写 SQLite
    - 进程崩溃时 main handler 检测 status 未完成 → 标记 failed
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
import traceback
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple


# =============================================================================
# 常量
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

SQLITE_PRAGMAS = [
    "PRAGMA journal_mode=WAL;",
    "PRAGMA synchronous=NORMAL;",
    "PRAGMA cache_size=-32000;",
    "PRAGMA temp_store=MEMORY;",
    "PRAGMA mmap_size=268435456;",
    "PRAGMA foreign_keys=ON;",
]

# 支持扫描的文件扩展名
TEXT_EXTENSIONS = {
    ".txt", ".md", ".py", ".js", ".ts", ".jsx", ".tsx", ".html", ".htm",
    ".css", ".scss", ".json", ".xml", ".yaml", ".yml", ".toml", ".ini",
    ".cfg", ".conf", ".log", ".csv", ".tsv", ".sql", ".r", ".java",
    ".c", ".cpp", ".h", ".hpp", ".rs", ".go", ".rb", ".php", ".swift",
    ".kt", ".scala", ".lua", ".sh", ".bash", ".ps1", ".bat",
}

IGNORE_DIRS = {
    "__pycache__", "node_modules", ".git", ".svn", ".hg",
    "venv", ".venv", "env", ".env", "dist", "build",
    ".next", ".nuxt", "target", "obj", "bin",
}


# =============================================================================
# 实体抽取引擎（基于正则 + 简单 NLP）
# =============================================================================

def extract_entities_from_text(text: str, file_path: str) -> Tuple[List[dict], List[dict]]:
    """从文本中抽取实体和关系。

    返回:
        (nodes: [{node_id, label, name, properties}],
         edges: [{edge_id, source_id, target_id, relation, weight, properties}])
    """
    nodes: List[dict] = []
    edges: List[dict] = []
    seen_entities: Set[str] = set()

    def make_node_id(label: str, name: str) -> str:
        h = hashlib.md5(f"{label}:{name}".encode("utf-8")).hexdigest()[:16]
        return f"ent_{h}"

    def add_entity(label: str, name: str, props: Optional[dict] = None) -> Optional[str]:
        key = f"{label}:{name}"
        if key in seen_entities:
            return make_node_id(label, name)
        seen_entities.add(key)
        nid = make_node_id(label, name)
        nodes.append({
            "node_id": nid,
            "label": label,
            "name": name,
            "properties": (props or {}) | {"source_file": file_path},
        })
        return nid

    def add_edge_(src_id: str, tgt_id: str, relation: str, weight: float = 1.0):
        edge_key = f"{src_id}:{tgt_id}:{relation}"
        eid = f"rel_{hashlib.md5(edge_key.encode()).hexdigest()[:12]}"
        edges.append({
            "edge_id": eid,
            "source_id": src_id,
            "target_id": tgt_id,
            "relation": relation,
            "weight": weight,
            "properties": {"source_file": file_path},
        })

    # --- 抽取规则 ---

    # 1. 文件引用（路径、文件名）
    file_patterns = [
        (r'([\w/\-\\]+\.(?:py|md|js|ts|json|yaml|yml|txt|html|css|java|cpp|h|rs|go))', "File"),
    ]
    for pat, label in file_patterns:
        for m in re.finditer(pat, text):
            fname = m.group(1).strip()
            if len(fname) > 2 and len(fname) < 200:
                add_entity(label, fname)

    # 2. URL
    for m in re.finditer(r'https?://[^\s<>"{}|\\^`\[\]]+', text):
        url = m.group(0).rstrip(".,;:!?)")
        if 10 < len(url) < 500:
            add_entity("URL", url)

    # 3. 模块/库引用（import 语句、require 等）
    for m in re.finditer(r'(?:from|import)\s+([\w.]+)', text):
        add_entity("Module", m.group(1))

    # 4. Python 类名、函数名
    for m in re.finditer(r'(?:class|def)\s+(\w+)', text):
        add_entity("Function" if "def" in m.group(0) else "Class", m.group(1))

    # 5. 中文专有名词（连续 2-6 个汉字的人名/概念）
    for m in re.finditer(r'[\u4e00-\u9fff]{2,6}', text):
        word = m.group(0)
        if len(word) >= 2:
            add_entity("Concept", word)

    # 6. 特殊标记（#xxx, @xxx, `xxx`）
    for m in re.finditer(r'#(\w+)', text):
        add_entity("Tag", m.group(1))
    for m in re.finditer(r'@(\w+)', text):
        add_entity("Mention", m.group(1))

    # 7. 英文单词（大写开头的可能是专有名词，长度 3-30）
    for m in re.finditer(r'\b[A-Z][a-zA-Z]{2,30}\b', text):
        word = m.group(0)
        if word not in ("The", "This", "That", "These", "Those", "There",
                        "From", "With", "When", "Where", "Which", "While",
                        "They", "Their", "Then", "Than", "Also", "Such",
                        "Each", "Every", "Other", "After", "Before"):
            add_entity("Entity", word)

    # --- 关系抽取 ---

    # 关系1：文件包含模块/类/函数
    file_entities = [n for n in nodes if n["label"] == "File"]
    module_entities = [n for n in nodes if n["label"] in ("Module", "Class", "Function")]
    for f in file_entities:
        for m in module_entities:
            if m["name"] in text or os.path.basename(f["name"]) in text:
                add_edge_(f["node_id"], m["node_id"], "contains", 0.8)

    # 关系2：模块导入关系
    imports = re.findall(r'(?:from|import)\s+([\w.]+)', text)
    file_nid = add_entity("File", file_path)
    if file_nid:
        for imp in imports:
            imp_nid = add_entity("Module", imp)
            if imp_nid and file_nid != imp_nid:
                add_edge_(file_nid, imp_nid, "imports", 0.9)

    return nodes, edges


# =============================================================================
# 数据库操作
# =============================================================================

def init_db(db_path: str) -> sqlite3.Connection:
    """初始化数据库连接和 schema。"""
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    for pragma in SQLITE_PRAGMAS:
        try:
            conn.execute(pragma)
        except Exception:
            pass
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    return conn


def update_job_status(conn: sqlite3.Connection, job_id: str, **kwargs):
    """更新作业状态。"""
    updates = ", ".join(f"{k}=?" for k in kwargs)
    values = list(kwargs.values()) + [job_id]
    conn.execute(f"UPDATE jobs SET {updates} WHERE job_id=?", values)
    conn.commit()


def update_graph_counts(conn: sqlite3.Connection, graph_id: str):
    """更新图谱统计。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    node_count = conn.execute(
        "SELECT COUNT(*) FROM nodes WHERE graph_id=?", (graph_id,)
    ).fetchone()[0]
    edge_count = conn.execute(
        "SELECT COUNT(*) FROM edges WHERE graph_id=?", (graph_id,)
    ).fetchone()[0]
    conn.execute(
        "UPDATE graphs SET node_count=?, edge_count=?, updated_at=? WHERE graph_id=?",
        (node_count, edge_count, now, graph_id),
    )
    conn.commit()


# =============================================================================
# 主流程
# =============================================================================

def run_worker(args: argparse.Namespace):
    """独立的建图工作进程入口。

    处理流程:
        1. 扫描 dataset 目录下所有文件
        2. 逐个文件读取、抽取实体关系
        3. 批量写入 SQLite
        4. 更新作业进度
        5. 完成后更新图谱状态
    """
    job_id = args.job_id
    graph_id = args.graph_id
    dataset_path = args.dataset
    db_path = args.db
    config = json.loads(args.config) if args.config else {}

    batch_size = config.get("batch_size", 100)
    max_file_size_mb = config.get("max_file_size_mb", 10)
    max_file_size = max_file_size_mb * 1024 * 1024

    print(f"[kg_worker] 启动: job={job_id}, graph={graph_id}, dataset={dataset_path}", flush=True)

    conn = init_db(db_path)

    try:
        # 标记作业开始
        update_job_status(
            conn, job_id,
            status="running",
            phase="scanning",
            started_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            pid=os.getpid(),
        )

        # ---- Phase 1: 扫描文件 ----
        all_files: List[str] = []
        for root, dirs, filenames in os.walk(dataset_path):
            # 跳过忽略目录
            dirs[:] = [d for d in dirs if d not in IGNORE_DIRS]
            for fname in filenames:
                ext = os.path.splitext(fname)[1].lower()
                if ext in TEXT_EXTENSIONS:
                    fp = os.path.join(root, fname)
                    try:
                        size = os.path.getsize(fp)
                        if size <= max_file_size:
                            all_files.append(fp)
                        else:
                            # 大文件跳过但记录
                            fid = f"f_{hashlib.md5(fp.encode()).hexdigest()[:12]}"
                            conn.execute(
                                "INSERT OR IGNORE INTO file_index (file_id, graph_id, file_path, file_size, status) VALUES (?,?,?,?,?)",
                                (fid, graph_id, fp, size, "skipped"),
                            )
                    except OSError:
                        pass

        total_files = len(all_files)
        print(f"[kg_worker] 扫描完成: {total_files} 个文件待处理", flush=True)

        update_job_status(
            conn, job_id,
            phase="scanning_complete",
            total_files=total_files,
            progress=0.05 if total_files > 0 else 1.0,
        )

        if total_files == 0:
            update_job_status(
                conn, job_id,
                status="completed",
                phase="done",
                progress=1.0,
                completed_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            )
            update_graph_counts(conn, graph_id)
            conn.execute(
                "UPDATE graphs SET status='ready', updated_at=? WHERE graph_id=?",
                (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), graph_id),
            )
            conn.commit()
            conn.close()
            return

        # ---- Phase 2: 逐文件处理 ----
        all_nodes: List[dict] = []
        all_edges: List[dict] = []
        processed = 0

        # 批量写入间隔
        commit_interval = max(1, total_files // 20)  # 至少每5%提交一次

        for idx, file_path in enumerate(all_files):
            try:
                # 读取文件
                try:
                    with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                        text = f.read()
                except Exception:
                    continue

                if not text.strip():
                    # 记录空文件
                    fid = f"f_{hashlib.md5(file_path.encode()).hexdigest()[:12]}"
                    conn.execute(
                        "INSERT OR IGNORE INTO file_index (file_id, graph_id, file_path, file_size, status) VALUES (?,?,?,?,?)",
                        (fid, graph_id, file_path, 0, "processed"),
                    )
                    processed += 1
                    continue

                # 分块处理大文本（简单策略：取前 100KB）
                if len(text) > 100_000:
                    text = text[:100_000]

                # 抽取实体和关系
                nodes, edges = extract_entities_from_text(text, file_path)

                all_nodes.extend(nodes)
                all_edges.extend(edges)

                # 记录文件索引
                fid = f"f_{hashlib.md5(file_path.encode()).hexdigest()[:12]}"
                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                conn.execute(
                    "INSERT OR IGNORE INTO file_index (file_id, graph_id, file_path, file_size, status, processed_at) VALUES (?,?,?,?,?,?)",
                    (fid, graph_id, file_path, len(text), "processed", now),
                )

                processed += 1

                # 定期提交进度
                if processed % commit_interval == 0 or processed == total_files:
                    # 批量写入节点（去重）
                    _batch_insert_nodes(conn, graph_id, all_nodes)
                    _batch_insert_edges(conn, graph_id, all_edges)
                    all_nodes = []
                    all_edges = []

                    progress = 0.05 + 0.85 * (processed / total_files)
                    update_job_status(
                        conn, job_id,
                        phase="entity_extraction",
                        progress=progress,
                        processed_files=processed,
                    )

                    print(f"[kg_worker] 进度: {processed}/{total_files} ({progress*100:.1f}%)", flush=True)

            except Exception as e:
                print(f"[kg_worker] 处理文件出错 {file_path}: {e}", flush=True)
                fid = f"f_{hashlib.md5(file_path.encode()).hexdigest()[:12]}"
                conn.execute(
                    "INSERT OR IGNORE INTO file_index (file_id, graph_id, file_path, file_size, status) VALUES (?,?,?,?,?)",
                    (fid, graph_id, file_path, 0, "error"),
                )

        # ---- Phase 3: 最终提交 ----
        if all_nodes or all_edges:
            _batch_insert_nodes(conn, graph_id, all_nodes)
            _batch_insert_edges(conn, graph_id, all_edges)

        # 更新统计
        update_graph_counts(conn, graph_id)

        # 标记完成
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        update_job_status(
            conn, job_id,
            status="completed",
            phase="done",
            progress=1.0,
            processed_files=processed,
            completed_at=now,
        )
        conn.execute(
            "UPDATE graphs SET status='ready', updated_at=? WHERE graph_id=?",
            (now, graph_id),
        )
        conn.commit()

        print(f"[kg_worker] 完成: {processed} 文件处理完毕", flush=True)

    except Exception as e:
        error_msg = f"{e}\n{traceback.format_exc()}"
        print(f"[kg_worker] 致命错误: {error_msg}", flush=True)
        try:
            update_job_status(
                conn, job_id,
                status="failed",
                error_message=error_msg[:1000],
                completed_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            )
            conn.execute(
                "UPDATE graphs SET status='error', updated_at=? WHERE graph_id=?",
                (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), graph_id),
            )
            conn.commit()
        except Exception:
            pass
    finally:
        conn.close()


def _batch_insert_nodes(conn: sqlite3.Connection, graph_id: str, nodes: List[dict]):
    """批量写入节点（去重）。"""
    if not nodes:
        return
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with conn:
        for n in nodes:
            nid = n.get("node_id", "")
            if not nid:
                continue
            props = json.dumps(n.get("properties", {}), ensure_ascii=False)
            conn.execute(
                "INSERT OR IGNORE INTO nodes (node_id, graph_id, label, name, properties, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (nid, graph_id, n.get("label", "Entity"), n.get("name", ""), props, now),
            )


def _batch_insert_edges(conn: sqlite3.Connection, graph_id: str, edges: List[dict]):
    """批量写入边（去重）。"""
    if not edges:
        return
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with conn:
        for e in edges:
            eid = e.get("edge_id", "")
            if not eid:
                continue
            props = json.dumps(e.get("properties", {}), ensure_ascii=False)
            conn.execute(
                "INSERT OR IGNORE INTO edges (edge_id, graph_id, source_id, target_id, relation, weight, properties, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    eid, graph_id,
                    e.get("source_id", ""), e.get("target_id", ""),
                    e.get("relation", "related_to"), float(e.get("weight", 1.0)),
                    props, now,
                ),
            )


# =============================================================================
# 入口
# =============================================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="知识图谱建图工作进程")
    parser.add_argument("--job-id", required=True, help="作业 ID")
    parser.add_argument("--graph-id", required=True, help="图谱 ID")
    parser.add_argument("--dataset", required=True, help="数据集目录路径")
    parser.add_argument("--db", required=True, help="SQLite 数据库路径")
    parser.add_argument("--config", default="{}", help="JSON 配置")
    args = parser.parse_args()

    run_worker(args)
