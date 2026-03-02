#!/usr/bin/env python
"""
arXiv 论文标题推理模块

使用预训练的 BERT 模型对 arXiv 论文标题进行分类，筛选出感兴趣的论文。
"""

import argparse
import json
import logging
import os
import sys
from typing import Optional

import pandas as pd
import torch
import torch.nn.functional as F
from torch.utils.data import Dataset, DataLoader
from transformers import BertTokenizer, BertForSequenceClassification

logger = logging.getLogger(__name__)

def _get_arxiv_fetcher():
    """动态获取 arxiv 数据获取函数"""
    try:
        from .arxiv_today import get_today_arxiv
        return get_today_arxiv
    except ImportError:
        return None

def _load_config():
    """加载配置文件"""
    config_path = os.path.join(os.path.dirname(__file__), "config.json")
    if os.path.exists(config_path):
        with open(config_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}

def _load_model_and_tokenizer(model_path: str, device: torch.device):
    """加载模型和分词器（公共函数，避免代码重复）"""
    # 处理相对路径
    if not os.path.isabs(model_path):
        current_dir = os.path.dirname(os.path.abspath(__file__))
        model_path = os.path.join(current_dir, model_path)
    
    # 验证路径
    if not os.path.exists(model_path):
        raise FileNotFoundError(f"Model path does not exist: {model_path}")
    
    if not os.path.isdir(model_path):
        raise ValueError(f"Model path must be a directory: {model_path}")
    
    # 加载模型和分词器
    model = BertForSequenceClassification.from_pretrained(model_path)
    tokenizer = BertTokenizer.from_pretrained(model_path)
    model.to(device)
    model.eval()
    
    return model, tokenizer

class TitleDataset(Dataset):
    """论文标题数据集"""
    
    def __init__(self, titles: list[str], tokenizer: BertTokenizer, max_len: int = 64):
        self.titles = titles
        self.tokenizer = tokenizer
        self.max_len = max_len

    def __len__(self) -> int:
        return len(self.titles)

    def __getitem__(self, idx: int):
        text = str(self.titles[idx])
        encoding = self.tokenizer.encode_plus(
            text,
            add_special_tokens=True,
            max_length=self.max_len,
            padding="max_length",
            truncation=True,
            return_attention_mask=True,
            return_tensors="pt",
        )
        return {
            "input_ids": encoding["input_ids"].squeeze(0),
            "attention_mask": encoding["attention_mask"].squeeze(0),
        }

def predict_titles(
    model: BertForSequenceClassification,
    tokenizer: BertTokenizer,
    titles: list[str],
    threshold: float = 0.5,
    batch_size: int = 32,
    max_len: int = 64,
    device: Optional[torch.device] = None
) -> tuple[list[float], list[int]]:
    """
    对一批标题做推理，返回正类的概率和二值预测。
    
    Args:
        model: BERT 分类模型
        tokenizer: BERT 分词器
        titles: 论文标题列表
        threshold: 分类阈值
        batch_size: 批处理大小
        max_len: 最大 token 长度
        device: 计算设备
        
    Returns:
        (概率列表, 预测列表)
    """
    if device is None:
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model.eval()
    ds = TitleDataset(titles, tokenizer, max_len=max_len)
    loader = DataLoader(ds, batch_size=batch_size)

    all_probs = []
    with torch.no_grad():
        for batch in loader:
            input_ids = batch["input_ids"].to(device)
            attention_mask = batch["attention_mask"].to(device)

            outputs = model(input_ids=input_ids, attention_mask=attention_mask)
            logits = outputs.logits  # (batch, 2)
            probs = F.softmax(logits, dim=1)[:, 1]  # 正类的概率
            all_probs.extend(probs.cpu().tolist())

    preds = [1 if p >= threshold else 0 for p in all_probs]
    return all_probs, preds

