"""两个 BERT 训练入口共用的装配、运行时与 epoch 助手。

两个 BERT 训练入口必须共用同一套随机种子、动态补齐、AMP 和 epoch 更新顺序，避免
标题模型与标题+摘要模型产生不可解释的训练差异。训练加载器可打乱或使用加权采样器，
验证加载器保持顺序；同一个 batch 是否含 token_type_ids 由数据集契约统一决定。
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd
import torch
from sklearn.model_selection import train_test_split
from sklearn.utils.class_weight import compute_class_weight
from torch.utils.data import DataLoader, WeightedRandomSampler
from tqdm.auto import tqdm

from ..training_common import (
    coerce_binary_labels,
    read_training_csv,
    seed_python_numpy,
    timestamp_log,
)


@dataclass(slots=True)
class ClassifierTrainingRuntime:
    """一次训练运行已经装配好的共享资源。"""

    runtime: dict[str, Any]
    frame: pd.DataFrame
    train_frame: pd.DataFrame
    validation_frame: pd.DataFrame
    tokenizer: Any
    model: Any
    train_loader: DataLoader[Any]
    validation_loader: DataLoader[Any]
    optimizer: Any
    scheduler: Any
    loss_fn: Any


def seed_everything(seed: int) -> None:
    """Seed Python, NumPy, PyTorch, and all visible CUDA devices."""

    seed_python_numpy(seed)
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)


def seed_worker(_worker_id: int) -> None:
    """Derive deterministic Python and NumPy seeds for a DataLoader worker."""

    worker_seed = torch.initial_seed() % 2**32
    seed_python_numpy(worker_seed)


def get_runtime_settings(device: torch.device) -> dict[str, Any]:
    """Return device-dependent AMP, transfer, and optimizer settings."""

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
    """Create AdamW, falling back when this PyTorch lacks fused AdamW."""

    if use_fused:
        try:
            return torch.optim.AdamW(params, lr=learning_rate, fused=True)
        except TypeError:
            # fused 是设备/构建能力而非训练语义；当前构建不支持时退回同一 AdamW。
            pass
    return torch.optim.AdamW(params, lr=learning_rate)


def build_loader_kwargs(num_workers: int, pin_memory: bool) -> dict[str, object]:
    """Build only the DataLoader options valid for the selected worker count."""

    kwargs: dict[str, object] = {
        "num_workers": num_workers,
        "pin_memory": pin_memory,
        "persistent_workers": num_workers > 0,
    }
    if num_workers > 0:
        kwargs["prefetch_factor"] = 4
    return kwargs


def move_batch_to_device(
    batch: dict[str, torch.Tensor],
    device: torch.device,
) -> dict[str, torch.Tensor]:
    """Move all tensors in one classifier batch to ``device``."""

    return {name: value.to(device) for name, value in batch.items()}


def forward_logits(
    model,
    batch: dict[str, torch.Tensor],
    device: torch.device,
    use_amp: bool,
    amp_dtype: torch.dtype,
) -> tuple[torch.Tensor, torch.Tensor]:
    """Run one title-only or title+abstract classifier batch."""

    batch = move_batch_to_device(batch, device)
    model_inputs = {
        "input_ids": batch["input_ids"],
        "attention_mask": batch["attention_mask"],
    }
    if "token_type_ids" in batch:
        model_inputs["token_type_ids"] = batch["token_type_ids"]
    with torch.autocast(device_type=device.type, enabled=use_amp, dtype=amp_dtype):
        logits = model(**model_inputs).logits
    return logits, batch["labels"]


def build_classifier_config_log_lines(
    config: Any,
    *,
    classifier_name: str,
    device: str,
    sample_count: int,
    train_count: int,
    val_count: int,
    negative_count: int,
    positive_count: int,
) -> list[str]:
    """Build the stable startup banner shared by both BERT trainers."""

    ratio = float("inf") if positive_count == 0 else negative_count / positive_count
    ratio_text = "inf" if ratio == float("inf") else f"{ratio:.1f}"
    return [
        "═" * 70,
        f"  {classifier_name} ({config.model_name.split('/')[-1]})",
        "═" * 70,
        f"  Device: {device}  |  Samples: {sample_count} (train={train_count}, val={val_count})",
        f"  Neg:Pos = {ratio_text}:1  |  Sampler -> balanced  |  Loss: weighted cross entropy",
        f"  Epochs={config.num_epochs}  |  BS={config.batch_size}  |  LR={config.learning_rate:.2e}",
        f"  MaxLen={config.max_len}  |  Warmup={config.warmup_proportion:.2f}  |  Seed={config.random_seed}",
        "─" * 70,
    ]


def log_epoch_header(epoch: int, total_epochs: int, learning_rate: float) -> None:
    """Print the stable epoch header used by both BERT trainers."""

    print(f"\n{'=' * 76}")
    print(f"  Epoch {epoch}/{total_epochs}  |  LR {learning_rate:.6e}")
    print(f"{'=' * 76}")


def split_train_validation_frame(
    df: pd.DataFrame,
    validation_size: float,
    random_seed: int,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Create the deterministic, label-stratified training split."""

    train_frame, validation_frame = train_test_split(
        df,
        test_size=validation_size,
        stratify=df["label"],
        random_state=random_seed,
    )
    return pd.DataFrame(train_frame), pd.DataFrame(validation_frame)


