#!/usr/bin/env python
"""
CLI 快速测试推理效果。

用法:
  python arxiv_test.py                      # 拉取今日论文并推理
  python arxiv_test.py --test-positive      # 用一篇假论文做 sanity-check
  python arxiv_test.py --model-path best_model_interest
"""

import argparse
import importlib
import logging
import sys
from pathlib import Path

import torch

def _load_inference():
    try:
        from . import arxiv_inference
        return arxiv_inference
    except ImportError:
        parent = Path(__file__).resolve().parent.parent
        if str(parent) not in sys.path:
            sys.path.insert(0, str(parent))
        return importlib.import_module("arxiv_filter.arxiv_inference")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")

    p = argparse.ArgumentParser(description="arXiv paper inference CLI")
    p.add_argument("--model-path", default=None, help="模型目录路径")
    p.add_argument("--threshold", type=float, default=None, help="分类阈值")
    p.add_argument("--batch-size", type=int, default=None, help="推理批大小")
    p.add_argument("--max-len", type=int, default=None, help="最大 token 长度")
    p.add_argument("--output", default="inference_output.csv", help="全量结果 CSV 路径")
    p.add_argument("--test-positive", action="store_true", help="用假论文做 sanity check")
    p.add_argument("--test-title", default="Diffusion Models for High-Resolution Image Synthesis")
    p.add_argument("--test-abstract", default=(
        "We present a diffusion-based generative model that achieves "
        "state-of-the-art image synthesis quality on standard benchmarks. "
        "Our method uses a hierarchical denoising process with classifier-free "
        "guidance to produce photorealistic images at high resolution."
    ))
    args = p.parse_args()

    inf = _load_inference()

    if args.test_positive:
        probs, preds, params = inf.run_single_paper_inference(
            title=args.test_title, abstract=args.test_abstract,
            model_path=args.model_path, threshold=args.threshold,
            batch_size=args.batch_size, max_len=args.max_len,
        )
        print(f"\n[Sanity Check] model_type={params.model_type}, input_mode={params.input_mode}, "
              f"threshold={params.threshold:.4f}")
        print(f"Title    : {args.test_title}")
        print(f"Prob     : {probs[0]:.4f}")
        print(f"Predicted: {'POSITIVE' if preds[0] == 1 else 'NEGATIVE'}")
        return

    data, result = inf.run_inference_for_today(
        model_path=args.model_path, threshold=args.threshold,
        batch_size=args.batch_size, max_len=args.max_len,
    )
    if data is None:
        print(result)
        return

    data.to_csv(args.output, index=False, encoding="utf-8-sig")
    print(f"[CSV saved] {len(data)} papers -> {args.output}")
    print(f"[Threshold] {result:.4f}  Positives: {data['Prediction'].sum()} / {len(data)}")

    positives = inf.select_positives(data)
    if len(positives) == 0:
        print("No positive predictions found.")
    else:
        print(inf.format_positives(positives))


if __name__ == "__main__":
    main()
