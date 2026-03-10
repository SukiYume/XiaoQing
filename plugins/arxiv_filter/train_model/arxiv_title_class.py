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

import json
import importlib
import os
import random
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, cast
from datetime import datetime

import numpy as np
import pandas as pd
import torch
from sklearn.metrics import (
    classification_report,
    confusion_matrix,
)
from sklearn.model_selection import train_test_split
from sklearn.utils.class_weight import compute_class_weight
from torch.utils.data import DataLoader, Dataset, WeightedRandomSampler
from tqdm.auto import tqdm

# 动态导入 transformers 库，避免静态依赖问题
transformers = importlib.import_module("transformers")
AutoModelForSequenceClassification = transformers.AutoModelForSequenceClassification
AutoTokenizer = transformers.AutoTokenizer
get_linear_schedule_with_warmup = transformers.get_linear_schedule_with_warmup

# =============================================================================
# 训练配置类
# =============================================================================

@dataclass(frozen=True)
class TrainingConfig:
    data_path: Path = Path(__file__).with_name("arxiv_papers_with_abstract.csv")
    model_name: str = "bert-base-cased"
    max_len: int = 64
    batch_size: int = 256
    num_epochs: int = 20
    learning_rate: float = 2e-5
    warmup_proportion: float = 0.1
    validation_size: float = 0.1
    random_seed: int = 42
    num_workers: int | None = None
    output_dir: Path = Path(__file__).with_name("best_model_title_class")


@dataclass(frozen=True)
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
# 日志工具函数
# =============================================================================

def _log(message: str = "") -> None:
    print(f"{datetime.now().strftime('%H:%M:%S')}  {message}", flush=True)

# =============================================================================
# 随机种子设置
# =============================================================================

def seed_everything(seed: int) -> None:
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)

def _seed_worker(worker_id: int) -> None:
    worker_seed = torch.initial_seed() % 2**32
    np.random.seed(worker_seed)
    random.seed(worker_seed)

# =============================================================================
# 运行时配置函数
# =============================================================================

def get_runtime_settings(device: torch.device) -> dict[str, Any]:
    use_amp = device.type == "cuda"
    if use_amp:
        torch.set_float32_matmul_precision("high")
        torch.backends.cuda.matmul.allow_tf32 = True
        torch.backends.cudnn.allow_tf32 = True

    return {
        "use_amp": use_amp,
        "pin_memory": use_amp,
        "use_fused": use_amp,
        "amp_dtype": torch.bfloat16 if use_amp else torch.float32,
    }

def create_optimizer(params, learning_rate: float, use_fused: bool):
    if use_fused:
        try:
            return torch.optim.AdamW(params, lr=learning_rate, fused=True)
        except TypeError:
            pass
    return torch.optim.AdamW(params, lr=learning_rate)

def build_loader_kwargs(num_workers: int, pin_memory: bool) -> dict[str, object]:
    kwargs: dict[str, object] = {
        "num_workers": num_workers,
        "pin_memory": pin_memory,
        "persistent_workers": num_workers > 0,
    }
    if num_workers > 0:
        kwargs["prefetch_factor"] = 4
    return kwargs

# =============================================================================
# 数据处理辅助函数
# =============================================================================

def move_batch_to_device(
    batch: dict[str, torch.Tensor], device: torch.device
) -> dict[str, torch.Tensor]:
    return {name: value.to(device) for name, value in batch.items()}

def forward_logits(
    model,
    batch: dict[str, torch.Tensor],
    device: torch.device,
    use_amp: bool,
    amp_dtype: torch.dtype,
) -> tuple[torch.Tensor, torch.Tensor]:
    batch = move_batch_to_device(batch, device)
    with torch.autocast(device_type=device.type, enabled=use_amp, dtype=amp_dtype):
        # 注意：这里不需要 token_type_ids，因为只是单句子输入
        logits = model(
            input_ids=batch["input_ids"],
            attention_mask=batch["attention_mask"],
        ).logits
    return logits, batch["labels"]

# =============================================================================
# 日志格式化函数
# =============================================================================

