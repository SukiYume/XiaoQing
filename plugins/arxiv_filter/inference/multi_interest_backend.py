#!/usr/bin/env python
"""
多兴趣 (sentence-transformers) 模型推理后端。

输入：InferenceParams + DataFrame (含 Title 列，可选 Abstract 列)
输出：(probs: list[float], preds: list[int])
"""

import importlib
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import joblib
import numpy as np
import pandas as pd
import torch
from sklearn.linear_model import LogisticRegression

from .shared import InferenceParams, load_training_config, resolve_multi_interest_model_path

logger = logging.getLogger(__name__)

_MODEL_CACHE: dict[str, "MultiInterestInferenceModel"] = {}


# =============================================================================
# Artifacts
# =============================================================================


@dataclass
class ModelArtifacts:
    encoder_name: str
    n_interests: int
    embedding_dim: int
    interest_centers: np.ndarray
    pos_centroid: np.ndarray
    neg_centroid: Optional[np.ndarray]
    classifier: LogisticRegression
    threshold: float
    threshold_beta: float
    cluster_keywords: List[str]
    cluster_sizes: List[int]
    cluster_examples: List[List[str]]
    feature_names: List[str]
    columns: Dict[str, Optional[str]]


# =============================================================================
# 工具函数
# =============================================================================


def _load_sentence_transformer():
    return importlib.import_module("sentence_transformers").SentenceTransformer


def _clean(x: object) -> str:
    if pd.isna(x):
        return ""
    return re.sub(r"\s+", " ", str(x)).strip()


def _softmax(x: np.ndarray, axis: int = 1) -> np.ndarray:
    x = x - np.max(x, axis=axis, keepdims=True)
    e = np.exp(x)
    return e / (np.sum(e, axis=axis, keepdims=True) + 1e-12)


def _build_texts(df: pd.DataFrame, title_col: str, abstract_col: Optional[str]) -> List[str]:
    """根据是否有 abstract 列构建输入文本。"""
    titles = df[title_col].fillna("").astype(str).tolist()
    if abstract_col is None or abstract_col not in df.columns:
        return [f"Title: {_clean(t)}" for t in titles]
    abstracts = df[abstract_col].fillna("").astype(str).tolist()
    return [f"Title: {_clean(t)}\nAbstract: {_clean(a)}" for t, a in zip(titles, abstracts)]


# =============================================================================
# 推理模型
# =============================================================================


class MultiInterestInferenceModel:
    def __init__(self, model_dir: str, batch_size: int = 256):
        self.model_dir = Path(model_dir)
        self.batch_size = batch_size

        artifacts_path = self.model_dir / "artifacts.joblib"
        if not artifacts_path.exists():
            raise FileNotFoundError(f"Model artifacts not found at {artifacts_path}")

        self.artifacts = ModelArtifacts(**joblib.load(artifacts_path))
        self.encoder = _load_sentence_transformer()(self.artifacts.encoder_name)
        self._use_fp16 = torch.cuda.is_available()

    def encode_texts(self, texts: List[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.artifacts.embedding_dim), dtype=np.float32)
        kw: Dict[str, Any] = dict(
            batch_size=self.batch_size,
            show_progress_bar=False,
            convert_to_numpy=True,
            normalize_embeddings=True,
        )
        if self._use_fp16:
            with torch.amp.autocast("cuda"):
                emb = self.encoder.encode(texts, **kw)
        else:
            emb = self.encoder.encode(texts, **kw)
        return emb.astype(np.float32)

    def _build_features(self, embeddings: np.ndarray) -> np.ndarray:
        a = self.artifacts
        sims = embeddings @ a.interest_centers.T

        sorted_sims = np.sort(sims, axis=1)
        top2 = sorted_sims[:, -2] if sims.shape[1] > 1 else sorted_sims[:, -1]

        probs = _softmax(sims, axis=1)
        entropy = -np.sum(probs * np.log(probs + 1e-12), axis=1)

        sim_pos = (embeddings @ a.pos_centroid.reshape(-1, 1)).ravel()
        sim_neg = (
            (embeddings @ a.neg_centroid.reshape(-1, 1)).ravel()
            if a.neg_centroid is not None
            else np.zeros(len(embeddings), dtype=np.float32)
        )

        return np.column_stack(
            [
                sims,
                sims.max(axis=1),
                sims.mean(axis=1),
                sims.std(axis=1),
                sims.min(axis=1),
                sorted_sims[:, -1] - top2,
                entropy,
                probs.max(axis=1),
                sim_pos,
                sim_neg,
                sim_pos - sim_neg,  # contrast_pos_neg: 离正样本重心比负样本重心近多少
            ]
        ).astype(np.float32)

    def predict_proba(self, df: pd.DataFrame, input_mode: str = "title_abstract") -> np.ndarray:
        """返回每篇论文的正类概率 (1-d array)。"""
        if len(df) == 0:
            return np.array([], dtype=np.float32)

        # 解析列名：优先用训练时的列名，fallback 到常见列名
        cols = self.artifacts.columns
        title_col = _resolve_col(df, cols.get("title"), ["Title", "title"])
        abstract_col = None
        if input_mode != "title_only":
            abstract_col = _resolve_col(df, cols.get("abstract"), ["Abstract", "abstract"])

        texts = _build_texts(df, title_col, abstract_col)
        embeddings = self.encode_texts(texts)
        X = self._build_features(embeddings)
        return self.artifacts.classifier.predict_proba(X)[:, 1]


def _resolve_col(
    df: pd.DataFrame, trained_col: Optional[str], fallbacks: List[str]
) -> Optional[str]:
    """在 df 中找到实际可用的列名。"""
    if trained_col and trained_col in df.columns:
        return trained_col
    for fb in fallbacks:
        if fb in df.columns:
            return fb
    return None


# =============================================================================
# 统一入口
# =============================================================================


def run_multi_interest_inference(
    params: InferenceParams,
    data: pd.DataFrame,
) -> tuple[list[float], list[int]]:
    """执行多兴趣模型推理。

    自动从 params 中读取 model_path、threshold、input_mode。
    """
    tcfg = load_training_config(params.model_path)
    runtime_path = resolve_multi_interest_model_path(params.model_path, tcfg)
    cache_key = str(Path(runtime_path).resolve())
    model = _MODEL_CACHE.get(cache_key)
    if model is None:
        model = MultiInterestInferenceModel(runtime_path, batch_size=params.batch_size)
        _MODEL_CACHE[cache_key] = model
    proba = model.predict_proba(data, input_mode=params.input_mode)

    probs = proba.astype(float).tolist()
    preds = [1 if p >= params.threshold else 0 for p in probs]
    return probs, preds