def dynamic_pad_collate(
    batch: list[dict[str, Any]],
    pad_id: int = 0,
) -> dict[str, torch.Tensor]:
    """Pad title-only or paired BERT samples to the longest item in a batch."""

    max_len = max(len(sample["input_ids"]) for sample in batch)
    # 同一 DataLoader 的样本字段必须一致，只检查首项即可；混合 schema 应由数据集
    # 构造阶段暴露，而不是在每个 batch 内静默补造 token_type_ids。
    include_token_types = "token_type_ids" in batch[0]
    input_ids: list[list[int]] = []
    attention_masks: list[list[int]] = []
    token_type_ids: list[list[int]] = []
    labels: list[int] = []

    for sample in batch:
        sample_input_ids = list(sample["input_ids"])
        sample_attention_mask = list(sample["attention_mask"])
        pad_len = max_len - len(sample_input_ids)
        input_ids.append(sample_input_ids + [pad_id] * pad_len)
        attention_masks.append(sample_attention_mask + [0] * pad_len)
        if include_token_types:
            token_type_ids.append(list(sample["token_type_ids"]) + [0] * pad_len)
        labels.append(sample["labels"])

    collated = {
        "input_ids": torch.tensor(input_ids, dtype=torch.long),
        "attention_mask": torch.tensor(attention_masks, dtype=torch.long),
    }
    if include_token_types:
        collated["token_type_ids"] = torch.tensor(token_type_ids, dtype=torch.long)
    collated["labels"] = torch.tensor(labels, dtype=torch.long)
    return collated


def create_seeded_data_loader(
    dataset,
    *,
    collate_fn,
    batch_size: int,
    sampler: WeightedRandomSampler | None = None,
    num_workers: int = 0,
    pin_memory: bool = False,
    random_seed: int | None = None,
    shuffle: bool = True,
) -> DataLoader[Any]:
    """Create a reproducibly seeded DataLoader without invalid worker options."""

    generator = None
    worker_init_fn = None
    if random_seed is not None:
        generator = torch.Generator()
        generator.manual_seed(random_seed)
        worker_init_fn = seed_worker

    loader_kwargs = build_loader_kwargs(num_workers=num_workers, pin_memory=pin_memory)
    return DataLoader(
        dataset,
        batch_size=batch_size,
        sampler=sampler,
        shuffle=shuffle and sampler is None,
        collate_fn=collate_fn,
        worker_init_fn=worker_init_fn,
        generator=generator,
        **loader_kwargs,
    )


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
    """Run one classifier training epoch and return accuracy and mean loss."""

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


def build_weighted_sampler(
    labels: list[int],
    class_weights: torch.Tensor,
) -> WeightedRandomSampler:
    """Build a replacement sampler using the configured per-class weights."""

    sample_weights = [class_weights[label].item() for label in labels]
    return WeightedRandomSampler(
        weights=sample_weights,
        num_samples=len(sample_weights),
        replacement=True,
    )