def get_positive_arxiv_today_as_string(
    model_path: Optional[str] = None,
    threshold: Optional[float] = None,
    batch_size: Optional[int] = None,
    max_len: Optional[int] = None
) -> str:
    """
    获取今日 arXiv 论文，执行推理并返回正类结果的格式化字符串。

    Args:
        model_path: 模型目录路径（默认从配置文件读取）
        threshold: 分类阈值（默认从配置文件读取）
        batch_size: 推理批大小（默认从配置文件读取）
        max_len: 最大 token 长度（默认从配置文件读取）

    Returns:
        格式化的结果字符串
    """
    # 加载配置
    config = _load_config()
    model_config = config.get("model", {})
    
    # 使用配置文件中的默认值
    if model_path is None:
        model_path = model_config.get("path", "best_model")
    if threshold is None:
        threshold = model_config.get("threshold", 0.5)
    if batch_size is None:
        batch_size = model_config.get("batch_size", 32)
    if max_len is None:
        max_len = model_config.get("max_len", 64)
    
    # 动态获取 arxiv 函数
    get_today_arxiv = _get_arxiv_fetcher()
    if get_today_arxiv is None:
        return "Error: get_today_arxiv function is not available. Check if arxiv_today.py exists."

    # 获取今日论文
    try:
        data = get_today_arxiv()
    except Exception as e:
        logger.error(f"Error fetching today's papers: {e}")
        return f"Error fetching today's papers: {str(e)}"

    # 设置设备
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    # 加载模型和分词器（使用公共函数）
    try:
        model, tokenizer = _load_model_and_tokenizer(model_path, device)
    except Exception as e:
        logger.error(f"Error loading model: {e}")
        return f"Error loading model from {model_path}: {str(e)}"

    # Get titles
    if "Title" not in data.columns:
        return "Error: Input data must contain a 'Title' column."
    titles = data["Title"].tolist()

    # Run inference
    probs, preds = predict_titles(
        model,
        tokenizer,
        titles,
        threshold=threshold,
        batch_size=batch_size,
        max_len=max_len,
        device=device,
    )

    # Add predictions to dataframe
    data["Probability"] = probs
    data["Prediction"] = preds

    # Format output as string
    output = []
    positives = data[data["Prediction"] == 1].reset_index(drop=True)

    if len(positives) == 0:
        return "No positive predictions found."

    for i, row in positives.iterrows():
        output.append(f"\n----- Positive #{i+1} -----")
        output.append(f"Title      : {row['Title']}")
        if "arXiv ID" in data.columns:
            arxiv_id = row["arXiv ID"].split('v')[0]
            output.append(f"Link       : https://arxiv.org/abs/{arxiv_id}")
        output.append(f"Probability: {row['Probability']:.4f}")

    return "\n".join(output)

def main():
    parser = argparse.ArgumentParser(description="ArXiv title classifier inference")
    parser.add_argument(
        "--model",
        type=str,
        default="best_model",
        help="Path to the trained model (directory or .pt file)",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.5,
        help="Classification threshold (on positive-class probability)",
    )
    parser.add_argument(
        "--input",
        type=str,
        help='Input CSV file with titles (should have a "Title" column)',
    )
    parser.add_argument(
        "--show", action="store_true", help="Display positive predictions in the console"
    )
    parser.add_argument(
        "--batch_size", type=int, default=32, help="Batch size for inference"
    )
    parser.add_argument(
        "--max_len", type=int, default=64, help="Max token length for tokenizer"
    )
    args = parser.parse_args()

    # 设备
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    logging.info(f"Using device: {device}")

    # 加载模型 & tokenizer（使用公共函数）
    try:
        model, tokenizer = _load_model_and_tokenizer(args.model, device)
    except Exception as e:
        logging.error(f"Error loading model: {e}")
        sys.exit(1)

    # 读取标题
    if args.input:
        data = pd.read_csv(args.input)
    else:
        if get_today_arxiv is None:
            logging.error("未提供 --input，也没有实现 get_today_arxiv()")
            sys.exit(1)
        data = get_today_arxiv()
    if "Title" not in data.columns:
        parser.error("输入 CSV 必须包含 'Title' 列")
    titles = data["Title"].tolist()
    logging.info(f"Loaded {len(titles)} titles from {'today_arxiv()' if not args.input else args.input}")

    # 推理
    probs, preds = predict_titles(
        model,
        tokenizer,
        titles,
        threshold=args.threshold,
        batch_size=args.batch_size,
        max_len=args.max_len,
        device=device,
    )
    logging.info("Inference completed")

    # 保存结果
    data["Probability"] = probs
    data["Prediction"] = preds
    out_csv = args.input or "today_arxiv.csv"
    data.to_csv(out_csv, index=False)
    logging.info(f"Results saved to {out_csv}")

    # 控制台输出
    if args.show:
        positives = data[data["Prediction"] == 1].reset_index(drop=True)
        for i, row in positives.iterrows():
            print(f"\n----- Positive #{i+1} -----")
            print(f"Title      : {row['Title']}")
            print(f"Probability: {row['Probability']:.4f}")

if __name__ == "__main__":
    main()
