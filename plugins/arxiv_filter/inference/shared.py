#!/usr/bin/env python
"""
推理模块公共配置与工具。

职责：
- 解析模型路径（支持自动搜索 best_model* 目录）
- 加载 training_config.json
- 根据 model_type / input_mode 构建统一的 InferenceParams
"""

import json
import logging
import os
from dataclasses import dataclass

from ..utils import load_plugin_config

logger = logging.getLogger(__name__)

_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# =============================================================================
# 数据类
# =============================================================================


@dataclass(frozen=True)
class InferenceParams:
    model_path: str
    threshold: float
    batch_size: int
    max_len: int
    input_mode: str = "title_abstract"  # "title_only" | "title_abstract"
    model_type: str = "transformers"  # "transformers" | "multi_interest"


# =============================================================================
# 模型路径解析
# =============================================================================


def _join_plugin_path(path: str) -> str:
    return path if os.path.isabs(path) else os.path.join(_PLUGIN_DIR, path)


def resolve_model_path(model_path: str | None = None) -> str:
    """解析并返回模型目录的绝对路径。

    优先级：
    1. 显式传入的 model_path
    2. ARXIV_MODEL_PATH 环境变量
    3. config.json 中的 model.path
    4. 自动搜索 best_model* 目录（优先 best_model_interest）
    """
    config = load_plugin_config()
    configured = config.get("model", {}).get("path", "best_model")
    environment_path = os.environ.get("ARXIV_MODEL_PATH", "").strip() or None
    if model_path:
        return _join_plugin_path(model_path)
    if environment_path:
        return _join_plugin_path(environment_path)

    candidates = [configured]

    # 固定 fallback
    candidates += ["best_model", "best_model_interest", "best_model_abs", "best_model_title"]

    seen = set()
    for c in candidates:
        if c is None or c in seen:
            continue
        seen.add(c)
        resolved = _join_plugin_path(c)
        if os.path.isdir(resolved):
            if c != configured:
                logger.warning(
                    "Preferred model path '%s' not found; falling back to '%s'",
                    configured,
                    c,
                )
            return resolved

    # 万一都找不到，返回 configured（让后续报错更清晰）
    return _join_plugin_path(configured)


# =============================================================================
# training_config.json 加载与检测
# =============================================================================


def load_training_config(model_path: str) -> dict:
    """加载模型目录中的 training_config.json，不存在返回空字典。"""
    cfg_file = os.path.join(model_path, "training_config.json")
    if not os.path.exists(cfg_file):
        return {}
    with open(cfg_file, encoding="utf-8") as f:
        cfg = json.load(f)
    logger.info("Loaded training config from %s", cfg_file)
    return cfg


def detect_model_type(training_config: dict) -> str:
    """从 training_config 判断模型类型: 'multi_interest' | 'transformers'"""
    mt = str(training_config.get("model_type", "")).strip().lower()
    if mt == "knn" or mt.startswith("knn"):
        return "knn"
    if "multi_interest" in mt:
        return "multi_interest"
    tv = str(training_config.get("train_version", "")).strip().lower()
    if tv.startswith("knn"):
        return "knn"
    if tv.startswith("multi_interest"):
        return "multi_interest"
    return "transformers"


def resolve_multi_interest_model_path(model_path: str, training_config: dict) -> str:
    """多兴趣模型可能把 artifacts 放在子目录，从 training_config 中解析实际路径。"""
    for key in ("multi_interest_model_path", "runtime_model_path", "artifacts_path", "model_path"):
        val = training_config.get(key)
        if isinstance(val, str) and val.strip():
            if os.path.isabs(val):
                return val
            return os.path.normpath(os.path.join(model_path, val))
    return model_path


# =============================================================================
# 一站式参数解析
# =============================================================================


def resolve_params(
    model_path: str | None = None,
    threshold: float | None = None,
    batch_size: int | None = None,
    max_len: int | None = None,
) -> InferenceParams:
    """解析所有推理参数，自动从 training_config / plugin config 读取默认值。"""
    plugin_cfg = load_plugin_config().get("model", {})
    resolved_path = resolve_model_path(model_path)
    tcfg = load_training_config(resolved_path)

    model_type = detect_model_type(tcfg)
    input_mode = str(tcfg.get("input_mode", "title_abstract"))

    return InferenceParams(
        model_path=resolved_path,
        threshold=(
            threshold
            if threshold is not None
            else float(tcfg.get("optimal_threshold", plugin_cfg.get("threshold", 0.5)))
        ),
        batch_size=(
            batch_size if batch_size is not None else int(plugin_cfg.get("batch_size", 32))
        ),
        max_len=(
            max_len
            if max_len is not None
            else int(tcfg.get("max_len", plugin_cfg.get("max_len", 512)))
        ),
        input_mode=input_mode,
        model_type=model_type,
    )
