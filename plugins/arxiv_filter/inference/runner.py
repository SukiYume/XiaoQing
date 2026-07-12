#!/usr/bin/env python
"""
推理统一入口。

职责：
- 根据 model_type 自动分发到 transformers / multi_interest 后端
- 统一输入输出格式
- 提供便捷函数给 main.py / arxiv_test.py 调用
"""

import logging
from typing import Optional, cast

import pandas as pd
import torch

from .shared import InferenceParams, resolve_params

logger = logging.getLogger(__name__)


# =============================================================================
# 分发推理
# =============================================================================


def _dispatch_inference(
    params: InferenceParams,
    data: pd.DataFrame,
) -> tuple[list[float], list[int]]:
    """根据 params.model_type 调用对应的后端。"""
    logger.info(
        "推理参数: model=%s, type=%s, input_mode=%s, threshold=%.4f, batch=%d",
        params.model_path,
        params.model_type,
        params.input_mode,
        params.threshold,
        params.batch_size,
    )

    if params.model_type == "knn":
        from .knn_backend import run_knn_inference

        logger.info("model_type=knn: 使用 k-NN 兴趣库推理")
        return run_knn_inference(params, data)
    elif params.model_type == "multi_interest":
        from .multi_interest_backend import run_multi_interest_inference

        logger.info("model_type=multi_interest: 使用多兴趣模型推理")
        return run_multi_interest_inference(params, data)
    else:
        from .transformers_backend import run_transformers_inference

        logger.info("model_type=transformers: 使用 BERT 分类推理")
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        return run_transformers_inference(params, data, device)


# =============================================================================
# 输出处理
# =============================================================================


def select_positives(data: pd.DataFrame) -> pd.DataFrame:
    return cast(pd.DataFrame, data.loc[data["Prediction"] == 1].reset_index(drop=True).copy())


def format_positives(positives: pd.DataFrame) -> str:
    lines = []
    for i, (_, row) in enumerate(positives.iterrows(), 1):
        lines.append(f"\n----- Positive #{i} -----")
        lines.append(f"Title      : {row['Title']}")
        if "arXiv ID" in positives.columns:
            arxiv_id = str(row["arXiv ID"]).split("v")[0]
            lines.append(f"Link       : https://arxiv.org/abs/{arxiv_id}")
        lines.append(f"Probability: {row['Probability']:.4f}")
    return "\n".join(lines)


# =============================================================================
# 高层 API
# =============================================================================


def run_inference_for_dataframe(
    data: pd.DataFrame,
    model_path: Optional[str] = None,
    threshold: Optional[float] = None,
    batch_size: Optional[int] = None,
    max_len: Optional[int] = None,
) -> tuple[pd.DataFrame, float] | tuple[None, str]:
    """对给定的 DataFrame 执行推理，返回 (带 Probability/Prediction 列的 df, threshold) 或 (None, error_msg)。"""
    params = resolve_params(model_path, threshold, batch_size, max_len)

    if data.empty:
        return None, "No papers found."

    # This is an internal API without a request context.  Unexpected backend
    # failures must retain their traceback so the public plugin boundary can
    # produce one redacted, request-scoped response.
    probs, preds = _dispatch_inference(params, data)

    output = data.copy()
    output["Probability"] = probs
    output["Prediction"] = preds
    return output, params.threshold


def run_single_paper_inference(
    title: str,
    abstract: str,
    model_path: Optional[str] = None,
    threshold: Optional[float] = None,
    batch_size: Optional[int] = None,
    max_len: Optional[int] = None,
) -> tuple[list[float], list[int], InferenceParams]:
    """对单篇论文执行推理，返回 (probs, preds, params)。"""
    params = resolve_params(model_path, threshold, batch_size, max_len)
    sample = pd.DataFrame([{"Title": title, "Abstract": abstract}])
    probs, preds = _dispatch_inference(params, sample)
    return probs, preds, params


def run_inference_for_today(
    model_path: Optional[str] = None,
    threshold: Optional[float] = None,
    batch_size: Optional[int] = None,
    max_len: Optional[int] = None,
) -> tuple[pd.DataFrame, float] | tuple[None, str]:
    """获取今日 arXiv 论文并执行推理。"""
    from ..arxiv_today import get_today_arxiv

    data = get_today_arxiv()

    return run_inference_for_dataframe(data, model_path, threshold, batch_size, max_len)


def get_positive_arxiv_today_as_string(
    model_path: Optional[str] = None,
    threshold: Optional[float] = None,
    batch_size: Optional[int] = None,
    max_len: Optional[int] = None,
) -> str:
    """获取今日正预测论文的格式化字符串。被 main.py 调用。"""
    data, result = run_inference_for_today(model_path, threshold, batch_size, max_len)
    if data is None:
        return str(result)
    positives = select_positives(data)
    if len(positives) == 0:
        return "No positive predictions found."
    return format_positives(positives)