def build_config_log_lines(
    config: TrainingConfig,
    device: str,
    sample_count: int,
    train_count: int,
    val_count: int,
    negative_count: int,
    positive_count: int,
) -> list[str]:
    ratio = float("inf") if positive_count == 0 else negative_count / positive_count
    ratio_text = "inf" if ratio == float("inf") else f"{ratio:.1f}"
    return [
        "═" * 70,
        f"  arXiv Title Classifier ({config.model_name.split('/')[-1]})",
        "═" * 70,
        f"  Device: {device}  |  Samples: {sample_count} (train={train_count}, val={val_count})",
        f"  Neg:Pos = {ratio_text}:1  |  Sampler -> balanced  |  Loss: weighted cross entropy",
        f"  Epochs={config.num_epochs}  |  BS={config.batch_size}  |  LR={config.learning_rate:.2e}",
        f"  MaxLen={config.max_len}  |  Warmup={config.warmup_proportion:.2f}  |  Seed={config.random_seed}",
        "─" * 70,
    ]

def log_epoch_header(epoch: int, total_epochs: int, learning_rate: float) -> None:
    print(f"\n{'=' * 76}")
    print(f"  Epoch {epoch}/{total_epochs}  |  LR {learning_rate:.6e}")
    print(f"{'=' * 76}")

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
    prepared["label"] = prepared["label"].astype(int)
    return prepared

def split_train_validation_frame(
    df: pd.DataFrame,
    validation_size: float,
    random_seed: int,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    train_df, val_df = train_test_split(
        df,
        test_size=validation_size,
        stratify=df["label"],
        random_state=random_seed,
    )
    return pd.DataFrame(train_df), pd.DataFrame(val_df)

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

def dynamic_pad_collate(batch: list[dict[str, Any]], pad_id: int = 0) -> dict[str, torch.Tensor]:
    max_len = max(
        len(sample_input_ids) for sample_input_ids in [sample["input_ids"] for sample in batch]
    )
    input_ids = []
    attention_mask = []
    labels = []

    for sample in batch:
        sample_input_ids = list(sample["input_ids"])
        sample_attention_mask = list(sample["attention_mask"])
        pad_len = max_len - len(sample_input_ids)

        input_ids.append(sample_input_ids + [pad_id] * pad_len)
        attention_mask.append(sample_attention_mask + [0] * pad_len)
        labels.append(sample["labels"])

    return {
        "input_ids": torch.tensor(input_ids, dtype=torch.long),
        "attention_mask": torch.tensor(attention_mask, dtype=torch.long),
        "labels": torch.tensor(labels, dtype=torch.long),
    }

def create_data_loader(
    df: Any,
    tokenizer,
    max_len: int,
    batch_size: int,
    sampler: WeightedRandomSampler | None = None,
    num_workers: int = 0,
    pin_memory: bool = False,
    random_seed: int | None = None,
) -> DataLoader[Any]:
    dataset = TitleDataset(
        titles=df["title"].tolist(),
        labels=df["label"].tolist(),
        tokenizer=tokenizer,
        max_len=max_len,
    )

    collate_fn = partial(dynamic_pad_collate, pad_id=tokenizer.pad_token_id or 0)

    generator = None
    worker_init_fn = None
    if random_seed is not None:
        generator = torch.Generator()
        generator.manual_seed(random_seed)
        worker_init_fn = _seed_worker

    loader_kwargs = build_loader_kwargs(num_workers=num_workers, pin_memory=pin_memory)
    loader_num_workers = cast(int, loader_kwargs["num_workers"])
    loader_pin_memory = cast(bool, loader_kwargs["pin_memory"])
    loader_persistent_workers = cast(bool, loader_kwargs["persistent_workers"])

    if num_workers > 0:
        return DataLoader(
            dataset,
            batch_size=batch_size,
            sampler=sampler,
            shuffle=sampler is None,
            collate_fn=collate_fn,
            num_workers=loader_num_workers,
            pin_memory=loader_pin_memory,
            persistent_workers=loader_persistent_workers,
            prefetch_factor=cast(int, loader_kwargs["prefetch_factor"]),
            worker_init_fn=worker_init_fn,
            generator=generator,
        )

    return DataLoader(
        dataset,
        batch_size=batch_size,
        sampler=sampler,
        shuffle=sampler is None,
        collate_fn=collate_fn,
        num_workers=loader_num_workers,
        pin_memory=loader_pin_memory,
        persistent_workers=loader_persistent_workers,
        worker_init_fn=worker_init_fn,
        generator=generator,
    )

# =============================================================================
# 训练和评估函数
# =============================================================================

def train_epoch(
    model,
    data_loader,
    optimizer,
    scheduler,
    device,
    loss_fn,
    use_amp: bool = False,
    amp_dtype: torch.dtype = torch.float32,
) -> tuple[float, float]:
    model.train()
    losses: list[float] = []
    correct_predictions = 0

    for batch in tqdm(data_loader, desc="Training", ascii=True):
        optimizer.zero_grad(set_to_none=True)

        logits, labels = forward_logits(model, batch, device, use_amp, amp_dtype)

        loss = loss_fn(logits.float(), labels)
        loss.backward()

        torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=1.0)

        optimizer.step()
        scheduler.step()

        preds = logits.argmax(dim=1)
        correct_predictions += torch.sum(preds == labels).item()
        losses.append(loss.item())

    accuracy = correct_predictions / len(data_loader.dataset)
    average_loss = sum(losses) / len(losses)
    return accuracy, average_loss

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

    accuracy = sum(pred == label for pred, label in zip(all_preds, all_labels)) / len(all_labels)
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
# 类别平衡工具
# =============================================================================

