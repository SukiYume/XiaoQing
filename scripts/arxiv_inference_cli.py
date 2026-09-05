#!/usr/bin/env python
"""在仓库中直接运行 arXiv 模型推理。

示例：
  python scripts/arxiv_inference_cli.py
  python scripts/arxiv_inference_cli.py --test-positive
  python scripts/arxiv_inference_cli.py --model-path <model-dir>

训练权重是外部运行资产，不随 XiaoQing wheel 或源码发行。模型路径优先级为
``--model-path``、``ARXIV_MODEL_PATH``、插件配置。
"""

from __future__ import annotations

import argparse
import importlib
import logging
import math
import os
import sys
import tempfile
from pathlib import Path
from types import ModuleType
from typing import Any


def _load_inference() -> ModuleType:
    """从仓库根导入插件公开 facade，不依赖调用者的当前目录。"""

    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    return importlib.import_module("plugins.arxiv_filter.arxiv_inference")


def build_parser() -> argparse.ArgumentParser:
    """构造可被测试复用的命令行参数。"""

    parser = argparse.ArgumentParser(
        description     = __doc__,
        formatter_class = argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--model-path", help="外部模型目录")
    parser.add_argument("--threshold", type=float, help="分类阈值")
    parser.add_argument("--batch-size", type=int, help="推理批大小")
    parser.add_argument("--max-len", type=int, help="最大 token 长度")
    parser.add_argument(
        "--output",
        type    = Path,
        default = Path("inference_output.csv"),
        help    = "完整结果 CSV 路径",
    )
    parser.add_argument("--force", action="store_true", help="允许原子替换已有输出文件")
    parser.add_argument("--test-positive", action="store_true", help="只运行单篇论文冒烟")
    parser.add_argument(
        "--test-title",
        default="Diffusion Models for High-Resolution Image Synthesis",
    )
    parser.add_argument(
        "--test-abstract",
        default=(
            "We present a diffusion-based generative model that achieves "
            "state-of-the-art image synthesis quality on standard benchmarks. "
            "Our method uses a hierarchical denoising process with classifier-free "
            "guidance to produce photorealistic images at high resolution."
        ),
    )
    return parser


def _validate_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """在加载模型或访问网络前拦截确定性的参数错误。"""

    if args.model_path is not None and not args.model_path.strip():
        parser.error("--model-path 不能为空")
    if args.threshold is not None and not math.isfinite(args.threshold):
        parser.error("--threshold 必须是有限数")
    if args.batch_size is not None and args.batch_size <= 0:
        parser.error("--batch-size 必须是正整数")
    if args.max_len is not None and args.max_len <= 0:
        parser.error("--max-len 必须是正整数")
    if args.test_positive and not args.test_title.strip():
        parser.error("--test-title 不能为空")


def _prepare_output(parser: argparse.ArgumentParser, output: Path, *, force: bool) -> Path:
    """在联网前确认输出位置可用；默认不覆盖已有文件。"""

    output = output.resolve()
    if output.exists():
        if not output.is_file():
            parser.error(f"--output 不是普通文件路径: {output}")
        if not force:
            parser.error(f"--output 已存在，使用 --force 才可替换: {output}")
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        parser.error(f"无法创建输出目录: {type(exc).__name__}: {exc}")
    return output


def _write_csv_atomic(data: Any, output: Path) -> None:
    """先在目标目录写临时 CSV，完整成功后再原子发布。"""

    descriptor, temporary_name = tempfile.mkstemp(
        prefix = f".{output.name}.",
        suffix = ".tmp",
        dir    = output.parent,
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        data.to_csv(temporary, index=False, encoding="utf-8-sig")
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args   = parser.parse_args(argv)
    _validate_args(parser, args)
    output = None if args.test_positive else _prepare_output(parser, args.output, force=args.force)
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")

    inference = _load_inference()
    if args.test_positive:
        probabilities, predictions, params = inference.run_single_paper_inference(
            title      = args.test_title,
            abstract   = args.test_abstract,
            model_path = args.model_path,
            threshold  = args.threshold,
            batch_size = args.batch_size,
            max_len    = args.max_len,
        )
        print(
            f"\n[冒烟检查] model_type={params.model_type}, "
            f"input_mode={params.input_mode}, threshold={params.threshold:.4f}"
        )
        print(f"标题: {args.test_title}")
        print(f"概率: {probabilities[0]:.4f}")
        print(f"预测: {'POSITIVE' if predictions[0] == 1 else 'NEGATIVE'}")
        return 0

    data, result = inference.run_inference_for_today(
        model_path = args.model_path,
        threshold  = args.threshold,
        batch_size = args.batch_size,
        max_len    = args.max_len,
    )
    if data is None:
        print(result)
        return 0

    if output is None:  # 仅用于类型收窄；普通模式已在加载模型前准备输出路径。
        raise RuntimeError("output path was not prepared")
    _write_csv_atomic(data, output)
    print(f"[CSV 已保存] {len(data)} 篇 -> {output}")
    print(f"[阈值] {result:.4f}  正例: {data['Prediction'].sum()} / {len(data)}")

    positives = inference.select_positives(data)
    if len(positives) == 0:
        print("No positive predictions found.")
    else:
        print(inference.format_positives(positives))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
