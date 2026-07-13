"""
arXiv 论文标题+摘要分类器训练脚本
=====================================

功能说明：
    使用 BERT 模型对 arXiv 天体物理论文进行二分类，判断用户是否对该论文感兴趣。
    输入：论文标题 (Segment A) + 摘要 (Segment B)
    输出：二分类结果（0=不感兴趣，1=感兴趣）

训练策略：
    - 使用 WeightedRandomSampler 平衡正负样本
    - 使用加权交叉熵损失函数处理类别不平衡
    - 支持混合精度训练 (AMP) 加速 GPU 训练
    - 动态 padding 优化显存使用

作者：XiaoQing
"""

from __future__ import annotations

import importlib
import json
import os
import random
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any, cast

import numpy as np
import pandas as pd
import torch
from sklearn.metrics import (
    classification_report,
    confusion_matrix,
    precision_recall_curve,
)
from sklearn.model_selection import train_test_split
from sklearn.utils.class_weight import compute_class_weight
from torch.utils.data import DataLoader, Dataset, WeightedRandomSampler
from tqdm.auto import tqdm

# 动态导入 transformers 库，避免静态依赖问题
transformers = importlib.import_module("transformers")
AutoModelForSequenceClassification = transformers.AutoModelForSequenceClassification  # 序列分类模型
AutoTokenizer = transformers.AutoTokenizer  # 分词器
get_linear_schedule_with_warmup = (
    transformers.get_linear_schedule_with_warmup
)  # 带预热的学习率调度器


# =============================================================================
# 训练配置类
# =============================================================================


@dataclass(frozen=True)
class TrainingConfig:
    """
    训练超参数配置

    属性说明：
        data_path: 训练数据 CSV 文件路径，默认为同目录下的 arxiv_papers_with_abstract.csv
        model_name: 预训练模型名称，默认使用 bert-base-cased
        max_len: 输入序列最大长度（token 数），BERT 最大支持 512
        batch_size: 每批次样本数量
        num_epochs: 训练轮数
        learning_rate: 学习率
        warmup_proportion: 预热阶段占总训练步数的比例（0.1 表示前 10% 步数用于预热）
        validation_size: 验证集占总数据的比例
        random_seed: 随机种子，确保实验完整可复现（覆盖 Python/NumPy/PyTorch/CUDA/DataLoader worker）
        num_workers: 数据加载器的工作进程数，None 自动选择（最多 8 个），0 表示主进程单进程加载
        output_dir: 最佳模型保存目录
    """

    data_path: Path = Path(__file__).resolve().parents[1] / "arxiv_papers_with_abstract.csv"
    model_name: str = "bert-base-cased"
    max_len: int = 512
    batch_size: int = 128
    num_epochs: int = 20
    learning_rate: float = 2e-5
    warmup_proportion: float = 0.1
    validation_size: float = 0.1
    random_seed: int = 42
    num_workers: int | None = 16
    threshold_beta: float = 2.0
    output_dir: Path = Path(__file__).with_name("best_model_title_abstract")


@dataclass(frozen=True)
class ValidationMetrics:
    accuracy: float
    average_loss: float
    report: str
    cm: np.ndarray
    positive_recall: float
    positive_f1: float
    threshold_score: float
    optimal_threshold: float


# 全局配置实例
CONFIG = TrainingConfig()

# 自动选择计算设备：优先使用 GPU，无 GPU 则使用 CPU
DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")


# =============================================================================
# 日志工具函数
# =============================================================================


def _log(message: str = "") -> None:
    """
    带时间戳的日志输出

    格式：HH:MM:SS  message
    例如：14:30:15  Loading tokenizer...
    """
    print(f"{datetime.now().strftime('%H:%M:%S')}  {message}", flush=True)


# =============================================================================
# 随机种子设置
# =============================================================================


def seed_everything(seed: int) -> None:
    """
    全局随机种子设置，确保实验完整可复现

    覆盖范围：Python random、NumPy、PyTorch CPU/GPU
    """
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)


def _seed_worker(worker_id: int) -> None:
    """DataLoader 工作进程种子初始化，由 PyTorch 自动传入 worker_id"""
    worker_seed = torch.initial_seed() % 2**32
    np.random.seed(worker_seed)
    random.seed(worker_seed)


