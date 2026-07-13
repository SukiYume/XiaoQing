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
import re
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import torch

from .shared import InferenceParams, load_training_config, resolve_multi_interest_model_path

logger = logging.getLogger(__name__)

_MODEL_CACHE: dict[str, "KNNInferenceModel"] = {}


# =============================================================================
# 工具函数
# =============================================================================


def _load_st():
    return importlib.import_module("sentence_transformers").SentenceTransformer


def _clean(x: object) -> str:
    if pd.isna(x):
        return ""
    return re.sub(r"\s+", " ", str(x)).strip()


def _build_texts(
    df: pd.DataFrame, title_col: str | None, abstract_col: str | None
) -> list[str]:
    titles = df[title_col].fillna("").astype(str).tolist() if title_col else [""] * len(df)
    if abstract_col and abstract_col in df.columns:
        abstracts = df[abstract_col].fillna("").astype(str).tolist()
        return [
            f"Title: {_clean(t)}\nAbstract: {_clean(a)}"
            for t, a in zip(titles, abstracts, strict=True)
        ]
    return [f"Title: {_clean(t)}" for t in titles]


def _resolve_col(
    df: pd.DataFrame, trained_col: str | None, fallbacks: list[str]
) -> str | None:
    if trained_col and trained_col in df.columns:
        return trained_col
    for fb in fallbacks:
        if fb in df.columns:
            return fb
    return None


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
        self.batch_size = batch_size

        # ── 加载元数据 ──────────────────────────────────────────────────
        meta_path = self.model_dir / "meta.json"
        if not meta_path.exists():
            raise FileNotFoundError(f"meta.json not found at {meta_path}")
        with open(meta_path, encoding="utf-8") as f:
            meta = json.load(f)

        self.encoder_name: str = meta["encoder_name"]
        self.embed_dim: int = meta["embed_dim"]
        self.k: int = int(meta.get("k", 10))
        self.neg_k: int = int(meta.get("neg_k", 5))
        self.neg_weight: float = float(meta.get("neg_weight", 0.5))
        self.threshold: float = float(meta.get("threshold", 0.5))
        self.columns: dict[str, Any] = meta.get("columns", {})

        # ── 加载 embedding 库 ───────────────────────────────────────────
        pos_path = self.model_dir / "pos_embeddings.npy"
        if not pos_path.exists():
            raise FileNotFoundError(f"pos_embeddings.npy not found at {pos_path}")
        self.pos_embeddings: np.ndarray = np.load(pos_path)  # (n_pos, D)
        logger.info("Loaded pos_embeddings: %s", self.pos_embeddings.shape)

        neg_path = self.model_dir / "neg_embeddings.npy"
        if neg_path.exists():
            self.neg_embeddings: np.ndarray | None = np.load(neg_path)  # (n_neg, D)
            logger.info("Loaded neg_embeddings: %s", self.neg_embeddings.shape)
        else:
            self.neg_embeddings = None
            logger.info("neg_embeddings.npy not found; running without negative penalty.")

        # ── 加载编码器 ──────────────────────────────────────────────────
        self._fp16 = torch.cuda.is_available()
        self.encoder = _load_st()(self.encoder_name)

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
        return emb.astype(np.float32)

    # ------------------------------------------------------------------
    # 得分计算
    # ------------------------------------------------------------------

    def _score(self, query_emb: np.ndarray) -> np.ndarray:
        """
        query_emb: (n, D) normalized → scores: (n,) float32

        score_i = mean(top_k_pos_sims_i) - neg_weight * mean(top_k_neg_sims_i)
        """
        # 正样本相似度
        pos_sims = query_emb @ self.pos_embeddings.T  # (n, n_pos)
        k = min(self.k, pos_sims.shape[1])
        top_k_pos = np.sort(pos_sims, axis=1)[:, -k:].mean(axis=1)  # (n,)

        # 负样本惩罚（可选）
        if self.neg_embeddings is not None and self.neg_k > 0 and self.neg_weight > 0:
            neg_sims = query_emb @ self.neg_embeddings.T  # (n, n_neg)
            nk = min(self.neg_k, neg_sims.shape[1])
            top_k_neg = np.sort(neg_sims, axis=1)[:, -nk:].mean(axis=1)
            return (top_k_pos - self.neg_weight * top_k_neg).astype(np.float32)

        return top_k_pos.astype(np.float32)

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------

    def predict_proba(self, df: pd.DataFrame, input_mode: str = "title_abstract") -> np.ndarray:
        """返回每篇论文的推荐得分 (1-d float32 array)。"""
        if len(df) == 0:
            return np.array([], dtype=np.float32)

        # 列名解析：优先用训练时记录的列名，fallback 到通用候选
        title_col = _resolve_col(df, self.columns.get("title"), ["Title", "title"])
        abstract_col = None
        if input_mode != "title_only":
            abstract_col = _resolve_col(df, self.columns.get("abstract"), ["Abstract", "abstract"])

        texts = _build_texts(df, title_col, abstract_col)
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
    cache_key = str(Path(runtime_path).resolve())
    model = _MODEL_CACHE.get(cache_key)
    if model is None:
        model = KNNInferenceModel(runtime_path, batch_size=params.batch_size)
        _MODEL_CACHE[cache_key] = model
    proba = model.predict_proba(data, input_mode=params.input_mode)

    probs = proba.astype(float).tolist()
    preds = [1 if p >= params.threshold else 0 for p in probs]
    return probs, preds
