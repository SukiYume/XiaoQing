#!/usr/bin/env python
"""
推理模块公共配置与工具。

职责：
- 解析模型路径（支持自动搜索 best_model* 目录）
- 加载 training_config.json
- 根据 model_type / input_mode 构建统一的 InferenceParams
"""

import hashlib
import json
import logging
import math
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from ..utils import load_plugin_config

logger = logging.getLogger(__name__)

_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CONTENT_HASH_LIMIT_BYTES = 1024 * 1024
_NON_RUNTIME_ARTIFACT_NAMES = {"validation_scored.csv"}
_NON_RUNTIME_ARTIFACT_DIRS = {"emb_cache", "__pycache__"}


# =============================================================================
# 数据类
# =============================================================================


@dataclass(frozen=True, slots=True)
class InferenceParams:
    model_path: str
    threshold: float
    batch_size: int
    max_len: int
    input_mode: str = "title_abstract"  # "title_only" | "title_abstract"
    model_type: str = "transformers"  # "transformers" | "multi_interest" | "knn"
    artifact_fingerprint: str | None = None

    def __post_init__(self) -> None:
        """在进入任一推理后端前统一拦截非法运行参数。"""

        if not isinstance(self.model_path, str) or not self.model_path.strip():
            raise ValueError("model_path must be a non-empty string")
        if isinstance(self.threshold, bool) or not isinstance(self.threshold, (int, float)):
            raise TypeError("threshold must be a finite number")
        if not math.isfinite(float(self.threshold)):
            raise ValueError("threshold must be a finite number")
        for field_name in ("batch_size", "max_len"):
            value = getattr(self, field_name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{field_name} must be a positive integer")
        if self.input_mode not in {"title_only", "title_abstract"}:
            raise ValueError("input_mode must be 'title_only' or 'title_abstract'")
        if self.model_type not in {"transformers", "multi_interest", "knn"}:
            raise ValueError("unsupported model_type")
        if self.artifact_fingerprint is not None and (
            not isinstance(self.artifact_fingerprint, str) or not self.artifact_fingerprint.strip()
        ):
            raise ValueError("artifact_fingerprint must be a non-empty string or null")


def clean_paper_text(value: object) -> str:
    """把论文文本转成单行、无首尾空白的字符串。"""
    if pd.isna(value):
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def build_paper_texts(
    dataframe: pd.DataFrame,
    title_column: str | None,
    abstract_column: str | None,
) -> list[str]:
    """按模型约定拼接标题和摘要；标题列是所有后端的必需输入。"""
    if title_column is None or title_column not in dataframe.columns:
        raise ValueError("Input data must contain a title column")
    titles = dataframe[title_column].fillna("").astype(str).tolist()
    if abstract_column is None or abstract_column not in dataframe.columns:
        return [f"Title: {clean_paper_text(title)}" for title in titles]

    abstracts = dataframe[abstract_column].fillna("").astype(str).tolist()
    return [
        f"Title: {clean_paper_text(title)}\nAbstract: {clean_paper_text(abstract)}"
        for title, abstract in zip(titles, abstracts, strict=True)
    ]


def resolve_dataframe_column(
    dataframe: pd.DataFrame,
    trained_column: str | None,
    fallbacks: tuple[str, ...],
) -> str | None:
    """优先使用训练元数据中的列名，否则依次尝试通用列名。"""
    if trained_column and trained_column in dataframe.columns:
        return trained_column
    return next((column for column in fallbacks if column in dataframe.columns), None)


def model_artifact_fingerprint(model_path: str) -> str:
    """Return a cheap, deterministic fingerprint for a model artifact tree.

    Small files are content-hashed so configuration or lightweight artifact
    replacements cannot reuse an old in-process model.  Large weight files use
    their size and nanosecond mtime to avoid hashing multi-gigabyte checkpoints
    on every inference request.
    """

    root = Path(model_path).resolve()
    digest = hashlib.sha256()
    digest.update(str(root).encode("utf-8", errors="surrogatepass"))
    if not root.exists():
        digest.update(b"\0missing")
        return digest.hexdigest()

    paths = (
        [root]
        if root.is_file()
        else sorted(
            path
            for path in root.rglob("*")
            if path.is_file()
            and path.name not in _NON_RUNTIME_ARTIFACT_NAMES
            and not _NON_RUNTIME_ARTIFACT_DIRS.intersection(path.relative_to(root).parts)
        )
    )
    for path in paths:
        stat = path.stat()
        relative = path.name if root.is_file() else path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8", errors="surrogatepass"))
        digest.update(f"\0{stat.st_size}\0{stat.st_mtime_ns}\0".encode())
        if stat.st_size <= _CONTENT_HASH_LIMIT_BYTES:
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(128 * 1024), b""):
                    digest.update(chunk)
    return digest.hexdigest()


