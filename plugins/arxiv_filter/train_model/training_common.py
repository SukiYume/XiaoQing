"""所有 arXiv 训练入口共享的轻量、无深度学习依赖工具。"""

from __future__ import annotations

import random
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import cast

import numpy as np
import pandas as pd


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


__all__ = [
    "coerce_binary_labels",
    "read_training_csv",
    "seed_python_numpy",
    "timestamp_log",
]
