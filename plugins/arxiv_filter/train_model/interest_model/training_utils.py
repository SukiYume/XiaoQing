"""Shared data preparation and metric helpers for interest-model training."""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Collection, Mapping, Sequence
from typing import cast

import numpy as np
import pandas as pd
from sklearn.metrics import (
    average_precision_score,
    classification_report,
    f1_score,
    roc_auc_score,
)
from sklearn.model_selection import train_test_split

# 文本拼接、编码器和打分都用线上推理的那一份：训练和推理看到的输入、算出的分数才一致。
from ...inference.shared import build_paper_texts as build_title_abstract_texts
from ...inference.shared import clean_paper_text
from ...inference.shared import encode_normalized as encode_normalized
from ...inference.shared import knn_scores as knn_scores
from ...inference.shared import load_sentence_encoder as load_sentence_encoder
from ..training_common import (
    best_fbeta_threshold,
    coerce_binary_labels,
    read_training_csv,
    seed_python_numpy,
    timestamp_log,
)


def require_encoder_name(value: object) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("encoder_name must be a non-empty string")


def require_int(name: str, value: object, minimum: int = 1) -> None:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")


def require_finite(name: str, value: object, *, positive: bool = False) -> None:
    """要求有限数（排除 bool）：``positive`` 为真时必须大于 0，否则只要不小于 0。"""

    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    if value < 0 or (positive and value == 0):
        raise ValueError(
            f"{name} must be a finite {'positive' if positive else 'non-negative'} number"
        )


def resolve_columns(
    frame: pd.DataFrame,
    candidates_by_field: Mapping[str, Sequence[str]],
    *,
    required_fields: Collection[str],
    error_prefix: str,
) -> dict[str, str | None]:
    """Resolve canonical field names from an ordered set of CSV aliases."""

    normalized = {
        re.sub(r"[^a-z0-9]", "", str(column).strip().lower()): column for column in frame.columns
    }
    resolved: dict[str, str | None] = {}
    for field, candidates in candidates_by_field.items():
        candidate_list = list(candidates)
        match = next((normalized[item] for item in candidate_list if item in normalized), None)
        if match is None and field in required_fields:
            raise ValueError(
                f"{error_prefix}。现有列：{list(frame.columns)}，候选：{candidate_list}"
            )
        resolved[field] = match
    return resolved


def build_paired_texts(
    frame: pd.DataFrame,
    title_column: str,
    abstract_column: str,
    template: str,
) -> list[str]:
    """Build normalized title/abstract strings using a caller-owned template."""

    titles    = frame[title_column].fillna("").astype(str).tolist()
    abstracts = frame[abstract_column].fillna("").astype(str).tolist()
    return [
        template.format(t=clean_paper_text(title), a=clean_paper_text(abstract)).strip()
        for title, abstract in zip(titles, abstracts, strict=True)
    ]


def validate_embedding_matrix(
    value: object,
    *,
    expected_rows: int,
    expected_dim: int | None = None,
    name: str                = "embeddings",
) -> np.ndarray:
    """验证预计算 embedding 的行数、维度和有限性，并统一为 float32。"""

    matrix = np.asarray(value)
    if matrix.ndim != 2 or matrix.shape[0] != expected_rows:
        raise ValueError(f"{name} must have {expected_rows} rows and be two-dimensional")
    if expected_dim is not None and matrix.shape[1] != expected_dim:
        raise ValueError(f"{name} must have embedding dimension {expected_dim}")
    if matrix.shape[1] == 0:
        raise ValueError(f"{name} must have at least one embedding dimension")
    if not np.issubdtype(matrix.dtype, np.number) or not np.isfinite(matrix).all():
        raise ValueError(f"{name} must contain only finite numeric values")
    return cast(np.ndarray, matrix.astype(np.float32, copy=False))


def fbeta_at_threshold(
    y_true: np.ndarray,
    y_score: np.ndarray,
    threshold: float,
    beta: float = 1.0,
) -> float:
    """按实际部署阈值（score >= threshold 为正）计算 F-beta。"""

    labels      = np.asarray(y_true).astype(int)
    predicted   = (np.asarray(y_score) >= threshold).astype(int)
    tp          = int(((predicted == 1) & (labels == 1)).sum())
    fp          = int(((predicted == 1) & (labels == 0)).sum())
    fn          = int(((predicted == 0) & (labels == 1)).sum())
    beta_sq     = beta * beta
    denominator = (1 + beta_sq) * tp + beta_sq * fn + fp
    return 0.0 if denominator == 0 else (1 + beta_sq) * tp / denominator


