#!/usr/bin/env python
"""
arXiv 论文推理模块

对 arXiv 论文（标题+摘要）进行分类，筛选出感兴趣的论文。
动态 padding（与训练一致），max_len 从模型目录的 training_config.json 读取。
"""

import json
import logging
import os
import argparse
from dataclasses import dataclass
from functools import partial
from typing import Optional, cast

ScalarValue = int | float | str

import torch
import pandas as pd
import torch.nn.functional as F
from torch.utils.data import Dataset, DataLoader
from transformers import AutoTokenizer, AutoModelForSequenceClassification

from .utils import load_plugin_config

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class InferenceParams:
    model_path: str
    threshold: float
    batch_size: int
    max_len: int
    input_mode: str = "title_abstract"  # "title_only" | "title_abstract"


def _get_plugin_dir() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def _join_plugin_path(path: str) -> str:
    if os.path.isabs(path):
        return path
    return os.path.join(_get_plugin_dir(), path)


def resolve_model_path(model_path: Optional[str] = None) -> str:
    """解析模型目录；默认配置不存在时回退到已恢复的本地模型目录。"""
    if model_path is not None:
        return _join_plugin_path(model_path)

    config = load_plugin_config()
    model_config = config.get("model", {})
    configured_path = model_config.get("path", "best_model")
    candidates = [
        configured_path,
        "best_model",
        "train_model/best_model_title_abstract",
    ]

    for candidate in candidates:
        resolved = _join_plugin_path(candidate)
        if os.path.isdir(resolved):
            if candidate != configured_path:
                logger.warning(
                    "Configured model path '%s' not found; falling back to '%s'",
                    configured_path,
                    candidate,
                )
            return resolved

    return _join_plugin_path(configured_path)


def _load_model_and_tokenizer(model_path: str, device: torch.device):
    """加载模型和分词器"""
    model_path = resolve_model_path(model_path)

    if not os.path.exists(model_path):
        raise FileNotFoundError(f"Model path does not exist: {model_path}")
    if not os.path.isdir(model_path):
        raise ValueError(f"Model path must be a directory: {model_path}")

    model = AutoModelForSequenceClassification.from_pretrained(model_path)
    tokenizer = AutoTokenizer.from_pretrained(model_path)
    model.to(device)
    model.eval()

    return model, tokenizer


