#!/usr/bin/env python
"""
k-NN 兴趣模型推理后端。

原理：加载训练时保存的正样本 embedding 库（pos_embeddings.npy），
对每篇新论文计算它与库中最相似 k 篇论文的平均余弦相似度，
减去与最相似若干负样本的惩罚，得到最终推荐得分。

不依赖任何聚类，彻底消除 FRB/WD/Pulsar 分布失衡问题：
每篇历史正样本都直接参与相似度计算，稀有话题同样被覆盖。
"""

import importlib
import json
import logging
import math
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import numpy as np
import pandas as pd
import torch

from .shared import (
    InferenceParams,
    build_paper_texts,
    load_training_config,
    resolve_artifact_fingerprint,
    resolve_dataframe_column,
    resolve_multi_interest_model_path,
)

logger = logging.getLogger(__name__)

_MODEL_CACHE: dict[tuple[str, str, int], "KNNInferenceModel"] = {}


def _load_embedding_matrix(
    path: Path,
    *,
    expected_dim: int,
    name: str,
) -> np.ndarray:
    """加载并验证可信模型目录中的二维有限浮点 embedding。"""

    matrix = np.load(path, allow_pickle=False)
    if matrix.ndim != 2 or matrix.shape[0] == 0 or matrix.shape[1] != expected_dim:
        raise ValueError(
            f"{name} must have shape (n, {expected_dim}) with at least one row; got {matrix.shape}"
        )
    if not np.issubdtype(matrix.dtype, np.number) or not np.isfinite(matrix).all():
        raise ValueError(f"{name} must contain only finite numeric values")
    return cast(np.ndarray, matrix.astype(np.float32, copy=False))


# =============================================================================
# 推理模型
# =============================================================================


