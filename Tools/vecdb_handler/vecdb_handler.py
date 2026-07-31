#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Vector Database Tool — 原生向量数据库构建与查询

把文本文件（.txt）分片 → 向量化 → 存入 ChromaDB，
然后直接用语义搜索查询，无需 HTTP 服务、无需端口。

用法（通过 Xenon 框架调用）：
    vecdb_build(book_folder="...", ...)  建库
    vecdb_query(query="...", ...)         查询
"""

from __future__ import annotations

import hashlib
import json
import os
import pickle
import re
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional


# =============================================================================
# 默认配置
# =============================================================================

DEFAULT_CHUNK_SIZE = 600        # 单条切片字数
DEFAULT_CHUNK_OVERLAP = 50      # 切片重叠字数
DEFAULT_COLLECTION = "documents"

# 工具自身的存储根目录（用户可覆盖）
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_EMBED_MODEL = os.path.join(PROJECT_ROOT, "models", "bge-small-zh-v1.5")
TOOL_STORAGE = os.path.join(PROJECT_ROOT, "KnowledgeBase")


# =============================================================================
# 文本处理工具
# =============================================================================

def _clean_text(text: str) -> str:
    """清洗文本：去多余空白、页码、水印、乱码标记"""
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"第\s*\d+\s*[页頁Pp]", "", text)
    text = re.sub(r"页码.*?[\n。；]", "", text)
    text = re.sub(r"扫描[版水].*?[\n]", "", text)
    text = re.sub(r"水印.*?[\n]", "", text)
    text = re.sub(r"[─━│┃┄┅┆┇┈┉┊┋┌┍┎┏]", "", text)
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
    text = text.replace("\u3000", " ")
    return text.strip()


def _split_text(content: str, chunk_size: int = DEFAULT_CHUNK_SIZE,
                overlap: int = DEFAULT_CHUNK_OVERLAP) -> list:
    """将长文切分为 chunk_size 字左右的片段，带 overlap 字重叠"""
    if len(content) <= chunk_size:
        return [content]

    chunks = []
    start = 0
    while start < len(content):
        end = start + chunk_size
        if end >= len(content):
            chunks.append(content[start:])
            break
        boundary = content.rfind("。", start + chunk_size - 50, end + 50)
        if boundary != -1 and boundary > start:
            end = boundary + 1
        else:
            boundary = content.rfind("\n", start + chunk_size - 30, end + 30)
            if boundary != -1 and boundary > start:
                end = boundary + 1
        chunks.append(content[start:end])
        start = end - overlap if end - overlap > start else end
    return chunks


# =============================================================================
# 核心：向量数据库管理
# =============================================================================

class _VecDBEngine:
    """向量数据库引擎 — 封装 ChromaDB 的建库与查询（内部类，不暴露为工具）"""

    def __init__(self, db_path: Optional[str] = None, embed_model: str = DEFAULT_EMBED_MODEL):
        self.db_path = db_path or os.path.join(TOOL_STORAGE, "default")
        self.embed_model_name = embed_model
        self._model = None
        self._client = None
        self._device = "cpu"
        self._gpu_diag = None  # GPU 检测诊断信息

    # ---- 依赖注入 ----

    def _ensure_packages(self, packages: list) -> None:
        """确保 Python 包已安装，未安装则自动安装"""
        for pkg in packages:
            try:
                __import__(pkg.replace("-", "_").split("[")[0])
            except ImportError:
                import subprocess as _sp
                import sys as _sys
                mirror = os.environ.get("HF_ENDPOINT", "")
                env = os.environ.copy()
                if "sentence" in pkg and not mirror:
                    env["HF_ENDPOINT"] = "https://hf-mirror.com"
                _sp.check_call(
                    [_sys.executable, "-m", "pip", "install", pkg, "-q"],
                    env=env, stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
                )

    def _get_model(self):
        """懒加载嵌入模型（GPU优先  →  CPU兜底）"""
        if self._model is None:
            self._ensure_packages(["sentence-transformers"])
            from sentence_transformers import SentenceTransformer
            # 设置国内镜像（如果没设的话）
            if "HF_ENDPOINT" not in os.environ:
                os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"
            # 动态解析模型路径：如果默认路径不存在，尝试 PROJECT_ROOT/models/
            model_path = self.embed_model_name
            if not os.path.isdir(model_path):
                alt_path = os.path.join(
                    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                    "models", "bge-small-zh-v1.5"
                )
                if os.path.isdir(alt_path):
                    model_path = alt_path
                    self.embed_model_name = alt_path

            # ── 设备选择：GPU优先，CPU兜底 ──
            device = "cpu"
            gpu_reason = "not_attempted"
            try:
                import torch
                gpu_reason = f"torch_imported_cuda={torch.cuda.is_available()}"
                if torch.cuda.is_available():
                    device = "cuda"
                    gpu_reason = "cuda_available"
            except ImportError as e:
                gpu_reason = f"torch_import_failed: {e}"
                pass

            try:
                print(f"[vecdb_diag] Attempting {model_path} on {device}")
                self._model = SentenceTransformer(model_path, device=device)
                self._device = device
                self._gpu_diag = f"gpu_reason={gpu_reason}, final_device={device}"
                print(f"[vecdb_diag] SUCCESS: model loaded on {device}")
            except Exception as e:
                print(f"[vecdb_diag] FAILED on {device}: {e}")
                if device == "cuda":
                    # GPU 加载失败（OOM / 驱动问题等），回退 CPU
                    print(f"[vecdb_diag] Falling back to CPU. GPU reason: {gpu_reason}")
                    device = "cpu"
                    self._model = SentenceTransformer(model_path, device=device)
                    self._device = device
                    self._gpu_diag = f"gpu_reason={gpu_reason}, cuda_failed={e}, final_device=cpu"
                    print(f"[vecdb_diag] SUCCESS: model loaded on CPU (fallback)")
                else:
                    self._gpu_diag = f"gpu_reason={gpu_reason}, error={e}"
                    raise
        return self._model

    def _get_chromadb(self):
        """懒加载 chromadb 并获取 collection"""
        if self._client is None:
            self._ensure_packages(["chromadb"])
            import chromadb
            os.makedirs(self.db_path, exist_ok=True)
            self._client = chromadb.PersistentClient(path=self.db_path)
        return self._client

    def _get_collection(self, name: str = DEFAULT_COLLECTION):
        client = self._get_chromadb()
        return client.get_or_create_collection(name=name)

    def _delete_collection(self, name: str = DEFAULT_COLLECTION):
        """删除集合并重建（用于重新建库）"""
        client = self._get_chromadb()
        try:
            client.delete_collection(name)
        except Exception:
            pass
        return client.get_or_create_collection(name=name)

    # ---- 建库 ----

    def build(
        self,
        book_folder: str,
        collection_name: str = DEFAULT_COLLECTION,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
        use_classification: bool = False,
        custom_categories: Optional[List[Dict]] = None,
        extensions: Optional[List[str]] = None,
        rebuild: bool = True,
    ) -> Dict[str, Any]:
        """
        从文本文件构建向量数据库。

        Args:
            book_folder: 存放文本文件的目录路径
            collection_name: ChromaDB 集合名称
            chunk_size: 文本分片大小（字符数）
            chunk_overlap: 分片重叠字符数
            use_classification: 是否启用文本分类（需配合 custom_categories 使用）
            custom_categories: 自定义分类体系（覆盖默认）
            extensions: 要扫描的文件扩展名，默认 [".txt", ".md"]
            rebuild: 是否重新构建（清空旧数据）

        Returns:
            建库统计信息
        """
        book_folder = os.path.abspath(book_folder)
        if not os.path.isdir(book_folder):
            return {"success": False, "error": f"目录不存在: {book_folder}"}

        if extensions is None:
            extensions = [".txt", ".md"]

        # 准备分类体系
        categories = custom_categories if use_classification else []

        def _classify(text: str) -> dict:
            """使用当前分类体系对文本分类"""
            if not categories or not use_classification:
                return {"category_id": "00", "category_name": "未分类", "score": 0}
            matched = {}
            for cat in categories:
                score = sum(1 for kw in cat.get("keywords", []) if kw in text)
                if score > 0:
                    matched[cat["id"]] = {
                        "category_id": cat["id"],
                        "category_name": cat["name"],
                        "score": score,
                    }
            if not matched:
                return {"category_id": "00", "category_name": "未分类·综合", "score": 0}
            return max(matched.values(), key=lambda x: x["score"])

        # 扫描文件
        files = []
        for root, dirs, fnames in os.walk(book_folder):
            fnames.sort()
            for fname in fnames:
                ext = os.path.splitext(fname)[1].lower()
                if ext in extensions:
                    files.append(os.path.join(root, fname))

        if not files:
            return {"success": False, "error": f"在 {book_folder} 中未找到 {extensions} 文件"}

        # 加载模型
        model = self._get_model()

        # 准备 collection
        if rebuild:
            coll = self._delete_collection(collection_name)
        else:
            coll = self._get_collection(collection_name)

        # 分片 + 分类 + 向量化 + 入库
        all_docs, all_metas, all_ids = [], [], []
        idx = 0
        book_count = 0
        skipped = 0

        for filepath in files:
            rel_path = os.path.relpath(filepath, book_folder)
            book_name = os.path.splitext(os.path.basename(filepath))[0]

            # 读取
            raw = None
            for enc in ["utf-8", "gbk", "gb18030"]:
                try:
                    with open(filepath, "r", encoding=enc) as f:
                        raw = f.read()
                    break
                except UnicodeDecodeError:
                    continue
            if not raw:
                skipped += 1
                continue

            # 清洗
            content = _clean_text(raw)
            if len(content) < 20:
                skipped += 1
                continue

            # 分片
            chunks = _split_text(content, chunk_size, chunk_overlap)

            for seg in chunks:
                if len(seg) < 10:
                    continue
                classification = _classify(seg)
                doc_id = f"doc_{idx:06d}"
                all_docs.append(seg)
                all_metas.append({
                    "book": book_name,
                    "book_path": rel_path,
                    "category_id": classification["category_id"],
                    "category": classification["category_name"],
                    "score": str(classification["score"]),
                    "chunk_idx": str(idx),
                })
                all_ids.append(doc_id)
                idx += 1

                # 每 500 条批量写入
                if len(all_docs) >= 500:
                    embeds = model.encode(all_docs, show_progress_bar=False)
                    coll.add(
                        documents=all_docs,
                        metadatas=all_metas,
                        ids=all_ids,
                        embeddings=embeds,
                    )
                    all_docs, all_metas, all_ids = [], [], []

            book_count += 1

        # 最后一批
        if all_docs:
            embeds = model.encode(all_docs, show_progress_bar=False)
            coll.add(
                documents=all_docs,
                metadatas=all_metas,
                ids=all_ids,
                embeddings=embeds,
            )

        # 分类统计
        cat_stats = {}
        for m in all_metas:
            cat = m.get("category", "未知")
            cat_stats[cat] = cat_stats.get(cat, 0) + 1

        return {
            "success": True,
            "data": {
                "db_path": self.db_path,
                "collection": collection_name,
                "total_chunks": idx,
                "books_processed": book_count,
                "files_skipped": skipped,
                "categories": cat_stats,
                "embed_model": self.embed_model_name,
                "device": self._device,
                "gpu_diag": self._gpu_diag,
            },
        }

    # ---- 查询 ----

    def query(
        self,
        query: str,
        collection_name: str = DEFAULT_COLLECTION,
        top_k: int = 5,
        category: str = "",
        min_score: float = 0.0,
    ) -> Dict[str, Any]:
        """
        语义搜索向量数据库。

        Args:
            query: 搜索关键词/句子
            collection_name: ChromaDB 集合名称
            top_k: 返回结果数量（最大 20）
            category: 按分类标签过滤（需建库时启用了分类），为空不限制
            min_score: 最小相关度阈值（0-1），低于此值不返回

        Returns:
            包含匹配结果的字典
        """
        if not query or not query.strip():
            return {"success": False, "error": "查询内容不能为空"}

        top_k = max(1, min(top_k, 20))

        try:
            # 检查向量库是否存在
            coll_path = os.path.join(self.db_path, "chroma.sqlite3")
            if not os.path.exists(coll_path):
                # 可能还没有 .sqlite3 文件，检查目录是否存在
                if not os.path.isdir(self.db_path):
                    return {"success": False, "error": f"向量库不存在: {self.db_path}，请先建库"}

            model = self._get_model()
            coll = self._get_collection(collection_name)

            # 检查 collection 是否有数据
            count = coll.count()
            if count == 0:
                return {"success": False, "error": f"集合 '{collection_name}' 为空，请先建库"}

            # 向量检索
            query_emb = model.encode(query)

            where_filter = None
            if category:
                where_filter = {"category": category}

            results = coll.query(
                query_embeddings=[query_emb],
                n_results=top_k * 2,
                where=where_filter,
            )

            # 格式化
            docs = []
            if results and results.get("documents") and results["documents"][0]:
                for i in range(len(results["documents"][0])):
                    score = float(results["distances"][0][i]) if results.get("distances") else 0
                    if min_score > 0 and score < min_score:
                        continue
                    docs.append({
                        "content": results["documents"][0][i],
                        "book_name": results["metadatas"][0][i].get("book", ""),
                        "book_path": results["metadatas"][0][i].get("book_path", ""),
                        "category": results["metadatas"][0][i].get("category", "未分类"),
                        "relevance_score": round(score, 4),
                    })
                    if len(docs) >= top_k:
                        break

            return {
                "success": True,
                "data": {
                    "query": query,
                    "total": len(docs),
                    "results": docs,
                },
            }

        except Exception as e:
            return {"success": False, "error": f"查询失败: {str(e)}"}

    # ---- 信息查询 ----

    def info(self, collection_name: str = DEFAULT_COLLECTION) -> Dict[str, Any]:
        """获取向量库信息"""
        try:
            coll_path = os.path.join(self.db_path, "chroma.sqlite3")
            if not os.path.exists(coll_path):
                return {"success": False, "error": f"向量库不存在: {self.db_path}"}

            coll = self._get_collection(collection_name)
            count = coll.count()

            # 获取所有元数据以统计分类
            if count > 0:
                all_data = coll.get(include=["metadatas"])
                cat_stats = {}
                for m in all_data["metadatas"]:
                    cat = m.get("category", "未知")
                    cat_stats[cat] = cat_stats.get(cat, 0) + 1
            else:
                cat_stats = {}

            return {
                "success": True,
                "data": {
                    "db_path": self.db_path,
                    "collection": collection_name,
                    "total_chunks": count,
                    "embed_model": self.embed_model_name,
                    "categories": cat_stats,
                },
            }

        except Exception as e:
            return {"success": False, "error": str(e)}

    def list_collections(self) -> Dict[str, Any]:
        """列出所有集合"""
        try:
            if not os.path.isdir(self.db_path):
                return {"success": True, "data": {"collections": []}}

            client = self._get_chromadb()
            collections = client.list_collections()
            return {
                "success": True,
                "data": {
                    "collections": [{"name": c.name, "count": c.count()} for c in collections],
                },
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    def delete_collection(self, collection_name: str) -> Dict[str, Any]:
        """删除集合"""
        try:
            client = self._get_chromadb()
            try:
                client.delete_collection(collection_name)
                return {"success": True, "data": {"collection": collection_name, "deleted": True}}
            except Exception as e:
                return {"success": False, "error": f"删除失败: {str(e)}"}
        except Exception as e:
            return {"success": False, "error": str(e)}


# =============================================================================
# 工具管理器（框架自动发现）
# =============================================================================

class VecdbToolManager:
    """向量数据库工具管理器 — 由 Xenon 框架自动发现并注册为工具。

    每个公开方法自动成为一个工具，工具全名格式：vecdb_handler_Vecdb_{方法名}
    """

    def __init__(self):
        self._engine: Optional[_VecDBEngine] = None
        self._build_tasks: Dict[str, Dict[str, Any]] = {}  # task_id -> status/info

    def _get_engine(self, db_path: str = "", embed_model: str = "") -> _VecDBEngine:
        """获取或创建 _VecDBEngine 实例（支持动态路径）"""
        if not db_path:
            db_path = os.path.join(TOOL_STORAGE, "default")
        if not embed_model:
            embed_model = DEFAULT_EMBED_MODEL
        if self._engine is not None:
            if self._engine.db_path == db_path and self._engine.embed_model_name == embed_model:
                return self._engine
        self._engine = _VecDBEngine(db_path=db_path, embed_model=embed_model)
        return self._engine

    # ---- 建库工具 ----

    def vecdb_build(
        self,
        book_folder: str,
        db_path: str = "",
        collection_name: str = DEFAULT_COLLECTION,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
        use_classification: bool = False,
        rebuild: bool = True,
    ) -> Dict[str, Any]:
        """从文本文件构建向量数据库（异步执行）。

        建库在后台线程中运行，不会阻塞 Xenon。
        完成后结果自动推送到消息轮询池（如果可用）。
        可通过 vecdb_build_status(task_id) 查询任务状态。

        Args:
            book_folder: 文本文件所在目录路径（必填）
            db_path: 向量库存储路径（留空使用默认位置）
            collection_name: 集合名称，默认 "documents"
            chunk_size: 文本分片大小（字符数），默认 600
            chunk_overlap: 分片重叠字符数，默认 50
            use_classification: 是否启用文本分类（需配合 custom_categories），默认 False
            rebuild: 是否重新构建（清空旧数据），默认 True

        Returns:
            {"success": true, "data": {"task_id": "...", "status": "started", ...}}
        """
        # 参数校验（同步执行，快速返回失败）
        abs_folder = os.path.abspath(book_folder)
        if not os.path.isdir(abs_folder):
            return {"success": False, "error": f"目录不存在: {book_folder}"}

        task_id = uuid.uuid4().hex[:8]

        # 记录任务
        self._build_tasks[task_id] = {
            "status": "running",
            "start_time": time.time(),
            "book_folder": abs_folder,
        }

        def _background_build():
            """后台建库线程"""
            try:
                manager = self._get_engine(db_path)
                result = manager.build(
                    book_folder=abs_folder,
                    collection_name=collection_name,
                    chunk_size=chunk_size,
                    chunk_overlap=chunk_overlap,
                    use_classification=use_classification,
                    rebuild=rebuild,
                )

                # 更新本地任务状态
                self._build_tasks[task_id] = {
                    "status": "completed",
                    "completed_at": time.time(),
                    "result": result,
                }

                # 推送到轮询池
                self._push_to_pool(task_id, result)

            except Exception as e:
                error_result = {"success": False, "error": f"建库异常: {str(e)}"}
                self._build_tasks[task_id] = {
                    "status": "failed",
                    "completed_at": time.time(),
                    "error": str(e),
                }
                self._push_to_pool(task_id, error_result)

        thread = threading.Thread(
            target=_background_build,
            daemon=True,
            name=f"vecdb_build_{task_id}",
        )
        thread.start()

        return {
            "success": True,
            "data": {
                "task_id": task_id,
                "status": "started",
                "message": (
                    f"建库任务已启动（task_id={task_id}），"
                    f"正在后台处理 {abs_folder} 中的文件，"
                    f"完成后结果将出现在轮询池中。"
                ),
            },
        }

    def _push_to_pool(self, task_id: str, result: Dict[str, Any]) -> None:
        """将建库结果推送到消息轮询池。"""
        try:
            from xenon_core.polling_pool import get_pool, PoolMessage

            pool = get_pool()
            pool.push(
                PoolMessage(
                    source="vecdb_handler",
                    scenario="vecdb_build",
                    msg_type="result",
                    payload={
                        "task_id": task_id,
                        "success": result.get("success", False),
                        "data": result.get("data", result),
                    },
                    priority=2 if not result.get("success", False) else 1,
                    ttl=3600,  # 1 小时后过期
                )
            )
        except ImportError as e:
            print(f"[vecdb_handler] 轮询池不可用: {e}")
        except Exception as e:
            print(f"[vecdb_handler] 推送轮询池失败: {e}")
            import traceback
            traceback.print_exc()

    def vecdb_build_status(self, task_id: str) -> Dict[str, Any]:
        """查询异步建库任务的状态。

        Args:
            task_id: vecdb_build 返回的任务 ID

        Returns:
            任务当前状态信息
        """
        info = self._build_tasks.get(task_id)
        if info is None:
            return {"success": False, "error": f"任务不存在: {task_id}"}

        elapsed = time.time() - info.get("start_time", time.time())
        return {
            "success": True,
            "data": {
                "task_id": task_id,
                "status": info["status"],
                "elapsed_seconds": round(elapsed, 1),
                "result": info.get("result"),
                "error": info.get("error"),
            },
        }

    # ---- 查询工具 ----

    def vecdb_query(
        self,
        query: str,
        db_path: str = "",
        collection_name: str = DEFAULT_COLLECTION,
        top_k: int = 5,
        category: str = "",
    ) -> Dict[str, Any]:
        """语义搜索向量数据库。

        将查询内容转为向量，在 ChromaDB 中做语义相似度搜索，
        返回最相关的文本片段及出处信息。

        Args:
            query: 搜索关键词或句子（必填）
            db_path: 向量库路径（与建库时一致，留空使用默认）
            collection_name: 集合名称，默认 "documents"
            top_k: 返回结果数量（1-20），默认 5
            category: 按分类标签过滤（需建库时启用了分类），留空不限制

        Returns:
            {"success": true/false, "data": {"query": "...", "results": [...]}}
        """
        manager = self._get_engine(db_path)
        return manager.query(
            query=query,
            collection_name=collection_name,
            top_k=top_k,
            category=category,
        )

    # ---- 信息工具 ----

    def vecdb_info(
        self,
        db_path: str = "",
        collection_name: str = DEFAULT_COLLECTION,
    ) -> Dict[str, Any]:
        """查看向量库信息。

        查询指定向量库的状态，包括总切片数、分类统计、使用的嵌入模型等。

        Args:
            db_path: 向量库路径（留空使用默认）
            collection_name: 集合名称，默认 "documents"

        Returns:
            {"success": true/false, "data": {"total_chunks": N, "categories": {...}, ...}}
        """
        manager = self._get_engine(db_path)
        return manager.info(collection_name=collection_name)

    def vecdb_list(
        self,
        db_path: str = "",
    ) -> Dict[str, Any]:
        """列出向量库中的所有集合。

        Args:
            db_path: 向量库路径（留空使用默认）

        Returns:
            {"success": true/false, "data": {"collections": [{"name": ..., "count": N}, ...]}}
        """
        manager = self._get_engine(db_path)
        return manager.list_collections()

    def vecdb_delete(
        self,
        collection_name: str,
        db_path: str = "",
    ) -> Dict[str, Any]:
        """删除向量库中的指定集合。

        Args:
            collection_name: 要删除的集合名称
            db_path: 向量库路径（留空使用默认）

        Returns:
            {"success": true/false, "data": {"deleted": true}}
        """
        manager = self._get_engine(db_path)
        return manager.delete_collection(collection_name)


# =============================================================================
# 模块级工具函数（框架也可以通过这些函数发现工具）
# =============================================================================

_default_tool_manager = VecdbToolManager()


def vecdb_build(book_folder: str, **kwargs) -> Dict[str, Any]:
    """从文本文件构建向量数据库（异步）"""
    return _default_tool_manager.vecdb_build(book_folder, **kwargs)


def vecdb_build_status(task_id: str) -> Dict[str, Any]:
    """查询异步建库任务的状态"""
    return _default_tool_manager.vecdb_build_status(task_id)


def vecdb_query(query: str, **kwargs) -> Dict[str, Any]:
    """语义搜索向量数据库"""
    return _default_tool_manager.vecdb_query(query, **kwargs)


def vecdb_info(**kwargs) -> Dict[str, Any]:
    """查看向量库信息"""
    return _default_tool_manager.vecdb_info(**kwargs)


def vecdb_list(**kwargs) -> Dict[str, Any]:
    """列出所有集合"""
    return _default_tool_manager.vecdb_list(**kwargs)


def vecdb_delete(collection_name: str, **kwargs) -> Dict[str, Any]:
    """删除集合"""
    return _default_tool_manager.vecdb_delete(collection_name, **kwargs)
