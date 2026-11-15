"""所有 arXiv 训练入口共享的轻量、无深度学习依赖工具。"""

from __future__ import annotations

import math
import random
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import cast

import numpy as np
import pandas as pd
from sklearn.metrics import precision_recall_curve


def timestamp_log(message: str = "") -> None:
    """Print one timestamped, immediately flushed training log line."""

    print(f"{datetime.now().strftime('%H:%M:%S')}  {message}", flush=True)


def seed_python_numpy(seed: int) -> None:
    """统一设置 Python 与 NumPy 随机种子。"""

    if type(seed) is not int or not 0 <= seed < 2**32:
        raise ValueError("seed must be an integer in [0, 2**32)")
    random.seed(seed)
    np.random.seed(seed)


def coerce_binary_labels(values: Iterable[object]) -> np.ndarray:
    """把 CSV 标签转换为 0/1 整数，并拒绝布尔、空值和其他类别。"""

    try:
        raw_values = list(values)
    except TypeError as exc:
        raise ValueError("labels must be a one-dimensional sequence") from exc
    if not raw_values or any(isinstance(value, (bool, np.bool_)) for value in raw_values):
        raise ValueError("labels must contain only 0 and 1")
    try:
        numeric = np.asarray(raw_values, dtype=np.float64)
    except (TypeError, ValueError) as exc:
        raise ValueError("labels must contain only 0 and 1") from exc
    if numeric.ndim != 1 or not np.isfinite(numeric).all() or not np.isin(numeric, (0, 1)).all():
        raise ValueError("labels must contain only 0 and 1")
    return cast(np.ndarray, numeric.astype(np.int64))


def read_training_csv(path: str | Path) -> pd.DataFrame:
    """按字符串读取训练 CSV，保留 arXiv ID 中有意义的前导零。"""

    return pd.read_csv(path, dtype=str)


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


__all__ = [
    "best_fbeta_threshold",
    "coerce_binary_labels",
    "read_training_csv",
    "seed_python_numpy",
    "timestamp_log",
]
