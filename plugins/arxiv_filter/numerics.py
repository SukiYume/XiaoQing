"""Numerically stable array helpers shared by training and inference."""

from __future__ import annotations

import numpy as np


def stable_softmax(x: np.ndarray, axis: int = 1) -> np.ndarray:
    """Compute softmax after max-shifting to avoid exponential overflow."""

    shifted = x - np.max(x, axis=axis, keepdims=True)
    exponentials = np.exp(shifted)
    return exponentials / (np.sum(exponentials, axis=axis, keepdims=True) + 1e-12)


__all__ = ["stable_softmax"]
