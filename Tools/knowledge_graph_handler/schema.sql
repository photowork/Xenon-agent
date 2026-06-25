-- ============================================
-- knowledge_graph_handler — SQLite Schema 参考
-- ============================================
-- 以下 SQL 由主模块自动执行，此文件仅作参考查阅
-- ============================================

-- 1. 图项目表（一个知识图谱实例）
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

-- 2. 节点表
CREATE TABLE IF NOT EXISTS nodes (
    node_id     TEXT PRIMARY KEY,
    graph_id    TEXT NOT NULL,
    label       TEXT NOT NULL,
    name        TEXT NOT NULL,
    properties  TEXT DEFAULT '{}',
    created_at  TEXT DEFAULT (datetime('now','localtime')),
    FOREIGN KEY (graph_id) REFERENCES graphs(graph_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_nodes_graph   ON nodes(graph_id);
CREATE INDEX IF NOT EXISTS idx_nodes_label   ON nodes(label);
CREATE INDEX IF NOT EXISTS idx_nodes_name    ON nodes(name);

-- 3. 关系边表
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

-- 4. 异步作业表
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

-- 5. 文件索引表
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

-- SQLite 性能配置
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA cache_size=-8000;
PRAGMA temp_store=MEMORY;
PRAGMA mmap_size=268435456;
PRAGMA foreign_keys=ON;
