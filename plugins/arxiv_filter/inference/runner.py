#!/usr/bin/env python
"""
推理统一入口。

职责：
- 根据 model_type 自动分发到 transformers / multi_interest 后端
- 统一输入输出格式
- 提供便捷函数给 main.py / scripts/arxiv_inference_cli.py 调用
"""

import logging
import math
from typing import cast

import pandas as pd

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
        probabilities, predictions = run_knn_inference(params, data)
    elif params.model_type == "multi_interest":
        from .multi_interest_backend import run_multi_interest_inference

        logger.info("model_type=multi_interest: 使用多兴趣模型推理")
        probabilities, predictions = run_multi_interest_inference(params, data)
    else:
        import torch

        from .transformers_backend import run_transformers_inference

        logger.info("model_type=transformers: 使用 BERT 分类推理")
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        probabilities, predictions = run_transformers_inference(params, data, device)

    # 后端是独立实现，必须在统一入口核对行数和数值，避免 pandas 静默错位。
    expected_count = len(data)
    if len(probabilities) != expected_count or len(predictions) != expected_count:
        raise ValueError("inference backend returned a result count that does not match the input")
    if any(
        isinstance(probability, bool)
        or not isinstance(probability, (int, float))
        or not math.isfinite(float(probability))
        for probability in probabilities
    ):
        raise ValueError("inference backend returned a non-finite probability")
    if any(type(prediction) is not int or prediction not in {0, 1} for prediction in predictions):
        raise ValueError("inference backend returned a non-binary prediction")
    return [float(value) for value in probabilities], predictions


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
    model_path: str | None  = None,
    threshold: float | None = None,
    batch_size: int | None  = None,
    max_len: int | None     = None,
    *,
    artifact_fingerprint: str | None = None,
) -> tuple[pd.DataFrame, float] | tuple[None, str]:
    """对给定的 DataFrame 执行推理，返回 (带 Probability/Prediction 列的 df, threshold) 或 (None, error_msg)。"""
    params = resolve_params(
        model_path,
        threshold,
        batch_size,
        max_len,
        artifact_fingerprint=artifact_fingerprint,
    )

    if data.empty:
        return None, "No papers found."

    # 这里是没有请求上下文的内部 API。后端异常必须保留原始 traceback，交给
    # 插件公开边界统一生成脱敏且带 request_id 的错误响应。
    probs, preds = _dispatch_inference(params, data)

    output                = data.copy()
    output["Probability"] = probs
    output["Prediction"]  = preds
    return output, params.threshold


def run_single_paper_inference(
    title: str,
    abstract: str,
    model_path: str | None  = None,
    threshold: float | None = None,
    batch_size: int | None  = None,
    max_len: int | None     = None,
    *,
    artifact_fingerprint: str | None = None,
) -> tuple[list[float], list[int], InferenceParams]:
    """对单篇论文执行推理，返回 (probs, preds, params)。"""
    params = resolve_params(
        model_path,
        threshold,
        batch_size,
        max_len,
        artifact_fingerprint=artifact_fingerprint,
    )
    sample = pd.DataFrame([{"Title": title, "Abstract": abstract}])
    probs, preds = _dispatch_inference(params, sample)
    return probs, preds, params


def run_inference_for_today(
    model_path: str | None  = None,
    threshold: float | None = None,
    batch_size: int | None  = None,
    max_len: int | None     = None,
    *,
    artifact_fingerprint: str | None = None,
) -> tuple[pd.DataFrame, float] | tuple[None, str]:
    """获取今日 arXiv 论文并执行推理。"""
    from ..arxiv_today import get_today_arxiv

    data = get_today_arxiv()

    return run_inference_for_dataframe(
        data,
        model_path,
        threshold,
        batch_size,
        max_len,
        artifact_fingerprint=artifact_fingerprint,
    )


def get_positive_arxiv_today_as_string(
    model_path: str | None  = None,
    threshold: float | None = None,
    batch_size: int | None  = None,
    max_len: int | None     = None,
    *,
    artifact_fingerprint: str | None = None,
) -> str:
    """获取今日正预测论文的格式化字符串。被 main.py 调用。"""
    data, result = run_inference_for_today(
        model_path,
        threshold,
        batch_size,
        max_len,
        artifact_fingerprint=artifact_fingerprint,
    )
    if data is None:
        return str(result)
    positives = select_positives(data)
    if len(positives) == 0:
        return "No positive predictions found."
    return format_positives(positives)
