#!/usr/bin/env python
"""
arXiv 推理 facade — re-export runner 的公开 API。

main.py 通过 ``from .arxiv_inference import get_positive_arxiv_today_as_string``，
仓库工具 ``scripts/arxiv_inference_cli.py`` 通过公开 facade 使用。
"""

from .inference.runner import (
    format_positives,
    get_positive_arxiv_today_as_string,
    run_inference_for_dataframe,
    run_inference_for_today,
    run_single_paper_inference,
    select_positives,
)
from .inference.shared import InferenceParams, resolve_params

__all__ = [
    "InferenceParams",
    "format_positives",
    "get_positive_arxiv_today_as_string",
    "resolve_params",
    "run_inference_for_dataframe",
    "run_inference_for_today",
    "run_single_paper_inference",
    "select_positives",
]
