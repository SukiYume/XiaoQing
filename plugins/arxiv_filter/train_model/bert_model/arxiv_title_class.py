"""
arXiv 论文标题分类器训练脚本
=====================================

功能说明：
    使用 BERT 模型对 arXiv 天体物理论文进行二分类，判断用户是否对该论文感兴趣。
    输入：论文标题 (Title)
    输出：二分类结果（0=不感兴趣，1=感兴趣）

训练策略：
    - 使用 WeightedRandomSampler 平衡正负样本
    - 使用加权交叉熵损失函数处理类别不平衡
    - 支持混合精度训练 (AMP) 加速 GPU 训练
    - 动态 padding 优化显存使用

作者：XiaoQing (Refactored)
"""

from __future__ import annotations

import importlib
import json
import sys
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any, cast

import numpy as np
import pandas as pd
import torch
from sklearn.metrics import (
    classification_report,
    confusion_matrix,
)
from torch.utils.data import DataLoader, Dataset, WeightedRandomSampler
from tqdm.auto import tqdm

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
    from plugins.arxiv_filter.train_model.bert_model import training_utils as _training
else:
    from . import training_utils as _training

_log = _training.timestamp_log
forward_logits = _training.forward_logits
log_epoch_header = _training.log_epoch_header
dynamic_pad_collate = _training.dynamic_pad_collate
train_epoch = _training.train_epoch

# 动态导入 transformers 库，避免静态依赖问题
transformers = importlib.import_module("transformers")
AutoModelForSequenceClassification = transformers.AutoModelForSequenceClassification
AutoTokenizer = transformers.AutoTokenizer
get_linear_schedule_with_warmup = transformers.get_linear_schedule_with_warmup

_PLUGIN_DIR = Path(__file__).resolve().parents[2]

# =============================================================================
# 训练配置类
# =============================================================================


@dataclass(frozen=True, slots=True)
class TrainingConfig:
    data_path: Path = _PLUGIN_DIR / "train_model" / "arxiv_papers_with_abstract.csv"
    model_name: str = "bert-base-cased"
    max_len: int = 64
    batch_size: int = 256
    num_epochs: int = 20
    learning_rate: float = 2e-5
    warmup_proportion: float = 0.1
    validation_size: float = 0.1
    random_seed: int = 42
    num_workers: int | None = None
    output_dir: Path = field(default_factory=lambda: _PLUGIN_DIR / "best_model_title")


@dataclass(frozen=True, slots=True)
class ValidationMetrics:
    accuracy: float
    average_loss: float
    report: str
    cm: np.ndarray


# 全局配置实例
CONFIG = TrainingConfig()

# 自动选择计算设备
DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")

# =============================================================================
# 数据预处理函数
# =============================================================================


def prepare_training_frame(df: pd.DataFrame) -> pd.DataFrame:
    renamed = df.rename(
        columns={
            "Title": "title",
            "label": "label",
        }
    )

    required_columns = {"title", "label"}
    missing_columns = required_columns - set(renamed.columns)
    if missing_columns:
        missing = ", ".join(sorted(missing_columns))
        raise ValueError(f"Missing required columns: {missing}")

    prepared = renamed.loc[:, ["title", "label"]].copy()
    prepared["title"] = prepared["title"].fillna("").astype(str)
    prepared["label"] = _training.coerce_binary_labels(prepared["label"])
    return prepared


# =============================================================================
# PyTorch Dataset 类
# =============================================================================


class TitleDataset(Dataset[dict[str, Any]]):
    def __init__(
        self,
        titles: list[str],
        labels: list[int],
        tokenizer,
        max_len: int,
    ):
        encodings = tokenizer(
            titles,
            add_special_tokens=True,
            max_length=max_len,
            padding=False,
            truncation=True,
        )
        self.input_ids: list[list[int]] = encodings["input_ids"]
        self.attention_mask: list[list[int]] = encodings["attention_mask"]
        self.labels = labels

    def __len__(self) -> int:
        return len(self.labels)

    def __getitem__(self, idx: int) -> dict[str, Any]:
        return {
            "input_ids": self.input_ids[idx],
            "attention_mask": self.attention_mask[idx],
            "labels": self.labels[idx],
        }


# =============================================================================
# 动态 Padding 和 DataLoader 创建
# =============================================================================


