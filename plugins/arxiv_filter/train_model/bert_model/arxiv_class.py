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
    precision_recall_curve,
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
AutoModelForSequenceClassification = transformers.AutoModelForSequenceClassification  # 序列分类模型
AutoTokenizer = transformers.AutoTokenizer  # 分词器
get_linear_schedule_with_warmup = (
    transformers.get_linear_schedule_with_warmup
)  # 带预热的学习率调度器

_PLUGIN_DIR = Path(__file__).resolve().parents[2]


# =============================================================================
# 训练配置类
# =============================================================================


@dataclass(frozen=True, slots=True)
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

    data_path: Path = _PLUGIN_DIR / "train_model" / "arxiv_papers_with_abstract.csv"
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
    output_dir: Path = field(default_factory=lambda: _PLUGIN_DIR / "best_model")


@dataclass(frozen=True, slots=True)
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
    prepared["label"] = _training.coerce_binary_labels(prepared["label"])
    return prepared


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
    accuracy = sum(pred == label for pred, label in zip(all_preds, all_labels, strict=True)) / len(
        all_labels
    )
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
    training = _training.prepare_classifier_training(
        config,
        device=DEVICE,
        classifier_name="arXiv Title+Abstract Classifier",
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
    best_positive_f1 = 0.0
    best_threshold_score = 0.0
    best_threshold = 0.5

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
