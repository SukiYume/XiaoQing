"""训练与推理共用的数值助手：稳定 softmax 与多兴趣特征构造。"""

from __future__ import annotations

from typing import cast

import numpy as np

# 兴趣相似度之外的特征列，顺序就是分类器看到的列顺序。训练和推理从这里取同一份，
# 任何一边都不能自己改顺序。
_SUMMARY_FEATURE_NAMES = (
    "max_sim",
    "mean_sim",
    "std_sim",
    "min_sim",
    "top1_top2_margin",
    "entropy",
    "concentration",
    "sim_to_pos_centroid",
    "sim_to_neg_centroid",
    "contrast_pos_neg",  # sim_pos − sim_neg：离正样本重心比负样本重心近多少
)


def stable_softmax(x: np.ndarray, axis: int = 1) -> np.ndarray:
    """先减去轴向最大值再计算 softmax，避免指数溢出。"""

    shifted = x - np.max(x, axis=axis, keepdims=True)
    exponentials = np.exp(shifted)
    return cast(
        np.ndarray,
        exponentials / (np.sum(exponentials, axis=axis, keepdims=True) + 1e-12),
    )


def interest_feature_names(n_interests: int) -> list[str]:
    """多兴趣分类器的特征列名：每个兴趣中心一列相似度，再接汇总特征。"""

    return [f"sim_interest_{i}" for i in range(n_interests)] + list(_SUMMARY_FEATURE_NAMES)


def build_interest_features(
    embeddings: np.ndarray,
    interest_centers: np.ndarray,
    pos_centroid: np.ndarray,
    neg_centroid: np.ndarray | None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """把句向量变成多兴趣分类器的特征矩阵。

    返回 ``(特征矩阵, 最近兴趣编号, 最大兴趣相似度)``，特征列顺序见
    ``interest_feature_names``。
    """

    centers = np.asarray(interest_centers)
    if centers.ndim != 2 or centers.shape[0] == 0 or centers.shape[1] == 0:
        raise ValueError("interest_centers must be a non-empty two-dimensional matrix")
    dimension = centers.shape[1]
    matrix    = np.asarray(embeddings)
    if matrix.ndim != 2 or matrix.shape[1] != dimension:
        raise ValueError(f"embeddings must have shape (n, {dimension})")
    if not np.issubdtype(matrix.dtype, np.number) or not np.isfinite(matrix).all():
        raise ValueError("embeddings must contain only finite numeric values")
    if np.asarray(pos_centroid).shape != (dimension,):
        raise ValueError("pos_centroid dimension does not match embeddings")
    if neg_centroid is not None and np.asarray(neg_centroid).shape != (dimension,):
        raise ValueError("neg_centroid dimension does not match embeddings")

    sims = matrix @ centers.T
    max_sim = sims.max(axis=1)
    best_interest = sims.argmax(axis=1)

    sorted_sims = np.sort(sims, axis=1)
    top2 = sorted_sims[:, -2] if sims.shape[1] > 1 else sorted_sims[:, -1]

    probs = stable_softmax(sims, axis=1)
    entropy = -np.sum(probs * np.log(probs + 1e-12), axis=1)

    sim_pos = (matrix @ pos_centroid.reshape(-1, 1)).ravel()
    sim_neg = (
        (matrix @ neg_centroid.reshape(-1, 1)).ravel()
        if neg_centroid is not None
        else np.zeros(len(matrix), dtype=np.float32)
    )

    features = np.column_stack(
        [
            sims,
            max_sim,
            sims.mean(axis=1),
            sims.std(axis=1),
            sims.min(axis=1),
            sorted_sims[:, -1] - top2,
            entropy,
            probs.max(axis=1),
            sim_pos,
            sim_neg,
            sim_pos - sim_neg,
        ]
    ).astype(np.float32)
    return cast(np.ndarray, features), best_interest, max_sim


__all__ = ["build_interest_features", "interest_feature_names", "stable_softmax"]