def _load_training_config(model_path: str) -> dict[str, object]:
    """
    从模型目录加载训练配置（含最优阈值、max_len 等超参数）。
    若文件不存在则返回空字典。
    """
    model_path = resolve_model_path(model_path)

    config_file = os.path.join(model_path, "training_config.json")
    if os.path.exists(config_file):
        with open(config_file, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        logger.info(f"Loaded training config from {config_file}: {cfg}")
        return cfg
    return {}


def _resolve_params(
    model_path: Optional[str] = None,
    threshold: Optional[float] = None,
    batch_size: Optional[int] = None,
    max_len: Optional[int] = None,
) -> InferenceParams:
    """
    统一解析推理参数。

    优先级:
    - model_path: 函数参数 > config.json > 已恢复的本地模型目录回退
    - threshold/max_len: 函数参数 > training_config.json > config.json > 默认值
    - batch_size: 函数参数 > config.json > 默认值
    """
    config = load_plugin_config()
    model_config = config.get("model", {})

    model_path = resolve_model_path(model_path)
    if batch_size is None:
        batch_size = int(model_config.get("batch_size", 32))

    training_cfg = _load_training_config(model_path)

    # max_len: 优先 training_config.json，确保与训练一致
    if max_len is None:
        max_len = int(
            cast(ScalarValue, training_cfg.get("max_len", model_config.get("max_len", 512)))
        )

    # threshold: 优先 training_config.json 的 optimal_threshold
    if threshold is None:
        threshold = float(
            cast(
                ScalarValue,
                training_cfg.get(
                    "optimal_threshold",
                    model_config.get("threshold", 0.5),
                ),
            )
        )

    # input_mode: 来自 training_config.json，默认 title_abstract 以兼容旧模型
    input_mode = str(training_cfg.get("input_mode", "title_abstract"))

    return InferenceParams(
        model_path=model_path,
        threshold=threshold,
        batch_size=batch_size,
        max_len=max_len,
        input_mode=input_mode,
    )


# ═══════════════════════════════════════════════════════════════════════
#  Dataset & Dynamic Padding（与训练 v3 一致）
# ═══════════════════════════════════════════════════════════════════════


class TitleAbstractDataset(Dataset[dict[str, list[int]]]):
    """
    论文标题+摘要数据集（动态 padding，与训练一致）。

    不在 __init__ 中做全局 padding，而是存储变长 token 序列，
    由 collate_fn 在每个 batch 内动态 pad 到该 batch 最长序列。
    """

    def __init__(
        self,
        titles: list[str],
        tokenizer: AutoTokenizer,
        abstracts: Optional[list[str]] = None,
        max_len: int = 512,
    ):
        # 有摘要时只截断摘要（Segment B），完整保留标题（Segment A），与训练一致
        truncation = "only_second" if abstracts is not None else True
        encodings = tokenizer(
            titles,
            text_pair=abstracts,
            add_special_tokens=True,
            max_length=max_len,
            padding=False,
            truncation=truncation,
        )
        self.input_ids = encodings["input_ids"]  # list[list[int]]
        self.attention_mask = encodings["attention_mask"]  # list[list[int]]
        self.token_type_ids = encodings.get("token_type_ids")  # None if model doesn't use them

    def __len__(self) -> int:
        return len(self.input_ids)

    def __getitem__(self, idx: int):
        item: dict = {
            "input_ids": self.input_ids[idx],
            "attention_mask": self.attention_mask[idx],
        }
        if self.token_type_ids is not None:
            item["token_type_ids"] = self.token_type_ids[idx]
        return item


def _dynamic_pad_collate(batch, pad_id: int = 0):
    """动态 Padding: 每个 batch pad 到该 batch 内最长序列长度。"""
    max_len = max(len(b["input_ids"]) for b in batch)
    input_ids, attention_mask, token_type_ids = [], [], []
    has_token_type_ids = "token_type_ids" in batch[0]
    for b in batch:
        pad_len = max_len - len(b["input_ids"])
        input_ids.append(b["input_ids"] + [pad_id] * pad_len)
        attention_mask.append(b["attention_mask"] + [0] * pad_len)
        if has_token_type_ids:
            token_type_ids.append(b["token_type_ids"] + [0] * pad_len)
    result = {
        "input_ids": torch.tensor(input_ids, dtype=torch.long),
        "attention_mask": torch.tensor(attention_mask, dtype=torch.long),
    }
    if has_token_type_ids:
        result["token_type_ids"] = torch.tensor(token_type_ids, dtype=torch.long)
    return result


# ═══════════════════════════════════════════════════════════════════════
#  推理
# ═══════════════════════════════════════════════════════════════════════


def _format_positives(positives: pd.DataFrame) -> str:
    """将正例 DataFrame 格式化为可读字符串。"""
    lines = []
    for display_index, (_, row) in enumerate(positives.iterrows(), start=1):
        lines.append(f"\n----- Positive #{display_index} -----")
        lines.append(f"Title      : {row['Title']}")
        if "arXiv ID" in positives.columns:
            arxiv_id = str(row["arXiv ID"]).split("v")[0]
            lines.append(f"Link       : https://arxiv.org/abs/{arxiv_id}")
        lines.append(f"Probability: {row['Probability']:.4f}")
    return "\n".join(lines)


def _select_positives(data: pd.DataFrame) -> pd.DataFrame:
    return cast(pd.DataFrame, data.loc[data["Prediction"] == 1].reset_index(drop=True).copy())


def prepare_inference_inputs(data: pd.DataFrame) -> tuple[list[str], Optional[list[str]]]:
    if "Title" not in data.columns:
        raise ValueError("Input data must contain a 'Title' column.")

    titles = cast(list[str], data["Title"].fillna("").astype(str).tolist())
    if "Abstract" not in data.columns:
        return titles, None

    abstracts = cast(list[str], data["Abstract"].fillna("").tolist())
    has_abstract = sum(1 for abstract in abstracts if abstract)
    logger.info(f"获取到 {has_abstract}/{len(abstracts)} 篇论文的摘要")
    return titles, abstracts


def predict_papers(
    model: AutoModelForSequenceClassification,
    tokenizer: AutoTokenizer,
    titles: list[str],
    abstracts: Optional[list[str]] = None,
    threshold: float = 0.5,
    batch_size: int = 32,
    max_len: int = 512,
    device: Optional[torch.device] = None,
) -> tuple[list[float], list[int]]:
    """
    对一批论文（标题+摘要）做推理，返回正类的概率和二值预测。

    使用动态 padding（与训练一致），每个 batch 只 pad 到 batch 内最长序列。
    """
    if device is None:
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model.eval()

    ds = TitleAbstractDataset(titles, tokenizer, abstracts=abstracts, max_len=max_len)
    pad_id = tokenizer.pad_token_id or 0
    collate_fn = partial(_dynamic_pad_collate, pad_id=pad_id)
    loader = DataLoader(ds, batch_size=batch_size, collate_fn=collate_fn)

    all_probs = []
    with torch.no_grad():
        for batch in loader:
            input_ids = batch["input_ids"].to(device)
            attention_mask = batch["attention_mask"].to(device)
            token_type_ids = batch["token_type_ids"].to(device) if "token_type_ids" in batch else None

            logits = model(
                input_ids=input_ids,
                attention_mask=attention_mask,
                token_type_ids=token_type_ids,
            ).logits
            probs = F.softmax(logits, dim=1)[:, 1]
            all_probs.extend(probs.cpu().tolist())

    preds = [1 if p >= threshold else 0 for p in all_probs]
    return all_probs, preds


def _run_inference(
    model_path: Optional[str] = None,
    threshold: Optional[float] = None,
    batch_size: Optional[int] = None,
    max_len: Optional[int] = None,
) -> tuple[pd.DataFrame, float] | tuple[None, str]:
    """
    核心推理流程：获取今日论文并执行模型推理。

    Returns:
        成功时返回 (data, threshold)，data 含 Probability/Prediction 列；
        失败时返回 (None, error_message)。
    """
    params = _resolve_params(model_path, threshold, batch_size, max_len)
    logger.info(
        f"推理参数: model={params.model_path}, threshold={params.threshold}, "
        f"batch_size={params.batch_size}, max_len={params.max_len}"
    )

    from .arxiv_today import get_today_arxiv

    try:
        data = get_today_arxiv()
    except Exception as e:
        logger.error(f"Error fetching today's papers: {e}")
        return None, f"Error fetching today's papers: {str(e)}"

    if data.empty:
        return None, "No papers found for today."

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    try:
        model, tokenizer = _load_model_and_tokenizer(params.model_path, device)
    except Exception as e:
        logger.error(f"Error loading model: {e}")
        return None, f"Error loading model from {params.model_path}: {str(e)}"

    try:
        titles, abstracts = prepare_inference_inputs(data)
    except ValueError as e:
        return None, f"Error: {e}"

    if params.input_mode == "title_only":
        abstracts = None
        logger.info("input_mode=title_only: 仅使用标题进行推理")
    else:
        logger.info("input_mode=title_abstract: 使用标题+摘要进行推理")

    probs, preds = predict_papers(
        model,
        tokenizer,
        titles,
        abstracts=abstracts,
        threshold=params.threshold,
        batch_size=params.batch_size,
        max_len=params.max_len,
        device=device,
    )

    data["Probability"] = probs
    data["Prediction"] = preds
    return data, params.threshold


def get_positive_arxiv_today_as_string(
    model_path: Optional[str] = None,
    threshold: Optional[float] = None,
    batch_size: Optional[int] = None,
    max_len: Optional[int] = None,
) -> str:
    """
    获取今日 arXiv 论文，执行推理并返回正类结果的格式化字符串。

    阈值优先级: 函数参数 > training_config.json > config.json > 0.5
    max_len 优先级: 函数参数 > training_config.json > config.json > 512
    """
    data, result = _run_inference(model_path, threshold, batch_size, max_len)
    if data is None:
        return str(result)
    positives = _select_positives(data)
    if len(positives) == 0:
        return "No positive predictions found."
    return _format_positives(positives)


def main() -> None:
    """命令行入口：执行今日 arXiv 论文推理，打印正例结果并将全量结果保存到 CSV。"""
    parser = argparse.ArgumentParser(
        description="Run arXiv paper inference and print positive predictions.",
    )
    parser.add_argument("--model-path", type=str, default=None, help="模型目录路径")
    parser.add_argument("--threshold", type=float, default=None, help="分类阈值")
    parser.add_argument("--batch-size", type=int, default=None, help="推理批大小")
    parser.add_argument("--max-len", type=int, default=None, help="最大 token 长度")
    parser.add_argument(
        "--output",
        type=str,
        default="inference_output.csv",
        help="全量推理结果 CSV 输出路径（默认: inference_output.csv）",
    )
    parser.add_argument(
        "--test-positive",
        action="store_true",
        help="注入一篇已知正样本，验证模型能否给出高概率（sanity check）",
    )
    parser.add_argument(
        "--test-title",
        type=str,
        default="Diffusion Models for High-Resolution Image Synthesis",
        help="测试正样本的标题",
    )
    parser.add_argument(
        "--test-abstract",
        type=str,
        default=(
            "We present a diffusion-based generative model that achieves "
            "state-of-the-art image synthesis quality on standard benchmarks. "
            "Our method uses a hierarchical denoising process with classifier-free "
            "guidance to produce photorealistic images at high resolution."
        ),
        help="测试正样本的摘要",
    )
    args = parser.parse_args()

    # ── sanity-check 模式 ──
    if args.test_positive:
        params = _resolve_params(args.model_path, args.threshold, args.batch_size, args.max_len)
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        model, tokenizer = _load_model_and_tokenizer(params.model_path, device)

        test_abstracts = None if params.input_mode == "title_only" else [args.test_abstract]
        probs, preds = predict_papers(
            model,
            tokenizer,
            [args.test_title],
            abstracts=test_abstracts,
            threshold=params.threshold,
            batch_size=params.batch_size,
            max_len=params.max_len,
            device=device,
        )
        print(f"\n[Sanity Check] threshold={params.threshold:.4f}, max_len={params.max_len}")
        print(f"Title    : {args.test_title}")
        print(f"Prob     : {probs[0]:.4f}")
        print(f"Predicted: {'POSITIVE' if preds[0] == 1 else 'NEGATIVE'}")
        return

    # ── 正常推理 ──
    data, result = _run_inference(
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

    positives = _select_positives(data)
    if len(positives) == 0:
        print("No positive predictions found.")
    else:
        print(_format_positives(positives))


if __name__ == "__main__":
    main()
