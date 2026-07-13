#!/usr/bin/env python
"""
arXiv 推理 facade — re-export runner 的公开 API。

main.py 通过 ``from .arxiv_inference import get_positive_arxiv_today_as_string``，
仓库工具 ``scripts/arxiv_inference_cli.py`` 通过公开 facade 使用。
"""

import importlib
import sys
from pathlib import Path

# ── 符号映射 ──────────────────────────────────────────────────────────────
_IMPORTS = {
    "runner": [
        "format_positives",
        "get_positive_arxiv_today_as_string",
        "run_inference_for_dataframe",
        "run_inference_for_today",
        "run_single_paper_inference",
        "select_positives",
    ],
    "shared": [
        "InferenceParams",
        "resolve_params",
    ],
}


def _import_all() -> dict:
    ns: dict = {}
    if __package__:
        for sub, names in _IMPORTS.items():
            mod = importlib.import_module(f".inference.{sub}", __package__)
            for name in names:
                ns[name] = getattr(mod, name)
    else:
        pkg_parent = Path(__file__).resolve().parent.parent
        if str(pkg_parent) not in sys.path:
            sys.path.insert(0, str(pkg_parent))
        for sub, names in _IMPORTS.items():
            mod = importlib.import_module(f"arxiv_filter.inference.{sub}")
            for name in names:
                ns[name] = getattr(mod, name)
    return ns


globals().update(_import_all())

__all__ = [name for names in _IMPORTS.values() for name in names]
