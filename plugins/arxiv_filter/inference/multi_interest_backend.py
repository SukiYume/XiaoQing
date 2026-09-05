#!/usr/bin/env python
"""
多兴趣 (sentence-transformers) 模型推理后端。

输入：InferenceParams + DataFrame (含 Title 列，可选 Abstract 列)
输出：(probs: list[float], preds: list[int])
"""

import importlib
import logging
import math
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import joblib
import numpy as np
import pandas as pd
import torch
from sklearn.linear_model import LogisticRegression

from ..numerics import stable_softmax
from .shared import (
    InferenceParams,
    build_paper_texts,
    load_training_config,
    resolve_artifact_fingerprint,
    resolve_dataframe_column,
    resolve_multi_interest_model_path,
)

logger = logging.getLogger(__name__)

_MODEL_CACHE: dict[tuple[str, str, int], "MultiInterestInferenceModel"] = {}


# =============================================================================
# Artifacts
# =============================================================================


@dataclass(frozen=True, slots=True)
class ModelArtifacts:
    encoder_name: str
    n_interests: int
    embedding_dim: int
    interest_centers: np.ndarray
    pos_centroid: np.ndarray
    neg_centroid: np.ndarray | None
    classifier: LogisticRegression
    threshold: float
    threshold_beta: float
    cluster_keywords: list[str]
    cluster_sizes: list[int]
    cluster_examples: list[list[str]]
    feature_names: list[str]
    columns: dict[str, str | None]


# =============================================================================
# 推理模型
# =============================================================================