def build_weighted_sampler(labels: list[int], class_weights: torch.Tensor) -> WeightedRandomSampler:
    sample_weights = [class_weights[label].item() for label in labels]
    return WeightedRandomSampler(
        weights=sample_weights,
        num_samples=len(sample_weights),
        replacement=True,
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
    seed_everything(config.random_seed)
    _log(f"Using device: {DEVICE}")
    runtime = get_runtime_settings(DEVICE)

    # 1. 加载并预处理数据
    df = prepare_training_frame(pd.read_csv(config.data_path))
    train_df, val_df = split_train_validation_frame(
        df,
        validation_size=config.validation_size,
        random_seed=config.random_seed,
    )

    # 2. 计算类别权重（用于加权损失函数和采样器）
    class_weights = compute_class_weight(
        class_weight="balanced",
        classes=np.unique(train_df.loc[:, "label"]),
        y=train_df.loc[:, "label"],
    )
    class_weights_tensor = torch.tensor(class_weights, dtype=torch.float, device=DEVICE)

    # 3. 创建加权损失函数和采样器
    loss_fn = torch.nn.CrossEntropyLoss(weight=class_weights_tensor)
    sampler = build_weighted_sampler(train_df.loc[:, "label"].tolist(), class_weights_tensor)

    negative_count = int((train_df.loc[:, "label"] == 0).sum())
    positive_count = int((train_df.loc[:, "label"] == 1).sum())

    for line in build_config_log_lines(
        config=config,
        device=str(DEVICE),
        sample_count=len(df),
        train_count=len(train_df),
        val_count=len(val_df),
        negative_count=negative_count,
        positive_count=positive_count,
    ):
        _log(line)

    num_workers = config.num_workers if config.num_workers is not None else min(8, os.cpu_count() or 4)

    _log("  Loading tokenizer...")
    tokenizer = AutoTokenizer.from_pretrained(config.model_name)
    train_loader = create_data_loader(
        train_df,
        tokenizer,
        config.max_len,
        config.batch_size,
        sampler=sampler,
        num_workers=num_workers,
        pin_memory=runtime["pin_memory"],
        random_seed=config.random_seed,
    )
    val_loader = create_data_loader(
        val_df,
        tokenizer,
        config.max_len,
        config.batch_size,
        num_workers=num_workers,
        pin_memory=runtime["pin_memory"],
        random_seed=config.random_seed,
    )

    _log(f"  Loading model {config.model_name.split('/')[-1]}...")
    model = AutoModelForSequenceClassification.from_pretrained(config.model_name, num_labels=2)
    model.to(DEVICE)

    optimizer = create_optimizer(
        model.parameters(),
        learning_rate=config.learning_rate,
        use_fused=runtime["use_fused"],
    )
    total_steps = len(train_loader) * config.num_epochs
    scheduler = get_linear_schedule_with_warmup(
        optimizer,
        num_warmup_steps=int(config.warmup_proportion * total_steps),
        num_training_steps=total_steps,
    )

    best_accuracy = 0.0
    config.output_dir.mkdir(parents=True, exist_ok=True)

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
            _log(
                "      | ★ New best (acc={acc:.4f}), saving -> {output_dir}".format(
                    acc=best_accuracy,
                    output_dir=config.output_dir,
                )
            )
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