def create_data_loader(
    df: Any,
    tokenizer,
    max_len: int,
    batch_size: int,
    sampler: WeightedRandomSampler | None = None,
    num_workers: int = 0,
    pin_memory: bool = False,
    random_seed: int | None = None,
    shuffle: bool = True,
) -> DataLoader[Any]:
    dataset = TitleDataset(
        titles=df["title"].tolist(),
        labels=df["label"].tolist(),
        tokenizer=tokenizer,
        max_len=max_len,
    )

    collate_fn = partial(dynamic_pad_collate, pad_id=tokenizer.pad_token_id or 0)

    return _training.create_seeded_data_loader(
        dataset,
        collate_fn=collate_fn,
        batch_size=batch_size,
        sampler=sampler,
        num_workers=num_workers,
        pin_memory=pin_memory,
        random_seed=random_seed,
        shuffle=shuffle,
    )


# =============================================================================
# 训练和评估函数
# =============================================================================


def eval_model(
    model,
    data_loader,
    device,
    use_amp: bool = False,
    amp_dtype: torch.dtype = torch.float32,
):
    model.eval()
    losses: list[float] = []
    all_labels: list[int] = []
    all_preds: list[int] = []
    loss_fn = torch.nn.CrossEntropyLoss()

    with torch.no_grad():
        for batch in tqdm(data_loader, desc="Validation", ascii=True):
            logits, labels = forward_logits(model, batch, device, use_amp, amp_dtype)
            logits = logits.float()
            loss = loss_fn(logits, labels)

            preds = logits.argmax(dim=1)

            losses.append(loss.item())
            all_labels.extend(labels.cpu().tolist())
            all_preds.extend(preds.cpu().tolist())

    accuracy = sum(pred == label for pred, label in zip(all_preds, all_labels, strict=True)) / len(
        all_labels
    )
    report = cast(
        str,
        classification_report(
            all_labels,
            all_preds,
            target_names=["negative", "positive"],
        ),
    )
    cm = confusion_matrix(all_labels, all_preds, labels=[0, 1])
    average_loss = sum(losses) / len(losses)
    return ValidationMetrics(
        accuracy=accuracy,
        average_loss=average_loss,
        report=report,
        cm=cm,
    )


# =============================================================================
# 模型保存
# =============================================================================


def save_training_config(
    output_dir: Path,
    config: TrainingConfig,
    best_accuracy: float,
) -> None:
    payload = {
        "model_name": config.model_name,
        "max_len": config.max_len,
        "best_validation_accuracy": float(best_accuracy),
        "input_mode": "title_only",
    }
    with output_dir.joinpath("training_config.json").open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)


# =============================================================================
# 主训练流程
# =============================================================================


def main(config: TrainingConfig = CONFIG) -> None:
    training = _training.prepare_classifier_training(
        config,
        device=DEVICE,
        classifier_name="arXiv Title Classifier",
        prepare_frame=prepare_training_frame,
        create_loader=create_data_loader,
        tokenizer_factory=AutoTokenizer,
        model_factory=AutoModelForSequenceClassification,
        scheduler_factory=get_linear_schedule_with_warmup,
    )
    runtime = training.runtime
    tokenizer = training.tokenizer
    model = training.model
    train_loader = training.train_loader
    val_loader = training.validation_loader
    optimizer = training.optimizer
    scheduler = training.scheduler
    loss_fn = training.loss_fn

    best_accuracy = 0.0

    for epoch in range(1, config.num_epochs + 1):
        log_epoch_header(epoch, config.num_epochs, optimizer.param_groups[-1]["lr"])

        train_acc, train_loss = train_epoch(
            model,
            train_loader,
            optimizer,
            scheduler,
            DEVICE,
            loss_fn,
            use_amp=runtime["use_amp"],
            amp_dtype=runtime["amp_dtype"],
        )
        print(f"\n  {'─' * 72}")
        _log(f"Train | loss={train_loss:.4f}  acc={train_acc:.4f}")

        val_metrics = eval_model(
            model,
            val_loader,
            DEVICE,
            use_amp=runtime["use_amp"],
            amp_dtype=runtime["amp_dtype"],
        )

        is_best = val_metrics.accuracy > best_accuracy
        _log(
            "Valid | loss={loss:.4f}  acc={acc:.4f}  {star}".format(
                loss=val_metrics.average_loss,
                acc=val_metrics.accuracy,
                star="★" if is_best else "",
            )
        )
        print(val_metrics.report)
        print("Confusion Matrix (labels=[negative, positive]):")
        print(val_metrics.cm)

        if is_best:
            best_accuracy = val_metrics.accuracy
            _log(f"      | ★ New best (acc={best_accuracy:.4f}), saving -> {config.output_dir}")
            model.save_pretrained(config.output_dir)
            tokenizer.save_pretrained(config.output_dir)
            save_training_config(
                config.output_dir,
                config,
                best_accuracy,
            )

    _log("═" * 70)
    _log("  Final Evaluation")
    _log("═" * 70)
    _log(f"  Best validation accuracy: {best_accuracy:.4f}")
    _log("─" * 70)


if __name__ == "__main__":
    main()