def resolve_artifact_fingerprint(
    params: InferenceParams,
    runtime_model_path: str | None = None,
) -> str:
    """复用入口已计算的指纹；独立后端调用才自行扫描模型目录。"""

    if params.artifact_fingerprint is not None:
        return params.artifact_fingerprint
    return model_artifact_fingerprint(runtime_model_path or params.model_path)


# =============================================================================
# 模型路径解析
# =============================================================================


def _join_plugin_path(path: str) -> str:
    return path if os.path.isabs(path) else os.path.join(_PLUGIN_DIR, path)


def _non_empty_path(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _mapping(value: object, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be a JSON object")
    return value


def _finite_float(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be a finite number")
    resolved = float(value)
    if not math.isfinite(resolved):
        raise ValueError(f"{field_name} must be a finite number")
    return resolved


def _positive_int(value: object, field_name: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer")
    return value


def resolve_model_path(model_path: str | None = None) -> str:
    """解析并返回模型目录的绝对路径。

    优先级：
    1. 显式传入的 model_path
    2. ARXIV_MODEL_PATH 环境变量
    3. config.json 中显式设置的 model.path
    4. 未设置 model.path 时按固定兼容顺序搜索 best_model* 目录
    """
    config = _mapping(load_plugin_config(), "plugin config")
    model_config = _mapping(config.get("model", {}), "model config")
    configured_value = model_config.get("path")
    configured = (
        _non_empty_path(configured_value, "model.path")
        if configured_value is not None
        else "best_model"
    )
    environment_path = os.environ.get("ARXIV_MODEL_PATH", "").strip() or None
    if model_path is not None:
        return _join_plugin_path(_non_empty_path(model_path, "model_path"))
    if environment_path:
        return _join_plugin_path(environment_path)

    if configured_value is not None:
        resolved = _join_plugin_path(configured)
        if not os.path.isdir(resolved):
            logger.error("Configured model path is unavailable: model.path")
        return resolved

    candidates = [
        "best_model",
        "best_model_interest",
        "best_model_knn",
        "best_model_title",
    ]

    seen: set[str] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        resolved = _join_plugin_path(candidate)
        if os.path.isdir(resolved):
            return resolved

    # 万一都找不到，返回 configured（让后续报错更清晰）
    return _join_plugin_path(configured)


# =============================================================================
# training_config.json 加载与检测
# =============================================================================


def load_training_config(model_path: str) -> dict[str, Any]:
    """加载模型目录中的 training_config.json，不存在返回空字典。"""
    cfg_file = os.path.join(model_path, "training_config.json")
    if not os.path.exists(cfg_file):
        return {}
    with open(cfg_file, encoding="utf-8") as f:
        payload = json.load(f)
    if not isinstance(payload, dict):
        raise ValueError("training_config.json must contain a JSON object")
    logger.info("Loaded training config from %s", cfg_file)
    return payload


def detect_model_type(training_config: Mapping[str, Any]) -> str:
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


def resolve_multi_interest_model_path(
    model_path: str,
    training_config: Mapping[str, Any],
) -> str:
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
    *,
    artifact_fingerprint: str | None = None,
) -> InferenceParams:
    """解析所有推理参数，自动从 training_config / plugin config 读取默认值。"""
    plugin_config = _mapping(load_plugin_config(), "plugin config")
    plugin_cfg = _mapping(plugin_config.get("model", {}), "model config")
    resolved_path = resolve_model_path(model_path)
    tcfg = load_training_config(resolved_path)

    model_type = detect_model_type(tcfg)
    input_mode = tcfg.get("input_mode", "title_abstract")
    threshold_value = (
        threshold
        if threshold is not None
        else tcfg.get("optimal_threshold", plugin_cfg.get("threshold", 0.5))
    )
    batch_size_value = batch_size if batch_size is not None else plugin_cfg.get("batch_size", 32)
    max_len_value = (
        max_len if max_len is not None else tcfg.get("max_len", plugin_cfg.get("max_len", 512))
    )

    return InferenceParams(
        model_path=resolved_path,
        threshold=_finite_float(threshold_value, "threshold"),
        batch_size=_positive_int(batch_size_value, "batch_size"),
        max_len=_positive_int(max_len_value, "max_len"),
        input_mode=input_mode if isinstance(input_mode, str) else "",
        model_type=model_type,
        artifact_fingerprint=artifact_fingerprint,
    )
