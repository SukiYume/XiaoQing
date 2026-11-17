#!/usr/bin/env python
"""
多兴趣 (sentence-transformers) 模型推理后端。

输入：InferenceParams + DataFrame (含 Title 列，可选 Abstract 列)
输出：(probs: list[float], preds: list[int])
"""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import joblib
import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression

from ..numerics import build_interest_features, interest_feature_names
from .shared import (
    InferenceParams,
    encode_normalized,
    load_sentence_encoder,
    paper_texts_for_mode,
    positive_int,
    run_cached_embedding_model,
    validated_columns,
)

_MODEL_CACHE: dict[tuple[str, str, int, int], "MultiInterestInferenceModel"] = {}


# =============================================================================
# Artifacts
# =============================================================================


@dataclass(frozen=True, slots=True)
class ModelArtifacts:
    """``artifacts.joblib`` 的内容；训练脚本用同一个类写出，推理从这里读回。

    ``threshold`` 与 ``threshold_beta`` 只是训练时的记录，推理阈值来自
    ``training_config.json`` 的 ``optimal_threshold``。
    """

    encoder_name    : str
    n_interests     : int
    embedding_dim   : int
    interest_centers: np.ndarray
    pos_centroid    : np.ndarray
    neg_centroid    : np.ndarray | None
    classifier      : LogisticRegression
    threshold       : float
    threshold_beta  : float
    cluster_keywords: list[str]
    cluster_sizes   : list[int]
    cluster_examples: list[list[str]]
    feature_names   : list[str]
    columns         : dict[str, str | None]


# =============================================================================
# 推理模型
# =============================================================================


class MultiInterestInferenceModel:
    def __init__(self, model_dir: str, batch_size: int = 256, max_len: int | None = None):
        self.model_dir  = Path(model_dir)
        self.batch_size = positive_int(batch_size, "batch_size")

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
        expected_features = len(interest_feature_names(artifacts.n_interests))
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
        validated_columns(artifacts.columns)

        self.encoder = load_sentence_encoder(artifacts.encoder_name, max_len)
        encoder_dim  = int(self.encoder.get_sentence_embedding_dimension())
        if encoder_dim != expected_dim:
            raise ValueError("encoder output dimension does not match model artifacts")

    def encode_texts(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.artifacts.embedding_dim), dtype=np.float32)
        embeddings = encode_normalized(self.encoder, texts, self.batch_size)
        return cast(np.ndarray, embeddings.astype(np.float32))

    def predict_proba(self, df: pd.DataFrame, input_mode: str = "title_abstract") -> np.ndarray:
        """返回每篇论文的正类概率 (1-d array)。"""
        if len(df) == 0:
            return cast(np.ndarray, np.array([], dtype=np.float32))
        artifacts  = self.artifacts
        texts      = paper_texts_for_mode(df, artifacts.columns, input_mode)
        embeddings = self.encode_texts(texts)
        features, _, _ = build_interest_features(
            embeddings,
            artifacts.interest_centers,
            artifacts.pos_centroid,
            artifacts.neg_centroid,
        )
        # sklearn 的运行时返回是 ndarray，但其泛型在当前 stubs 中退化为 Any。
        return cast(np.ndarray, artifacts.classifier.predict_proba(features)[:, 1])


# =============================================================================
# 统一入口
# =============================================================================


def run_multi_interest_inference(
    params: InferenceParams,
    data: pd.DataFrame,
) -> tuple[list[float], list[int]]:
    """被 runner.py 调用，返回 (probs, preds)。"""

    return run_cached_embedding_model(params, data, _MODEL_CACHE, MultiInterestInferenceModel)