# =============================================================================
# 运行时配置函数
# =============================================================================


def get_runtime_settings(device: torch.device) -> dict[str, Any]:
    """
    根据设备类型获取运行时优化设置

    当使用 GPU 时，启用以下优化：
        - use_amp: 混合精度训练（自动在 FP32 和 BF16/FP16 间切换）
        - pin_memory: 锁页内存，加速 CPU 到 GPU 的数据传输
        - use_fused: 使用融合 AdamW 优化器，减少 GPU 内核调用
        - amp_dtype: 混合精度数据类型（GPU 用 bfloat16，CPU 用 float32）
        - TF32 加速：在 Ampere 架构 GPU 上使用 TF32 进行矩阵运算

    参数：
        device: 计算设备对象

    返回：
        包含运行时配置的字典
    """
    use_amp = device.type == "cuda"
    if use_amp:
        # 在 CUDA 设备上启用 TF32 加速（Ampere 架构及更新）
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
    """
    创建 AdamW 优化器

    如果支持融合优化器（fused=True），可以减少 GPU 内核调用次数，提升训练速度。

    参数：
        params: 模型参数迭代器
        learning_rate: 学习率
        use_fused: 是否尝试使用融合优化器

    返回：
        torch.optim.AdamW 优化器实例
    """
    if use_fused:
        try:
            return torch.optim.AdamW(params, lr=learning_rate, fused=True)
        except TypeError:
            # 旧版 PyTorch 不支持 fused 参数，回退到普通 AdamW
            pass
    return torch.optim.AdamW(params, lr=learning_rate)


def build_loader_kwargs(num_workers: int, pin_memory: bool) -> dict[str, object]:
    """
    构建 DataLoader 的配置参数

    参数：
        num_workers: 数据加载工作进程数
        pin_memory: 是否使用锁页内存

    返回：
        DataLoader 配置字典

    说明：
        - num_workers > 0 时启用多进程数据加载
        - prefetch_factor=4 表示每个工作进程预取 4 批数据
        - persistent_workers=True 在 epoch 间保持工作进程存活，减少启动开销
    """
    # prefetch_factor 只在启用多进程时有效
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
    """
    将一个批次的数据移动到指定设备（GPU/CPU）

    参数：
        batch: 包含多个张量的字典
        device: 目标设备

    返回：
        所有张量已移动到目标设备的字典
    """
    return {name: value.to(device) for name, value in batch.items()}


