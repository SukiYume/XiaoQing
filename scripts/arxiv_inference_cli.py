#!/usr/bin/env python
"""Run the repository-only arXiv model inference CLI.

Examples:
  python scripts/arxiv_inference_cli.py
  python scripts/arxiv_inference_cli.py --test-positive
  python scripts/arxiv_inference_cli.py --model-path C:\\models\\xiaoqing-arxiv

The trained model is an external runtime asset and is not published in the
XiaoQing wheel or source distribution.  ``--model-path`` takes precedence over
``ARXIV_MODEL_PATH`` and the plugin configuration.
"""

from __future__ import annotations

import argparse
import importlib
import logging
import sys
from pathlib import Path


def _load_inference():
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    return importlib.import_module("plugins.arxiv_filter.arxiv_inference")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")

    parser = argparse.ArgumentParser(description="arXiv paper inference CLI")
    parser.add_argument("--model-path", default=None, help="external model directory")
    parser.add_argument("--threshold", type=float, default=None, help="classification threshold")
    parser.add_argument("--batch-size", type=int, default=None, help="inference batch size")
    parser.add_argument("--max-len", type=int, default=None, help="maximum token length")
    parser.add_argument("--output", default="inference_output.csv", help="full result CSV path")
    parser.add_argument("--test-positive", action="store_true", help="run a positive sanity check")
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
    args = parser.parse_args()

    inference = _load_inference()
    if args.test_positive:
        probabilities, predictions, params = inference.run_single_paper_inference(
            title=args.test_title,
            abstract=args.test_abstract,
            model_path=args.model_path,
            threshold=args.threshold,
            batch_size=args.batch_size,
            max_len=args.max_len,
        )
        print(
            f"\n[Sanity Check] model_type={params.model_type}, "
            f"input_mode={params.input_mode}, threshold={params.threshold:.4f}"
        )
        print(f"Title    : {args.test_title}")
        print(f"Prob     : {probabilities[0]:.4f}")
        print(f"Predicted: {'POSITIVE' if predictions[0] == 1 else 'NEGATIVE'}")
        return

    data, result = inference.run_inference_for_today(
        model_path=args.model_path,
        threshold=args.threshold,
        batch_size=args.batch_size,
        max_len=args.max_len,
    )
    if data is None:
        print(result)
        return

    data.to_csv(args.output, index=False, encoding="utf-8-sig")
    print(f"[CSV saved] {len(data)} papers -> {args.output}")
    print(f"[Threshold] {result:.4f}  Positives: {data['Prediction'].sum()} / {len(data)}")

    positives = inference.select_positives(data)
    if len(positives) == 0:
        print("No positive predictions found.")
    else:
        print(inference.format_positives(positives))


if __name__ == "__main__":
    main()