class MultiInterestInferenceModel:
    def __init__(self, model_dir: str, batch_size: int = 256):
        self.model_dir = Path(model_dir)
        if type(batch_size) is not int or batch_size <= 0:
            raise ValueError("batch_size must be a positive integer")
        self.batch_size = batch_size

        artifacts_path = self.model_dir / "artifacts.joblib"
        if not artifacts_path.exists():
            raise FileNotFoundError(f"Model artifacts not found at {artifacts_path}")

        payload = joblib.load(artifacts_path)
        if not isinstance(payload, Mapping):
            raise ValueError("artifacts.joblib must contain a mapping")
        self.artifacts = ModelArtifacts(**payload)
        artifacts      = self.artifacts
        if not isinstance(artifacts.encoder_name, str) or not artifacts.encoder_name.strip():
            raise ValueError("encoder_name must be a non-empty string")
        if type(artifacts.n_interests) is not int or artifacts.n_interests <= 0:
            raise ValueError("n_interests must be a positive integer")
        if type(artifacts.embedding_dim) is not int or artifacts.embedding_dim <= 0:
            raise ValueError("embedding_dim must be a positive integer")
        if (
            isinstance(artifacts.threshold, bool)
            or not isinstance(artifacts.threshold, (int, float))
            or not math.isfinite(float(artifacts.threshold))
        ):
            raise ValueError("threshold must be a finite number")
        expected_dim = artifacts.embedding_dim
        centers      = np.asarray(artifacts.interest_centers)
        pos_centroid = np.asarray(artifacts.pos_centroid)
        neg_centroid = (
            None if artifacts.neg_centroid is None else np.asarray(artifacts.neg_centroid)
        )
        if centers.shape != (artifacts.n_interests, expected_dim):
            raise ValueError("interest_centers shape does not match model metadata")
        if pos_centroid.shape != (expected_dim,):
            raise ValueError("pos_centroid shape does not match model metadata")
        if neg_centroid is not None and neg_centroid.shape != (expected_dim,):
            raise ValueError("neg_centroid shape does not match model metadata")
        for name, value in (
            ("interest_centers", centers),
            ("pos_centroid", pos_centroid),
            ("neg_centroid", neg_centroid),
        ):
            if value is not None and (
                not np.issubdtype(value.dtype, np.number) or not np.isfinite(value).all()
            ):
                raise ValueError(f"{name} must contain only finite numeric values")
        if not isinstance(artifacts.classifier, LogisticRegression):
            raise ValueError("classifier must be a fitted LogisticRegression")
        expected_features = artifacts.n_interests + 10
        if len(artifacts.feature_names) != expected_features:
            raise ValueError("feature_names do not match the multi-interest feature layout")
        if getattr(artifacts.classifier, "n_features_in_", None) != expected_features:
            raise ValueError("classifier feature count does not match model artifacts")
        if (
            len(artifacts.cluster_keywords) != artifacts.n_interests
            or len(artifacts.cluster_sizes) != artifacts.n_interests
            or len(artifacts.cluster_examples) != artifacts.n_interests
        ):
            raise ValueError("cluster metadata count does not match n_interests")
        if not isinstance(artifacts.columns, Mapping) or any(
            not isinstance(key, str) or (value is not None and not isinstance(value, str))
            for key, value in artifacts.columns.items()
        ):
            raise ValueError("columns must map strings to strings or null")

        sentence_transformer = importlib.import_module("sentence_transformers").SentenceTransformer
        self.encoder         = sentence_transformer(artifacts.encoder_name)
        encoder_dim          = int(self.encoder.get_sentence_embedding_dimension())
        if encoder_dim != expected_dim:
            raise ValueError("encoder output dimension does not match model artifacts")
        self._use_fp16 = torch.cuda.is_available()

    def encode_texts(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.artifacts.embedding_dim), dtype=np.float32)
        kw: dict[str, Any] = {
            "batch_size": self.batch_size,
            "show_progress_bar": False,
            "convert_to_numpy": True,
            "normalize_embeddings": True,
        }
        if self._use_fp16:
            with torch.amp.autocast("cuda"):
                emb = self.encoder.encode(texts, **kw)
        else:
            emb = self.encoder.encode(texts, **kw)
        # sentence-transformers 是动态导入，运行时已要求 convert_to_numpy=True。
        return cast(np.ndarray, emb.astype(np.float32))

    def _build_features(self, embeddings: np.ndarray) -> np.ndarray:
        a            = self.artifacts
        matrix       = np.asarray(embeddings)
        expected_dim = int(np.asarray(a.interest_centers).shape[1])
        if matrix.ndim != 2 or matrix.shape[1] != expected_dim:
            raise ValueError(f"embeddings must have shape (n, {expected_dim})")
        if not np.issubdtype(matrix.dtype, np.number) or not np.isfinite(matrix).all():
            raise ValueError("embeddings must contain only finite numeric values")
        sims = matrix @ a.interest_centers.T

        sorted_sims = np.sort(sims, axis=1)
        top2 = sorted_sims[:, -2] if sims.shape[1] > 1 else sorted_sims[:, -1]

        probs = stable_softmax(sims, axis=1)
        entropy = -np.sum(probs * np.log(probs + 1e-12), axis=1)

        sim_pos = (matrix @ a.pos_centroid.reshape(-1, 1)).ravel()
        sim_neg = (
            (matrix @ a.neg_centroid.reshape(-1, 1)).ravel()
            if a.neg_centroid is not None
            else np.zeros(len(embeddings), dtype=np.float32)
        )

        return cast(
            np.ndarray,
            np.column_stack(
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
                    sim_pos - sim_neg,  # 离正样本重心比负样本重心近多少
                ]
            ).astype(np.float32),
        )

    def predict_proba(self, df: pd.DataFrame, input_mode: str = "title_abstract") -> np.ndarray:
        """返回每篇论文的正类概率 (1-d array)。"""
        if len(df) == 0:
            return cast(np.ndarray, np.array([], dtype=np.float32))
        if input_mode not in {"title_only", "title_abstract"}:
            raise ValueError("unsupported input_mode")

        # 解析列名：优先用训练时的列名，fallback 到常见列名
        cols         = self.artifacts.columns
        title_col    = resolve_dataframe_column(df, cols.get("title"), ("Title", "title"))
        abstract_col = None
        if input_mode != "title_only":
            abstract_col = resolve_dataframe_column(
                df,
                cols.get("abstract"),
                ("Abstract", "abstract"),
            )
            if abstract_col is None:
                raise ValueError("title_abstract input requires an abstract column")

        texts      = build_paper_texts(df, title_col, abstract_col)
        embeddings = self.encode_texts(texts)
        X          = self._build_features(embeddings)
        # sklearn 的运行时返回是 ndarray，但其泛型在当前 stubs 中退化为 Any。
        return cast(np.ndarray, self.artifacts.classifier.predict_proba(X)[:, 1])


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
    tcfg          = load_training_config(params.model_path)
    runtime_path  = resolve_multi_interest_model_path(params.model_path, tcfg)
    resolved_path = str(Path(runtime_path).resolve())
    cache_key     = (
        resolved_path,
        resolve_artifact_fingerprint(params, resolved_path),
        int(params.batch_size),
    )
    model = _MODEL_CACHE.get(cache_key)
    if model is None:
        model = MultiInterestInferenceModel(runtime_path, batch_size=params.batch_size)
        for stale_key in [key for key in _MODEL_CACHE if key[0] == resolved_path]:
            _MODEL_CACHE.pop(stale_key, None)
        _MODEL_CACHE[cache_key] = model
    proba = model.predict_proba(data, input_mode=params.input_mode)

    probs = proba.astype(float).tolist()
    preds = [1 if p >= params.threshold else 0 for p in probs]
    return probs, preds