def forward_logits(
    model,
    batch: dict[str, torch.Tensor],
    device: torch.device,
    use_amp: bool,
    amp_dtype: torch.dtype,
) -> tuple[torch.Tensor, torch.Tensor]:
    """
    执行模型前向传播，获取分类 logits

    参数：
        model: BERT 分类模型
        batch: 输入批次数据
        device: 计算设备
        use_amp: 是否使用混合精度
        amp_dtype: 混合精度数据类型

    返回：
        (logits, labels) 元组
        - logits: 模型输出的未归一化预测值，形状 (batch_size, num_classes)
        - labels: 真实标签，形状 (batch_size,)
    """
    batch = move_batch_to_device(batch, device)
    with torch.autocast(device_type=device.type, enabled=use_amp, dtype=amp_dtype):
        logits = model(
            input_ids=batch["input_ids"],
            attention_mask=batch["attention_mask"],
            token_type_ids=batch["token_type_ids"],
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
    """
    构建训练开始时的配置信息日志

    输出示例：
    ═════════════════════════════════════════════════════════════════════
      arXiv Title+Abstract Classifier (bert-base-cased)
    ═════════════════════════════════════════════════════════════════════
      Device: cuda  |  Samples: 1000 (train=900, val=100)
      Neg:Pos = 3.5:1  |  Sampler -> balanced  |  Loss: weighted cross entropy
      Epochs=10  |  BS=128  |  LR=2.00e-05
      MaxLen=512  |  Warmup=0.10  |  Seed=42
    ─────────────────────────────────────────────────────────────────────
    """
    # 计算正负样本比例
    ratio = float("inf") if positive_count == 0 else negative_count / positive_count
    ratio_text = "inf" if ratio == float("inf") else f"{ratio:.1f}"
    return [
        "═" * 70,
        f"  arXiv Title+Abstract Classifier ({config.model_name.split('/')[-1]})",
        "═" * 70,
        f"  Device: {device}  |  Samples: {sample_count} (train={train_count}, val={val_count})",
        f"  Neg:Pos = {ratio_text}:1  |  Sampler -> balanced  |  Loss: weighted cross entropy",
        f"  Epochs={config.num_epochs}  |  BS={config.batch_size}  |  LR={config.learning_rate:.2e}",
        f"  MaxLen={config.max_len}  |  Warmup={config.warmup_proportion:.2f}  |  Seed={config.random_seed}",
        "─" * 70,
    ]


def log_epoch_header(epoch: int, total_epochs: int, learning_rate: float) -> None:
    """
    打印每个 epoch 开始时的标题头

    输出示例：
    ============================================================================
      Epoch 3/10  |  LR 1.500000e-05
    ============================================================================
    """
    print(f"\n{'=' * 76}")
    print(f"  Epoch {epoch}/{total_epochs}  |  LR {learning_rate:.6e}")
    print(f"{'=' * 76}")


# =============================================================================
# 数据预处理函数
# =============================================================================


def prepare_training_frame(df: pd.DataFrame) -> pd.DataFrame:
    """
    预处理训练数据框

    步骤：
        1. 重命名列：Title -> title, Abstract -> abstract
        2. 检查必需列是否存在（title, abstract, label）
        3. 处理空值：将 NaN 替换为空字符串
        4. 确保数据类型正确

    参数：
        df: 原始数据框

    返回：
        处理后的数据框，只包含 title, abstract, label 三列

    抛出：
        ValueError: 缺少必需列时
    """
    renamed = df.rename(
        columns={
            "Title": "title",
            "Abstract": "abstract",
        }
    )

    # 检查必需列
    required_columns = {"title", "abstract", "label"}
    missing_columns = required_columns - set(renamed.columns)
    if missing_columns:
        missing = ", ".join(sorted(missing_columns))
        raise ValueError(f"Missing required columns: {missing}")

    # 选择需要的列并处理数据类型
    prepared = renamed.loc[:, ["title", "abstract", "label"]].copy()
    prepared["title"] = prepared["title"].fillna("").astype(str)
    prepared["abstract"] = prepared["abstract"].fillna("").astype(str)
    prepared["label"] = prepared["label"].astype(int)
    return prepared


def split_train_validation_frame(
    df: pd.DataFrame,
    validation_size: float,
    random_seed: int,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    将数据集划分为训练集和验证集

    使用分层抽样（stratify），确保训练集和验证集的正负样本比例一致。

    参数：
        df: 完整数据框
        validation_size: 验证集比例（0.1 表示 10%）
        random_seed: 随机种子

    返回：
        (train_df, val_df) 元组
    """
    train_df, val_df = train_test_split(
        df,
        test_size=validation_size,
        stratify=df["label"],  # 分层抽样，保持标签分布一致
        random_state=random_seed,
    )
    return pd.DataFrame(train_df), pd.DataFrame(val_df)


# =============================================================================
# PyTorch Dataset 类
# =============================================================================


class TitleAbstractDataset(Dataset[dict[str, Any]]):
    """
    arXiv 论文标题+摘要数据集

    将论文的标题和摘要转换为 BERT 可处理的 token 序列。
    标题作为 Segment A，摘要作为 Segment B。

    BERT 输入格式：
        [CLS] 标题 tokens [SEP] 摘要 tokens [SEP] [PAD]...

    参数：
        titles: 标题列表
        abstracts: 摘要列表
        labels: 标签列表（0 或 1）
        tokenizer: BERT 分词器
        max_len: 最大序列长度
    """

    def __init__(
        self,
        titles: list[str],
        abstracts: list[str],
        labels: list[int],
        tokenizer,
        max_len: int,
    ):
        # 使用分词器处理文本对（title + abstract）
        encodings = tokenizer(
            titles,
            text_pair=abstracts,  # 摘要作为第二个文本段
            add_special_tokens=True,  # 自动添加 [CLS], [SEP] 等特殊 token
            max_length=max_len,
            padding=False,  # 不在此处 padding，由 collate_fn 动态处理
            truncation="only_second",  # 仅截断摘要（Segment B），完整保留标题（Segment A）
        )
        self.input_ids: list[list[int]] = encodings["input_ids"]
        self.attention_mask: list[list[int]] = encodings["attention_mask"]
        self.token_type_ids: list[list[int]] = encodings["token_type_ids"]
        self.labels = labels

    def __len__(self) -> int:
        """返回数据集大小"""
        return len(self.labels)

    def __getitem__(self, idx: int) -> dict[str, Any]:
        """
        获取单个样本

        返回：
            {
                "input_ids": token ID 列表,
                "attention_mask": 注意力掩码（1 表示有效 token，0 表示 padding）,
                "labels": 标签值
            }
        """
        return {
            "input_ids": self.input_ids[idx],
            "attention_mask": self.attention_mask[idx],
            "token_type_ids": self.token_type_ids[idx],
            "labels": self.labels[idx],
        }


# =============================================================================
# 动态 Padding 和 DataLoader 创建
# =============================================================================


def dynamic_pad_collate(batch: list[dict[str, Any]], pad_id: int = 0) -> dict[str, torch.Tensor]:
    """
    动态 padding 函数：将一个批次内的样本 padding 到相同长度

    为什么使用动态 padding？
        - 不同论文的长度差异很大
        - 如果预先 pad 到 max_len，会浪费大量计算资源
        - 动态 pad 只 pad 到当前批次的最大长度，节省显存和计算

    参数：
        batch: 样本列表，每个样本是一个字典
        pad_id: padding token 的 ID，通常是 0（[PAD] token）

    返回：
        包含批次张量的字典，所有序列已 pad 到相同长度
    """
    # 找出批次内最长的序列
    max_len = max(
        len(sample_input_ids) for sample_input_ids in [sample["input_ids"] for sample in batch]
    )
    input_ids = []
    attention_mask = []
    token_type_ids = []
    labels = []

    for sample in batch:
        sample_input_ids = list(sample["input_ids"])
        sample_attention_mask = list(sample["attention_mask"])
        sample_token_type_ids = list(sample["token_type_ids"])
        pad_len = max_len - len(sample_input_ids)  # 需要填充的长度

        # 在末尾添加 padding
        input_ids.append(sample_input_ids + [pad_id] * pad_len)
        attention_mask.append(sample_attention_mask + [0] * pad_len)  # padding 位置的 mask 为 0
        token_type_ids.append(
            sample_token_type_ids + [0] * pad_len
        )  # padding 归入 Segment A（type 0）
        labels.append(sample["labels"])

    return {
        "input_ids": torch.tensor(input_ids, dtype=torch.long),
        "attention_mask": torch.tensor(attention_mask, dtype=torch.long),
        "token_type_ids": torch.tensor(token_type_ids, dtype=torch.long),
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
    """
    创建数据加载器

    参数：
        df: 包含 title, abstract, label 列的数据框
        tokenizer: BERT 分词器
        max_len: 最大序列长度
        batch_size: 批次大小
        sampler: 自定义采样器（用于处理类别不平衡）
        num_workers: 数据加载进程数
        pin_memory: 是否使用锁页内存

    返回：
        PyTorch DataLoader 实例

    说明：
        - 如果提供了 sampler，则 shuffle 必须为 False
        - 使用 partial 绑定 pad_id 到 collate_fn
    """
    dataset = TitleAbstractDataset(
        titles=df["title"].tolist(),
        abstracts=df["abstract"].tolist(),
        labels=df["label"].tolist(),
        tokenizer=tokenizer,
        max_len=max_len,
    )

    # 创建 collate 函数，绑定 padding token ID
    collate_fn = partial(dynamic_pad_collate, pad_id=tokenizer.pad_token_id or 0)

    # 设置随机种子（用于 DataLoader shuffle/sampler 和 worker 进程）
    generator = None
    worker_init_fn = None
    if random_seed is not None:
        generator = torch.Generator()
        generator.manual_seed(random_seed)
        worker_init_fn = _seed_worker

    # 获取数据加载器配置
    loader_kwargs = build_loader_kwargs(num_workers=num_workers, pin_memory=pin_memory)
    loader_num_workers = cast(int, loader_kwargs["num_workers"])
    loader_pin_memory = cast(bool, loader_kwargs["pin_memory"])
    loader_persistent_workers = cast(bool, loader_kwargs["persistent_workers"])

    # 多进程加载需要额外配置
    if num_workers > 0:
        return DataLoader(
            dataset,
            batch_size=batch_size,
            sampler=sampler,
            shuffle=sampler is None,  # 有 sampler 时不能 shuffle
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
    """
    执行一个 epoch 的训练

    训练流程：
        1. 将模型设为训练模式
        2. 遍历所有批次：
           a. 前向传播
           b. 计算损失
           c. 反向传播
           d. 梯度裁剪（防止梯度爆炸）
           e. 更新参数
           f. 更新学习率
        3. 计算平均损失和准确率

    参数：
        model: BERT 分类模型
        data_loader: 训练数据加载器
        optimizer: 优化器
        scheduler: 学习率调度器
        device: 计算设备
        loss_fn: 损失函数
        use_amp: 是否使用混合精度
        amp_dtype: 混合精度数据类型

    返回：
        (accuracy, average_loss) 元组
        - accuracy: 训练集准确率
        - average_loss: 平均损失值
    """
    model.train()
    losses: list[float] = []
    correct_predictions = 0

    for batch in tqdm(data_loader, desc="Training", ascii=True):
        optimizer.zero_grad(set_to_none=True)  # 清零梯度，set_to_none 更高效

        # 前向传播
        logits, labels = forward_logits(model, batch, device, use_amp, amp_dtype)

        # 计算损失
        loss = loss_fn(logits.float(), labels)
        loss.backward()

        # 梯度裁剪，防止梯度爆炸
        torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=1.0)

        # 更新参数
        optimizer.step()
        scheduler.step()

        # 统计准确率
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
    threshold_beta: float = 1.0,
):
    """
    评估模型在验证集上的性能

    评估流程：
        1. 将模型设为评估模式（关闭 dropout 等）
        2. 禁用梯度计算
        3. 遍历所有批次，收集预测结果
        4. 计算准确率、分类报告和混淆矩阵

    参数：
        model: BERT 分类模型
        data_loader: 验证数据加载器
        device: 计算设备
        use_amp: 是否使用混合精度
        amp_dtype: 混合精度数据类型

    返回：
        (accuracy, average_loss, report, cm) 元组
        - accuracy: 验证集准确率
        - average_loss: 平均损失值
        - report: sklearn 分类报告（包含 precision, recall, f1-score）
        - cm: 混淆矩阵
    """
    model.eval()
    losses: list[float] = []
    all_labels: list[int] = []
    all_positive_probs: list[float] = []
    loss_fn = torch.nn.CrossEntropyLoss()

    with torch.no_grad():  # 评估时不需要计算梯度
        for batch in tqdm(data_loader, desc="Validation", ascii=True):
            logits, labels = forward_logits(model, batch, device, use_amp, amp_dtype)
            logits = logits.float()  # 确保使用 float32 计算损失
            loss = loss_fn(logits, labels)
            positive_probs = torch.softmax(logits, dim=1)[:, 1]

            losses.append(loss.item())
            all_labels.extend(labels.cpu().tolist())
            all_positive_probs.extend(positive_probs.cpu().tolist())

    optimal_threshold, threshold_score = find_optimal_threshold(
        all_labels,
        all_positive_probs,
        beta=threshold_beta,
    )
    all_preds = [1 if prob >= optimal_threshold else 0 for prob in all_positive_probs]
    accuracy = sum(
        pred == label for pred, label in zip(all_preds, all_labels, strict=True)
    ) / len(all_labels)
    # 生成分类报告
    report = cast(
        str,
        classification_report(
            all_labels,
            all_preds,
            target_names=["negative", "positive"],
        ),
    )
    # 生成混淆矩阵
    cm = confusion_matrix(all_labels, all_preds, labels=[0, 1])
    true_positive = int(cm[1, 1])
    false_positive = int(cm[0, 1])
    false_negative = int(cm[1, 0])
    positive_precision = true_positive / max(true_positive + false_positive, 1)
    positive_recall = true_positive / max(true_positive + false_negative, 1)
    positive_f1 = (2 * positive_precision * positive_recall) / max(
        positive_precision + positive_recall,
        1e-12,
    )
    average_loss = sum(losses) / len(losses)
    return ValidationMetrics(
        accuracy=accuracy,
        average_loss=average_loss,
        report=report,
        cm=cm,
        positive_recall=float(positive_recall),
        positive_f1=float(positive_f1),
        threshold_score=threshold_score,
        optimal_threshold=optimal_threshold,
    )


def find_optimal_threshold(
    labels: list[int], positive_probs: list[float], beta: float = 1.0
) -> tuple[float, float]:
    if not labels or not positive_probs:
        return 0.5, 0.0
    if beta <= 0:
        raise ValueError("beta must be positive")

    precision, recall, thresholds = precision_recall_curve(labels, positive_probs)
    if len(thresholds) == 0:
        return 0.5, 0.0

    beta_sq = beta * beta
    fbeta_scores = ((1 + beta_sq) * precision[:-1] * recall[:-1]) / np.clip(
        (beta_sq * precision[:-1]) + recall[:-1], 1e-12, None
    )
    best_index = int(np.nanargmax(fbeta_scores))
    return float(thresholds[best_index]), float(fbeta_scores[best_index])


# =============================================================================
# 类别平衡工具
# =============================================================================


def build_weighted_sampler(labels: list[int], class_weights: torch.Tensor) -> WeightedRandomSampler:
    """
    构建加权随机采样器，用于处理类别不平衡问题

    原理：
        - 为每个样本分配一个采样权重
        - 少数类样本获得更高的权重
        - 采样时按权重随机抽取，使每个 epoch 内各类别样本数趋于平衡

    参数：
        labels: 所有样本的标签列表
        class_weights: 各类别的权重张量

    返回：
        WeightedRandomSampler 实例

    示例：
        假设有 90 个负样本（label=0）和 10 个正样本（label=1）
        class_weights = [0.1, 0.9]  # 正样本权重更高
        采样器会更多地抽取正样本，使训练时正负样本数量接近
    """
    # 为每个样本分配其所属类别的权重
    sample_weights = [class_weights[label].item() for label in labels]
    return WeightedRandomSampler(
        weights=sample_weights,
        num_samples=len(sample_weights),
        replacement=True,  # 允许重复采样，确保少数类能被多次采样
    )


# =============================================================================
# 模型保存
# =============================================================================


def save_training_config(
    output_dir: Path,
    config: TrainingConfig,
    best_accuracy: float,
    best_positive_f1: float,
    optimal_threshold: float,
) -> None:
    """
    保存训练配置到 JSON 文件

    保存内容：
        - 模型名称
        - 最大序列长度
        - 最佳验证准确率
        - 训练版本标识

    参数：
        output_dir: 输出目录
        config: 训练配置
        best_accuracy: 最佳验证准确率
    """
    payload = {
        "model_name": config.model_name,
        "max_len": config.max_len,
        "best_validation_accuracy": float(best_accuracy),
        "best_positive_f1": float(best_positive_f1),
        "optimal_threshold": float(optimal_threshold),
        "threshold_beta": float(config.threshold_beta),
        "train_version": "title_abstract_v2",
        "input_mode": "title_abstract",
    }
    with output_dir.joinpath("training_config.json").open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)


# =============================================================================
# 主训练流程
# =============================================================================


def main(config: TrainingConfig = CONFIG) -> None:
    """
    主训练流程

    步骤：
        1. 加载并预处理数据
        2. 划分训练集和验证集
        3. 计算类别权重和处理类别不平衡
        4. 创建数据加载器
        5. 加载预训练模型和分词器
        6. 创建优化器和学习率调度器
        7. 训练循环：
           - 每个 epoch 训练并验证
           - 保存最佳模型
        8. 输出最终评估结果
    """
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
    # "balanced" 模式：权重 = 总样本数 / (类别数 * 该类别样本数)
    class_weights = compute_class_weight(
        class_weight="balanced",
        classes=np.unique(train_df.loc[:, "label"]),
        y=train_df.loc[:, "label"],
    )
    class_weights_tensor = torch.tensor(class_weights, dtype=torch.float, device=DEVICE)

    # 3. 创建加权损失函数和采样器
    loss_fn = torch.nn.CrossEntropyLoss(weight=class_weights_tensor)
    sampler = build_weighted_sampler(train_df.loc[:, "label"].tolist(), class_weights_tensor)

    # 统计正负样本数量
    negative_count = int((train_df.loc[:, "label"] == 0).sum())
    positive_count = int((train_df.loc[:, "label"] == 1).sum())

    # 打印训练配置信息
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

    # 4. 确定数据加载进程数
    num_workers = (
        config.num_workers if config.num_workers is not None else min(8, os.cpu_count() or 4)
    )

    # 5. 加载分词器和创建数据加载器
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

    # 6. 加载预训练模型
    _log(f"  Loading model {config.model_name.split('/')[-1]}...")
    model = AutoModelForSequenceClassification.from_pretrained(config.model_name, num_labels=2)
    model.to(DEVICE)

    # 7. 创建优化器和学习率调度器
    optimizer = create_optimizer(
        model.parameters(),
        learning_rate=config.learning_rate,
        use_fused=runtime["use_fused"],
    )
    total_steps = len(train_loader) * config.num_epochs
    # 学习率调度器：先预热（线性增加），然后线性衰减
    scheduler = get_linear_schedule_with_warmup(
        optimizer,
        num_warmup_steps=int(config.warmup_proportion * total_steps),
        num_training_steps=total_steps,
    )

    best_accuracy = 0.0
    best_positive_f1 = 0.0
    best_threshold_score = 0.0
    best_threshold = 0.5
    config.output_dir.mkdir(parents=True, exist_ok=True)

    # 8. 训练循环
    for epoch in range(1, config.num_epochs + 1):
        log_epoch_header(epoch, config.num_epochs, optimizer.param_groups[-1]["lr"])

        # 训练一个 epoch
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

        # 在验证集上评估
        val_metrics = eval_model(
            model,
            val_loader,
            DEVICE,
            use_amp=runtime["use_amp"],
            amp_dtype=runtime["amp_dtype"],
            threshold_beta=config.threshold_beta,
        )

        # 检查是否是最佳模型
        is_best = (val_metrics.threshold_score > best_threshold_score) or (
            val_metrics.threshold_score == best_threshold_score
            and val_metrics.accuracy >= best_accuracy
        )
        _log(
            "Valid | loss={loss:.4f}  acc={acc:.4f}  pos_recall={pos_recall:.4f}  pos_f1={pos_f1:.4f}  fbeta={fbeta:.4f}  thr={thr:.4f}  {star}".format(
                loss=val_metrics.average_loss,
                acc=val_metrics.accuracy,
                pos_recall=val_metrics.positive_recall,
                pos_f1=val_metrics.positive_f1,
                fbeta=val_metrics.threshold_score,
                thr=val_metrics.optimal_threshold,
                star="★" if is_best else "",
            )
        )
        print(val_metrics.report)
        print("Confusion Matrix (labels=[negative, positive]):")
        print(val_metrics.cm)

        # 如果是最佳模型，保存之
        if is_best:
            best_accuracy = val_metrics.accuracy
            best_positive_f1 = val_metrics.positive_f1
            best_threshold_score = val_metrics.threshold_score
            best_threshold = val_metrics.optimal_threshold
            _log(
                f"      | ★ New best (recall={val_metrics.positive_recall:.4f}, pos_f1={best_positive_f1:.4f}, fbeta={best_threshold_score:.4f}, thr={best_threshold:.4f}), saving -> {config.output_dir}"
            )
            model.save_pretrained(config.output_dir)
            tokenizer.save_pretrained(config.output_dir)
            save_training_config(
                config.output_dir,
                config,
                best_accuracy,
                best_positive_f1,
                best_threshold,
            )

    # 9. 打印最终结果
    _log("═" * 70)
    _log("  Final Evaluation")
    _log("═" * 70)
    _log(f"  Best validation accuracy: {best_accuracy:.4f}")
    _log(f"  Best positive F1: {best_positive_f1:.4f}")
    _log(f"  Optimal threshold: {best_threshold:.4f}")
    _log("─" * 70)


if __name__ == "__main__":
    main()