class KNNInferenceModel:
    """
    加载已训练的 k-NN 兴趣模型并对新论文打分。

    文件布局（model_dir/）：
      pos_embeddings.npy    — 全部正样本 embedding，(n_pos, D) float32
      neg_embeddings.npy    — 负样本子集 embedding，(n_neg, D) float32（可选）
      meta.json             — encoder_name / k / neg_k / neg_weight / threshold / columns
      training_config.json  — 与推理层约定的元数据
    """

    def __init__(self, model_dir: str, batch_size: int = 256):
        self.model_dir = Path(model_dir)
        if type(batch_size) is not int or batch_size <= 0:
            raise ValueError("batch_size must be a positive integer")
        self.batch_size = batch_size

        # ── 加载元数据 ──────────────────────────────────────────────────
        meta_path = self.model_dir / "meta.json"
        if not meta_path.exists():
            raise FileNotFoundError(f"meta.json not found at {meta_path}")
        with open(meta_path, encoding="utf-8") as f:
            meta = json.load(f)
        if not isinstance(meta, Mapping):
            raise ValueError("meta.json must contain a JSON object")

        encoder_name = meta.get("encoder_name")
        embed_dim = meta.get("embed_dim")
        k = meta.get("k", 10)
        neg_k = meta.get("neg_k", 5)
        neg_weight = meta.get("neg_weight", 0.5)
        threshold = meta.get("threshold", 0.5)
        columns = meta.get("columns", {})
        if not isinstance(encoder_name, str) or not encoder_name.strip():
            raise ValueError("encoder_name must be a non-empty string")
        if type(embed_dim) is not int or embed_dim <= 0:
            raise ValueError("embed_dim must be a positive integer")
        if type(k) is not int or k <= 0:
            raise ValueError("k must be a positive integer")
        if type(neg_k) is not int or neg_k < 0:
            raise ValueError("neg_k must be a non-negative integer")
        if (
            isinstance(neg_weight, bool)
            or not isinstance(neg_weight, (int, float))
            or not math.isfinite(float(neg_weight))
            or float(neg_weight) < 0
        ):
            raise ValueError("neg_weight must be a finite non-negative number")
        if (
            isinstance(threshold, bool)
            or not isinstance(threshold, (int, float))
            or not math.isfinite(float(threshold))
        ):
            raise ValueError("threshold must be a finite number")
        if not isinstance(columns, Mapping) or any(
            not isinstance(key, str) or (value is not None and not isinstance(value, str))
            for key, value in columns.items()
        ):
            raise ValueError("columns must map strings to strings or null")

        self.encoder_name = encoder_name.strip()
        self.embed_dim = embed_dim
        self.k = k
        self.neg_k = neg_k
        self.neg_weight = float(neg_weight)
        self.threshold = float(threshold)
        self.columns: dict[str, Any] = dict(columns)

        # ── 加载 embedding 库 ───────────────────────────────────────────
        pos_path = self.model_dir / "pos_embeddings.npy"
        if not pos_path.exists():
            raise FileNotFoundError(f"pos_embeddings.npy not found at {pos_path}")
        self.pos_embeddings = _load_embedding_matrix(
            pos_path,
            expected_dim=self.embed_dim,
            name="pos_embeddings",
        )
        logger.info("Loaded pos_embeddings: %s", self.pos_embeddings.shape)

        neg_path = self.model_dir / "neg_embeddings.npy"
        if neg_path.exists():
            self.neg_embeddings: np.ndarray | None = _load_embedding_matrix(
                neg_path,
                expected_dim=self.embed_dim,
                name="neg_embeddings",
            )
            logger.info("Loaded neg_embeddings: %s", self.neg_embeddings.shape)
        else:
            self.neg_embeddings = None
            logger.info("neg_embeddings.npy not found; running without negative penalty.")

        # ── 加载编码器 ──────────────────────────────────────────────────
        self._fp16 = torch.cuda.is_available()
        sentence_transformer = importlib.import_module("sentence_transformers").SentenceTransformer
        self.encoder = sentence_transformer(self.encoder_name)

    # ------------------------------------------------------------------
    # Encoding
    # ------------------------------------------------------------------

    def encode(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.embed_dim), dtype=np.float32)
        kw: dict[str, Any] = {
            "batch_size": self.batch_size,
            "show_progress_bar": False,
            "convert_to_numpy": True,
            "normalize_embeddings": True,
        }
        if self._fp16:
            with torch.amp.autocast("cuda"):
                emb = self.encoder.encode(texts, **kw)
        else:
            emb = self.encoder.encode(texts, **kw)
        # sentence-transformers 是动态导入，运行时已要求 convert_to_numpy=True。
        return cast(np.ndarray, emb.astype(np.float32))

    # ------------------------------------------------------------------
    # 得分计算
    # ------------------------------------------------------------------

    def _score(self, query_emb: np.ndarray) -> np.ndarray:
        """
        query_emb: (n, D) normalized → scores: (n,) float32

        score_i = mean(top_k_pos_sims_i) - neg_weight * mean(top_k_neg_sims_i)
        """
        query = np.asarray(query_emb)
        expected_dim = int(self.pos_embeddings.shape[1])
        if query.ndim != 2 or query.shape[1] != expected_dim:
            raise ValueError(f"query embeddings must have shape (n, {expected_dim})")
        if not np.issubdtype(query.dtype, np.number) or not np.isfinite(query).all():
            raise ValueError("query embeddings must contain only finite numeric values")
        if len(query) == 0:
            return cast(np.ndarray, np.array([], dtype=np.float32))

        # 正样本相似度
        pos_sims = query @ self.pos_embeddings.T  # (n, n_pos)
        k = min(self.k, pos_sims.shape[1])
        top_k_pos = np.sort(pos_sims, axis=1)[:, -k:].mean(axis=1)  # (n,)

        # 负样本惩罚（可选）
        if self.neg_embeddings is not None and self.neg_k > 0 and self.neg_weight > 0:
            neg_sims = query @ self.neg_embeddings.T  # (n, n_neg)
            nk = min(self.neg_k, neg_sims.shape[1])
            top_k_neg = np.sort(neg_sims, axis=1)[:, -nk:].mean(axis=1)
            return cast(
                np.ndarray,
                (top_k_pos - self.neg_weight * top_k_neg).astype(np.float32),
            )

        return cast(np.ndarray, top_k_pos.astype(np.float32))

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------

    def predict_proba(self, df: pd.DataFrame, input_mode: str = "title_abstract") -> np.ndarray:
        """返回每篇论文的推荐得分 (1-d float32 array)。"""
        if len(df) == 0:
            return cast(np.ndarray, np.array([], dtype=np.float32))
        if input_mode not in {"title_only", "title_abstract"}:
            raise ValueError("unsupported input_mode")

        # 列名解析：优先用训练时记录的列名，fallback 到通用候选
        title_col = resolve_dataframe_column(df, self.columns.get("title"), ("Title", "title"))
        abstract_col = None
        if input_mode != "title_only":
            abstract_col = resolve_dataframe_column(
                df,
                self.columns.get("abstract"),
                ("Abstract", "abstract"),
            )
            if abstract_col is None:
                raise ValueError("title_abstract input requires an abstract column")

        texts = build_paper_texts(df, title_col, abstract_col)
        emb = self.encode(texts)
        return self._score(emb)


# =============================================================================
# 统一入口（被 runner.py 调用）
# =============================================================================


def run_knn_inference(
    params: InferenceParams,
    data: pd.DataFrame,
) -> tuple[list[float], list[int]]:
    """执行 k-NN 模型推理，返回 (probs, preds)。"""
    tcfg = load_training_config(params.model_path)
    runtime_path = resolve_multi_interest_model_path(params.model_path, tcfg)
    resolved_path = str(Path(runtime_path).resolve())
    cache_key = (
        resolved_path,
        resolve_artifact_fingerprint(params, resolved_path),
        int(params.batch_size),
    )
    model = _MODEL_CACHE.get(cache_key)
    if model is None:
        model = KNNInferenceModel(runtime_path, batch_size=params.batch_size)
        for stale_key in [key for key in _MODEL_CACHE if key[0] == resolved_path]:
            _MODEL_CACHE.pop(stale_key, None)
        _MODEL_CACHE[cache_key] = model
    proba = model.predict_proba(data, input_mode=params.input_mode)

    probs = proba.astype(float).tolist()
    preds = [1 if p >= params.threshold else 0 for p in probs]
    return probs, preds
