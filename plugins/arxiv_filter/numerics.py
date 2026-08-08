"""训练与推理共用的数值稳定数组助手。"""

from __future__ import annotations

from typing import cast

import numpy as np


def stable_softmax(x: np.ndarray, axis: int = 1) -> np.ndarray:
    """先减去轴向最大值再计算 softmax，避免指数溢出。"""

    shifted = x - np.max(x, axis=axis, keepdims=True)
    exponentials = np.exp(shifted)
    return cast(
        np.ndarray,
        exponentials / (np.sum(exponentials, axis=axis, keepdims=True) + 1e-12),
    )


__all__ = ["stable_softmax"]
