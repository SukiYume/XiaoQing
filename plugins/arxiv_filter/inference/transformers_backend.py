#!/usr/bin/env python
"""
Transformers (BERT) 模型推理后端。

输入：InferenceParams + DataFrame (含 Title 列，可选 Abstract 列)
输出：(probs: list[float], preds: list[int])
"""

import importlib
import logging
import os
from functools import partial

import pandas as pd
import torch
import torch.nn.functional as F
from torch.utils.data import DataLoader, Dataset

from .shared import InferenceParams

logger = logging.getLogger(__name__)

transformers = importlib.import_module("transformers")
AutoModelForSequenceClassification = transformers.AutoModelForSequenceClassification
AutoTokenizer = transformers.AutoTokenizer

_MODEL_CACHE: dict[tuple[str, str], tuple[object, object]] = {}


# =============================================================================
# Dataset & Collate
# =============================================================================

class TitleAbstractDataset(Dataset):
    def __init__(self, titles: list[str], tokenizer, abstracts: list[str] | None = None,
                 max_len: int = 512):
        truncation = "only_second" if abstracts is not None else True
        enc = tokenizer(titles, text_pair=abstracts, add_special_tokens=True,
                        max_length=max_len, padding=False, truncation=truncation)
        self.input_ids = enc["input_ids"]
        self.attention_mask = enc["attention_mask"]
        self.token_type_ids = enc.get("token_type_ids")

    def __len__(self) -> int:
        return len(self.input_ids)

    def __getitem__(self, idx: int):
        item = {"input_ids": self.input_ids[idx], "attention_mask": self.attention_mask[idx]}
        if self.token_type_ids is not None:
            item["token_type_ids"] = self.token_type_ids[idx]
        return item


def dynamic_pad_collate(batch, pad_id: int = 0):
    max_len = max(len(b["input_ids"]) for b in batch)
    has_tti = "token_type_ids" in batch[0]
    input_ids, attn, tti = [], [], []
    for b in batch:
        pad = max_len - len(b["input_ids"])
        input_ids.append(b["input_ids"] + [pad_id] * pad)
        attn.append(b["attention_mask"] + [0] * pad)
        if has_tti:
            tti.append(b["token_type_ids"] + [0] * pad)
    result = {
        "input_ids": torch.tensor(input_ids, dtype=torch.long),
        "attention_mask": torch.tensor(attn, dtype=torch.long),
    }
    if has_tti:
        result["token_type_ids"] = torch.tensor(tti, dtype=torch.long)
    return result


# =============================================================================
# 模型加载 & 预测
# =============================================================================

def load_model_and_tokenizer(model_path: str, device: torch.device):
    if not os.path.isdir(model_path):
        raise FileNotFoundError(f"Model path does not exist or is not a directory: {model_path}")
    cache_key = (os.path.abspath(model_path), str(device))
    cached = _MODEL_CACHE.get(cache_key)
    if cached is not None:
        return cached

    model = AutoModelForSequenceClassification.from_pretrained(model_path).to(device).eval()
    tokenizer = AutoTokenizer.from_pretrained(model_path)
    _MODEL_CACHE[cache_key] = (model, tokenizer)
    return model, tokenizer


def run_transformers_inference(
    params: InferenceParams, data: pd.DataFrame, device: torch.device,
) -> tuple[list[float], list[int]]:
    """执行 BERT 类推理，根据 input_mode 自动决定是否使用 abstract。"""
    model, tokenizer = load_model_and_tokenizer(params.model_path, device)

    if "Title" not in data.columns:
        raise ValueError("Input data must contain a 'Title' column.")
    titles = data["Title"].fillna("").astype(str).tolist()

    abstracts = None
    if params.input_mode != "title_only" and "Abstract" in data.columns:
        abstracts = data["Abstract"].fillna("").astype(str).tolist()
        logger.info("input_mode=%s: 使用标题+摘要推理 (%d/%d 篇有摘要)",
                     params.input_mode, sum(1 for a in abstracts if a), len(abstracts))
    else:
        logger.info("input_mode=%s: 仅使用标题推理", params.input_mode)

    ds = TitleAbstractDataset(titles, tokenizer, abstracts=abstracts, max_len=params.max_len)
    loader = DataLoader(ds, batch_size=params.batch_size,
                        collate_fn=partial(dynamic_pad_collate, pad_id=tokenizer.pad_token_id or 0))

    all_probs: list[float] = []
    with torch.no_grad():
        for batch in loader:
            kw = {k: v.to(device) for k, v in batch.items()}
            logits = model(**kw).logits
            all_probs.extend(F.softmax(logits, dim=1)[:, 1].cpu().tolist())

    preds = [1 if p >= params.threshold else 0 for p in all_probs]
    return all_probs, preds