def prepare_classifier_training(
    config: Any,
    *,
    device: torch.device,
    classifier_name: str,
    prepare_frame: Callable[[pd.DataFrame], pd.DataFrame],
    create_loader: Callable[..., DataLoader[Any]],
    tokenizer_factory: Any,
    model_factory: Any,
    scheduler_factory: Callable[..., Any],
) -> ClassifierTrainingRuntime:
    """装配两个 BERT 分类器完全相同的训练启动流程。

    数据集类、验证指标、最佳模型判定和保存元数据仍由各入口负责；这里只集中
    随机种子、数据切分、类别平衡、加载器以及优化器/调度器，防止两套训练入口漂移。
    """

    seed_everything(config.random_seed)
    timestamp_log(f"Using device: {device}")
    runtime = get_runtime_settings(device)

    # arXiv ID 可能含前导零，所有入口都必须经字符串 CSV 读取器进入预处理。
    frame = prepare_frame(read_training_csv(config.data_path))
    train_frame, validation_frame = split_train_validation_frame(
        frame,
        validation_size=config.validation_size,
        random_seed=config.random_seed,
    )

    class_weights = compute_class_weight(
        class_weight="balanced",
        classes=np.unique(train_frame.loc[:, "label"]),
        y=train_frame.loc[:, "label"],
    )
    class_weights_tensor = torch.tensor(class_weights, dtype=torch.float, device=device)
    loss_fn = torch.nn.CrossEntropyLoss(weight=class_weights_tensor)
    sampler = build_weighted_sampler(
        train_frame.loc[:, "label"].tolist(),
        class_weights_tensor,
    )

    negative_count = int((train_frame.loc[:, "label"] == 0).sum())
    positive_count = int((train_frame.loc[:, "label"] == 1).sum())
    for line in build_classifier_config_log_lines(
        config,
        classifier_name=classifier_name,
        device=str(device),
        sample_count=len(frame),
        train_count=len(train_frame),
        val_count=len(validation_frame),
        negative_count=negative_count,
        positive_count=positive_count,
    ):
        timestamp_log(line)

    num_workers = (
        config.num_workers if config.num_workers is not None else min(8, os.cpu_count() or 4)
    )
    timestamp_log("  Loading tokenizer...")
    tokenizer = tokenizer_factory.from_pretrained(config.model_name)
    train_loader = create_loader(
        train_frame,
        tokenizer,
        config.max_len,
        config.batch_size,
        sampler=sampler,
        num_workers=num_workers,
        pin_memory=runtime["pin_memory"],
        random_seed=config.random_seed,
    )
    validation_loader = create_loader(
        validation_frame,
        tokenizer,
        config.max_len,
        config.batch_size,
        num_workers=num_workers,
        pin_memory=runtime["pin_memory"],
        random_seed=config.random_seed,
        shuffle=False,
    )

    timestamp_log(f"  Loading model {config.model_name.split('/')[-1]}...")
    model = model_factory.from_pretrained(config.model_name, num_labels=2)
    model.to(device)
    optimizer = create_optimizer(
        model.parameters(),
        learning_rate=config.learning_rate,
        use_fused=runtime["use_fused"],
    )
    total_steps = len(train_loader) * config.num_epochs
    scheduler = scheduler_factory(
        optimizer,
        num_warmup_steps=int(config.warmup_proportion * total_steps),
        num_training_steps=total_steps,
    )
    config.output_dir.mkdir(parents=True, exist_ok=True)

    return ClassifierTrainingRuntime(
        runtime=runtime,
        frame=frame,
        train_frame=train_frame,
        validation_frame=validation_frame,
        tokenizer=tokenizer,
        model=model,
        train_loader=train_loader,
        validation_loader=validation_loader,
        optimizer=optimizer,
        scheduler=scheduler,
        loss_fn=loss_fn,
    )


__all__ = [
    "ClassifierTrainingRuntime",
    "build_classifier_config_log_lines",
    "build_loader_kwargs",
    "build_weighted_sampler",
    "coerce_binary_labels",
    "create_optimizer",
    "create_seeded_data_loader",
    "dynamic_pad_collate",
    "forward_logits",
    "get_runtime_settings",
    "log_epoch_header",
    "move_batch_to_device",
    "prepare_classifier_training",
    "read_training_csv",
    "seed_everything",
    "seed_worker",
    "split_train_validation_frame",
    "timestamp_log",
    "train_epoch",
]