def report_interest_metrics(
    y_true: np.ndarray,
    y_score: np.ndarray,
    y_pred: np.ndarray,
    *,
    threshold: float,
    name: str,
) -> dict[str, float]:
    """兴趣模型的评估指标：计算、打印并返回（k-NN 与多兴趣训练共用）。"""

    has_both                  = len(np.unique(y_true)) >= 2
    metrics: dict[str, float] = {
        "roc_auc": float(roc_auc_score(y_true, y_score)) if has_both else float("nan"),
        "pr_auc": float(average_precision_score(y_true, y_score)) if has_both else float("nan"),
        "f1": float(f1_score(y_true, y_pred, zero_division=0)),
        "precision_at_5": precision_at_k(y_true, y_score, 5),
        "precision_at_10": precision_at_k(y_true, y_score, 10),
        "precision_at_20": precision_at_k(y_true, y_score, 20),
        "threshold": float(threshold),
    }

    print(f"\n{'=' * 72}\n[{name}] Metrics\n{'=' * 72}")
    for key, value in metrics.items():
        text = "nan" if np.isnan(value) else f"{value:.4f}"
        print(f"  {key:<22}: {text}")
    print(f"\n{'=' * 72}\n[{name}] Classification Report\n{'=' * 72}")
    print(
        classification_report(
            y_true,
            y_pred,
            labels        = [0, 1],
            target_names  = ["negative", "positive"],
            digits        = 4,
            zero_division = 0,
        )
    )
    return metrics


def precision_at_k(y_true: np.ndarray, y_score: np.ndarray, k: int) -> float:
    """Return precision among the highest-scoring ``k`` rows."""

    if type(k) is not int:
        raise ValueError("k must be an integer")
    labels = np.asarray(y_true)
    scores = np.asarray(y_score)
    if labels.ndim != 1 or scores.ndim != 1 or len(labels) != len(scores):
        raise ValueError("y_true and y_score must be one-dimensional arrays of equal length")
    if not np.isfinite(labels).all() or not np.isfinite(scores).all():
        raise ValueError("y_true and y_score must contain only finite values")
    bounded_k = min(k, len(labels))
    if bounded_k <= 0:
        return 0.0
    return float(np.mean(labels[np.argsort(-scores)[:bounded_k]]))


def split_dataframe(
    frame: pd.DataFrame,
    val_size: float,
    split_mode: str,
    *,
    seed: int                  = 42,
    date_col: str | None       = None,
    label_col: str | None      = None,
    log: Callable[[str], None] = timestamp_log,
    missing_date_error: str,
    stratify_fallback_message: str,
    invalid_dates_message: str | None = None,
    empty_training_error: str | None  = None,
    unknown_mode_error: str | None    = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Create a time-ordered or deterministic random interest-model split."""

    if (
        isinstance(val_size, bool)
        or not isinstance(val_size, (int, float))
        or not math.isfinite(float(val_size))
        or not 0 < float(val_size) < 1
    ):
        raise ValueError("val_size must be a finite number between 0 and 1")
    if len(frame) < 2:
        raise ValueError("at least two rows are required to split a dataset")
    if split_mode not in {"time", "random"}:
        message = unknown_mode_error or "unknown split_mode: {mode}"
        raise ValueError(message.format(mode=split_mode))
    working = frame.copy()
    if split_mode == "time":
        if not date_col or date_col not in working.columns:
            raise ValueError(missing_date_error)
        datetimes = pd.to_datetime(working[date_col], errors="coerce")
        invalid_count = int(datetimes.isna().sum())
        if invalid_count and invalid_dates_message is not None:
            log(invalid_dates_message.format(count=invalid_count))
        working["_dt"] = datetimes
        # 无法解析的日期排在最前面进入训练集，验证集只由已知最新时间的样本组成。
        working = working.sort_values("_dt", na_position="first", kind="stable").reset_index(
            drop=True
        )
        validation_count = max(1, int(round(len(working) * val_size)))
        training_count   = len(working) - validation_count
        if training_count <= 0:
            raise ValueError(empty_training_error or "validation split leaves no training rows")
        train_frame = working.iloc[:training_count].drop(columns=["_dt"]).reset_index(drop=True)
        validation_frame = (
            working.iloc[training_count:].drop(columns=["_dt"]).reset_index(drop=True)
        )
        return train_frame, validation_frame

    if label_col is not None:
        if label_col not in working.columns:
            raise ValueError(f"label column not found: {label_col}")
        try:
            train_frame, validation_frame = train_test_split(
                working,
                test_size    = val_size,
                stratify     = working[label_col],
                random_state = seed,
            )
            return train_frame.reset_index(drop=True), validation_frame.reset_index(drop=True)
        except ValueError:
            log(stratify_fallback_message)
    train_frame, validation_frame = train_test_split(
        working,
        test_size    = val_size,
        random_state = seed,
    )
    return train_frame.reset_index(drop=True), validation_frame.reset_index(drop=True)


__all__ = [
    "best_fbeta_threshold",
    "build_paired_texts",
    "build_title_abstract_texts",
    "coerce_binary_labels",
    "encode_normalized",
    "fbeta_at_threshold",
    "knn_scores",
    "load_sentence_encoder",
    "precision_at_k",
    "read_training_csv",
    "report_interest_metrics",
    "require_encoder_name",
    "require_finite",
    "require_int",
    "resolve_columns",
    "seed_python_numpy",
    "split_dataframe",
    "timestamp_log",
    "validate_embedding_matrix",
]
