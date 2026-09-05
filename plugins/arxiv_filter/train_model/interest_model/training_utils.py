"""Shared data preparation and metric helpers for interest-model training."""

from __future__ import annotations

import importlib
import math
import re
from collections.abc import Callable, Collection, Mapping, Sequence
from typing import cast

import numpy as np
import pandas as pd
from sklearn.metrics import precision_recall_curve
from sklearn.model_selection import train_test_split

from ..training_common import (
    coerce_binary_labels,
    read_training_csv,
    seed_python_numpy,
    timestamp_log,
)


def load_sentence_transformer_class():
    """Load the optional sentence-transformers dependency only when needed."""

    return importlib.import_module("sentence_transformers").SentenceTransformer


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


def _clean_text(x: object) -> str:
    """Collapse whitespace and normalize pandas missing values to empty text."""

    if pd.isna(x):
        return ""
    return re.sub(r"\s+", " ", str(x)).strip()


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
        template.format(t=_clean_text(title), a=_clean_text(abstract)).strip()
        for title, abstract in zip(titles, abstracts, strict=True)
    ]


def build_title_abstract_texts(
    frame: pd.DataFrame,
    title_column: str,
    abstract_column: str,
) -> list[str]:
    """构建两个兴趣模型共用的标准标题+摘要输入。"""

    return build_paired_texts(
        frame,
        title_column,
        abstract_column,
        "Title: {t}\nAbstract: {a}",
    )


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


def best_fbeta_threshold(
    y_true: np.ndarray,
    y_score: np.ndarray,
    beta: float = 1.0,
) -> tuple[float, float]:
    """Return the threshold and score at the best finite PR-curve F-beta point."""

    if isinstance(beta, bool) or not isinstance(beta, (int, float)) or not math.isfinite(beta):
        raise ValueError("beta must be a finite positive number")
    if beta <= 0:
        raise ValueError("beta must be a finite positive number")
    labels       = np.asarray(y_true)
    scores_input = np.asarray(y_score)
    if labels.ndim != 1 or scores_input.ndim != 1 or len(labels) != len(scores_input):
        raise ValueError("y_true and y_score must be one-dimensional arrays of equal length")
    if not np.isfinite(labels).all() or not np.isfinite(scores_input).all():
        raise ValueError("y_true and y_score must contain only finite values")
    if not np.isin(labels, (0, 1)).all():
        raise ValueError("y_true must contain only 0 and 1")
    if len(np.unique(labels)) < 2:
        return 0.5, 0.0
    precision, recall, thresholds = precision_recall_curve(labels, scores_input)
    if len(thresholds) == 0:
        return 0.5, 0.0
    beta_squared = beta * beta
    scores       = ((1 + beta_squared) * precision[:-1] * recall[:-1]) / np.clip(
        (beta_squared * precision[:-1]) + recall[:-1],
        1e-12,
        None,
    )
    if np.all(np.isnan(scores)):
        return 0.5, 0.0
    best_index = int(np.nanargmax(scores))
    return float(thresholds[best_index]), float(scores[best_index])


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
        working = working.sort_values("_dt").reset_index(drop=True)
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
    "load_sentence_transformer_class",
    "precision_at_k",
    "read_training_csv",
    "resolve_columns",
    "seed_python_numpy",
    "split_dataframe",
    "timestamp_log",
    "validate_embedding_matrix",
]
