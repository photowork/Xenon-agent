"""
embedding.py — 轻量语义嵌入服务

封装 bge-small-zh-v1.5，为 MemoryAPI 提供统一的向量化能力。
零外部依赖（除 sentence-transformers），惰性加载。

用法:
    embedder = EmbeddingService("models/bge-small-zh-v1.5")
    vec = embedder.encode("这是一段文本")       # np.ndarray, shape=(512,), 已归一化
    sim = embedder.similarity(vec1, vec2)        # 余弦相似度 (0~1)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional, List, Union

import numpy as np


class EmbeddingService:
    """语义嵌入服务 — 封装 sentence-transformers 模型（GPU优先，CPU兜底）"""

    def __init__(self, model_path: str, device: str = None):
        """
        Args:
            model_path: 模型文件夹路径（绝对路径或相对于项目根目录）
            device: 'cpu', 'cuda', 'cuda:0' 等，None 表示自动选择（GPU优先 → CPU兜底）
        """
        self.model_path = str(Path(model_path).resolve())
        self.device = device
        self._model = None
        self._active_device: Optional[str] = None

    @property
    def dim(self) -> int:
        """向量维度"""
        model = self._get_model()
        # 兼容新旧版本的 sentence-transformers
        if hasattr(model, 'get_embedding_dimension'):
            return model.get_embedding_dimension()
        return model.get_sentence_embedding_dimension()

    @property
    def loaded(self) -> bool:
        return self._model is not None

    @property
    def active_device(self) -> str:
        """实际运行的设备名称（cuda / cpu），未加载前返回 pending"""
        if self._active_device:
            return self._active_device
        if self.loaded:
            return self._active_device or "unknown"
        return "pending"

    def _get_model(self):
        """惰性加载模型（GPU优先  →  CPU兜底）"""
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            # ── 设备选择 ──
            device = self.device
            if device is None:
                # 自动选择：GPU优先，CPU兜底
                device = "cpu"
                try:
                    import torch
                    if torch.cuda.is_available():
                        device = "cuda"
                except ImportError:
                    pass

            try:
                self._model = SentenceTransformer(self.model_path, device=device)
                self._active_device = device
            except Exception:
                if device == "cuda":
                    # GPU 加载失败（OOM/驱动问题），回退 CPU
                    device = "cpu"
                    self._model = SentenceTransformer(self.model_path, device=device)
                    self._active_device = device
                else:
                    raise
        return self._model

    def encode(
        self,
        text: str,
        normalize: bool = True,
    ) -> np.ndarray:
        """
        将文本编码为向量。

        Args:
            text: 输入文本
            normalize: 是否归一化（默认 True，使得相似度 = 向量点积）

        Returns:
            np.ndarray，shape=(dim,)，float32
        """
        model = self._get_model()
        vec = model.encode(
            text,
            normalize_embeddings=normalize,
            show_progress_bar=False,
        )
        return np.asarray(vec, dtype=np.float32)

    def encode_batch(
        self,
        texts: List[str],
        normalize: bool = True,
    ) -> np.ndarray:
        """
        批量编码。

        Args:
            texts: 文本列表
            normalize: 是否归一化

        Returns:
            np.ndarray，shape=(len(texts), dim)，float32
        """
        model = self._get_model()
        vecs = model.encode(
            texts,
            normalize_embeddings=normalize,
            show_progress_bar=False,
        )
        return np.asarray(vecs, dtype=np.float32)

    def similarity(self, vec_a: np.ndarray, vec_b: np.ndarray) -> float:
        """
        计算两个向量的余弦相似度。

        因为 encode 默认归一化，所以直接点积即可。
        """
        return float(np.dot(vec_a, vec_b))

    def mean_vector(self, vectors: List[np.ndarray]) -> np.ndarray:
        """
        计算多个向量的均值（用于父节点聚合）。

        Args:
            vectors: 向量列表，不能为空

        Returns:
            均值向量（已归一化）
        """
        if not vectors:
            raise ValueError("vectors cannot be empty")
        stacked = np.stack(vectors, axis=0)
        mean = np.mean(stacked, axis=0)
        # 重新归一化
        norm = np.linalg.norm(mean)
        if norm > 0:
            mean = mean / norm
        return mean.astype(np.float32)

    def vector_to_list(self, vec: np.ndarray) -> List[float]:
        """将 np.ndarray 转为 List[float]，便于 JSON 序列化"""
        return vec.tolist()

    def list_to_vector(self, lst: List[float]) -> np.ndarray:
        """将 List[float] 还原为 np.ndarray"""
        return np.asarray(lst, dtype=np.float32)
