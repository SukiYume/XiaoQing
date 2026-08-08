"""
arXiv k-NN 兴趣模型训练脚本
============================

核心思想：直接把过往感兴趣的论文 embedding 存成"兴趣库"，
对每篇新论文计算它与兴趣库中最相似的 k 篇论文的平均相似度，
作为推荐得分。不做任何聚类，彻底消除 FRB/WD/Pulsar 分布失衡问题。

优势：
  - 无聚类分布偏差：每篇历史正样本对推荐的贡献完全平等
  - WD / Pulsar 论文天然被覆盖：只要新论文与任何一篇正样本相似就能被召回
  - 无超参地狱：只有 k（邻居数）和阈值两个关键参数
  - 可解释性强：可以输出"匹配了哪几篇历史论文"

修复 / 新增（v2）：
  [FIX]  output_dir 指向 plugins/arxiv_filter/best_model_knn/（推理系统期望的路径）
  [NEW]  embedding 缓存：首次编码后存盘，后续训练直接从磁盘加载，节省数十分钟
  [NEW]  per-topic recall 分析：训练结束时输出 FRB / Pulsar / WD / Gaia 等子领域通过率
"""

from __future__ import annotations  # noqa: I001 - torch must load before NumPy on Windows

import hashlib
import json
import math
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import torch

import numpy as np
import pandas as pd

from sklearn.metrics import (
    average_precision_score,
    classification_report,
    f1_score,
    roc_auc_score,
)

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
    from plugins.arxiv_filter.train_model.interest_model import training_utils as _training
else:
    from . import training_utils as _training

_log = _training.timestamp_log

# =============================================================================
# 路径常量
# =============================================================================

# knn_arxiv.py 位于 plugins/arxiv_filter/train_model/interest_model/
# parents[0] = interest_model/
# parents[1] = train_model/
# parents[2] = plugins/arxiv_filter/   ← 推理系统期望模型在这一层
_SCRIPT_DIR = Path(__file__).resolve().parent  # …/interest_model/
_TRAIN_DIR = _SCRIPT_DIR.parent  # …/train_model/
_PLUGIN_DIR = _TRAIN_DIR.parent  # …/arxiv_filter/

# =============================================================================
# 配置
# =============================================================================


@dataclass(frozen=True, slots=True)
class KNNConfig:
    input_path: Path = _TRAIN_DIR / "arxiv_papers_with_abstract.csv"

    # 训练产物固定写入运行时加载器所使用的 best_model_knn 目录。
    output_dir: Path = _PLUGIN_DIR / "best_model_knn"

    # 默认跟随 output_dir；显式指定时可把大体积缓存放到其他磁盘。
    emb_cache_dir: Path | None = None

    encoder_name: str = "sentence-transformers/all-mpnet-base-v2"

    # ── k-NN 参数 ──────────────────────────────────────────────────────────
    # k=10：对每篇新论文取 top-10 最相似正样本的平均相似度作为得分
    # 较小的 k 对稀有话题（WD, Pulsar）更敏感；较大的 k 更稳健
    k: int = 10

    # 负样本惩罚：score = top_k_pos_mean - neg_weight * top_k_neg_mean
    # neg_weight=0 表示不使用负样本惩罚
    neg_k: int = 5
    neg_weight: float = 0.5

    # 存储负样本子集的数量（全量 ~98k 没必要，3000 足以代表负样本空间）
    neg_sample_size: int = 3000

    batch_size: int = 256
    val_size: float = 0.15

    # beta=1.0 → F1（Precision/Recall 对等）
    # beta=2.0 → 偏向 Recall（更多召回，threshold 更低）
    # beta=0.5 → 偏向 Precision（更精准，threshold 更高）
    beta: float = 2.0
    min_threshold: float = 0.30  # 阈值下限，防止被极端不平衡压太低

    split_mode: str = "random"
    random_seed: int = 42
    max_len: int = 512

    def __post_init__(self) -> None:
        """在耗时编码开始前校验全部训练超参数。"""

        if self.emb_cache_dir is None:
            object.__setattr__(self, "emb_cache_dir", self.output_dir / "emb_cache")
        if not isinstance(self.encoder_name, str) or not self.encoder_name.strip():
            raise ValueError("encoder_name must be a non-empty string")
        for name in ("k", "neg_k", "neg_sample_size", "batch_size", "max_len"):
            value = getattr(self, name)
            minimum = 0 if name == "neg_k" else 1
            if type(value) is not int or value < minimum:
                raise ValueError(f"{name} must be an integer >= {minimum}")
        for name, lower_bound in (("neg_weight", 0.0), ("beta", 0.0)):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or float(value) < lower_bound
                or (name == "beta" and float(value) == 0)
            ):
                raise ValueError(f"{name} has an invalid value")
        if not 0 < self.val_size < 1:
            raise ValueError("val_size must be between 0 and 1")
        if not math.isfinite(self.min_threshold):
            raise ValueError("min_threshold must be finite")
        if self.split_mode not in {"random", "time"}:
            raise ValueError("split_mode must be 'random' or 'time'")

    @property
    def resolved_emb_cache_dir(self) -> Path:
        """返回 `__post_init__` 已解析完成的缓存目录。"""

        cache_dir = self.emb_cache_dir
        if cache_dir is None:  # 仅防御绕过 dataclass 构造器的异常对象。
            raise RuntimeError("embedding cache directory was not initialized")
        return cache_dir


CONFIG = KNNConfig()

# ── per-topic 评估关键词（title 模糊匹配，大小写不敏感）────────────────────
# 每个 topic 匹配其中任意一个关键词即算作该 topic 的正样本
TOPIC_KEYWORDS: dict[str, list[str]] = {
    "FRB": ["fast radio burst", r"\bfrb\b", "repeating burst"],
    "Pulsar": [r"\bpulsar\b", "magnetar", r"\bneutron star\b", "spin-down", "timing noise"],
    "WD": ["white dwarf", r"\bwd\b", r"\bdwd\b", "cataclysmic variable", "AM CVn"],
    "Gaia": [r"\bgaia\b", "astrometry", "parallax catalog", "dr[23456]"],
    "ML/Method": [
        "machine learning",
        "neural network",
        "deep learning",
        r"\btransformer\b",
        "convolutional",
        "bayesian inference",
        "gaussian process",
    ],
    "GW/Multi-messenger": [
        "gravitational wave",
        r"\bligo\b",
        r"\bvirgo\b",
        "multimessenger",
        "multi-messenger",
        "kilonova",
    ],
    "Time-domain": [
        "transient",
        "variable star",
        "survey",
        "light curve",
        "flare",
        "supernova",
        r"\bsne\b",
    ],
}


# =============================================================================
# 工具函数
# =============================================================================


def resolve_columns(df: pd.DataFrame) -> dict[str, str | None]:
    return _training.resolve_columns(
        df,
        {
            "id": ["arxivid", "id", "paperid"],
            "title": ["title", "papertitle"],
            "abstract": ["abstract", "summary", "description"],
            "label": ["label", "interest", "target", "y"],
            "date": ["date", "created", "submitted", "published", "updated"],
        },
        required_fields={"title", "abstract"},
        error_prefix="找不到列名",
    )


def split_dataframe(
    df: pd.DataFrame,
    val_size: float,
    split_mode: str,
    seed: int = 42,
    date_col: str | None = None,
    label_col: str | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """切分数据集，返回的 DataFrame 保留 _orig_idx 列以便对齐预计算 embedding。"""
    return _training.split_dataframe(
        df,
        val_size,
        split_mode,
        seed=seed,
        date_col=date_col,
        label_col=label_col or None,
        log=_log,
        missing_date_error="split_mode=time 需要日期列",
        stratify_fallback_message="警告：分层随机切分失败，退回普通随机切分",
    )


# =============================================================================
# ★ 新增：Embedding 缓存
# =============================================================================


def _cache_paths(cache_dir: Path, encoder_name: str) -> tuple[Path, Path]:
    short = re.sub(r"[^a-z0-9\-]", "-", encoder_name.split("/")[-1].lower())
    return (
        cache_dir / f"all_embeddings_{short}.npy",
        cache_dir / f"all_embeddings_{short}_meta.json",
    )


def _data_fingerprint(df: pd.DataFrame, title_col: str, abstract_col: str) -> str:
    """按行哈希标题和摘要，避免“行数相同但内容已变”时误用旧缓存。"""

    digest = hashlib.sha256()
    digest.update(pd.__version__.encode())
    digest.update(title_col.encode())
    digest.update(abstract_col.encode())
    row_hashes = pd.util.hash_pandas_object(
        df.loc[:, [title_col, abstract_col]],
        index=True,
        categorize=True,
    )
    digest.update(row_hashes.to_numpy(dtype=np.uint64, copy=False).tobytes())
    return digest.hexdigest()


def _cache_is_valid(
    meta_path: Path,
    npy_path: Path,
    encoder_name: str,
    n_rows: int,
    data_fingerprint: str,
) -> bool:
    """检查缓存是否存在且与当前数据/编码器匹配。"""
    if not meta_path.exists() or not npy_path.exists():
        return False
    try:
        with open(meta_path, encoding="utf-8") as f:
            meta = json.load(f)
        return (
            isinstance(meta, dict)
            and meta.get("encoder_name") == encoder_name
            and type(meta.get("n_rows")) is int
            and meta["n_rows"] == n_rows
            and meta.get("data_fingerprint") == data_fingerprint
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False


def load_or_encode(
    df: pd.DataFrame,
    title_col: str,
    abstract_col: str,
    encoder_name: str,
    cache_dir: Path,
    batch_size: int = 256,
) -> np.ndarray:
    """
    返回 df 中每一行对应的 normalized embedding，shape = (len(df), D)。

    首次调用会编码全部文本并写入缓存；后续调用直接从缓存加载。
    缓存失效条件：编码器、数据行数，或标题/摘要内容发生变化。
    """
    npy_path, meta_path = _cache_paths(cache_dir, encoder_name)
    data_fingerprint = _data_fingerprint(df, title_col, abstract_col)

    if _cache_is_valid(
        meta_path,
        npy_path,
        encoder_name,
        len(df),
        data_fingerprint,
    ):
        _log(f"[cache] 从磁盘加载 embedding（{npy_path.name}）…")
        try:
            emb = _training.validate_embedding_matrix(
                np.load(npy_path, allow_pickle=False),
                expected_rows=len(df),
                name="cached embeddings",
            )
            _log(f"[cache] 命中，shape={emb.shape}")
            return emb
        except (OSError, ValueError):
            _log("[cache] 缓存损坏，重新编码")

    _log(f"[encode] 编码 {len(df)} 篇论文（encoder={encoder_name}）…")
    _log("[encode] 预计耗时（CPU ~5-15 min；GPU ~1 min）")

    SentenceTransformer = _training.load_sentence_transformer_class()
    model = SentenceTransformer(encoder_name)
    fp16 = torch.cuda.is_available()
    if fp16:
        _log("[encode] CUDA 可用，启用 fp16")

    texts = _training.build_title_abstract_texts(df, title_col, abstract_col)
    kw: dict[str, Any] = {
        "batch_size": batch_size,
        "show_progress_bar": True,
        "convert_to_numpy": True,
        "normalize_embeddings": True,
    }
    if fp16:
        with torch.amp.autocast("cuda"):
            emb = model.encode(texts, **kw)
    else:
        emb = model.encode(texts, **kw)
    emb = _training.validate_embedding_matrix(
        emb,
        expected_rows=len(df),
        name="encoded embeddings",
    )

    # 写入缓存
    cache_dir.mkdir(parents=True, exist_ok=True)
    np.save(npy_path, emb)
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "encoder_name": encoder_name,
                "n_rows": len(df),
                "embed_dim": emb.shape[1],
                "data_fingerprint": data_fingerprint,
            },
            f,
            indent=2,
        )
    _log(f"[cache] 已保存到 {npy_path}（{emb.nbytes / 1024 / 1024:.1f} MB）")
    return emb


# =============================================================================
# ★ 新增：Per-topic Recall 分析
# =============================================================================


def evaluate_per_topic(
    df: pd.DataFrame,
    scores: np.ndarray,
    threshold: float,
    title_col: str,
    label_col: str,
    topic_keywords: dict[str, list[str]] = TOPIC_KEYWORDS,
    name: str = "Validation",
) -> dict[str, dict[str, float]]:
    """
    对验证集的正样本按话题关键词分组，报告每个话题的通过率和均分。

    输出示例：
      FRB      :  112 pos |  91 passed | 81.3% pass rate | mean=0.9199 | min=0.7012 | max=0.9921
      WD       :    8 pos |   0 passed |  0.0% pass rate | mean=0.5254 | min=0.3801 | max=0.6712
    """
    titles = df[title_col].fillna("").astype(str)
    labels = df[label_col].astype(int).to_numpy()
    preds = (scores >= threshold).astype(int)

    results: dict[str, dict[str, float]] = {}
    lines: list[str] = []

    header = f"\n{'=' * 72}\n[{name}] Per-topic Recall Breakdown\n{'=' * 72}"
    lines.append(header)

    for topic, kw_list in topic_keywords.items():
        pattern = "|".join(kw_list)
        topic_mask = titles.str.contains(pattern, case=False, regex=True, na=False).to_numpy()
        pos_topic = (topic_mask) & (labels == 1)
        n_pos = int(pos_topic.sum())
        if n_pos == 0:
            continue

        topic_scores = scores[pos_topic]
        n_passed = int(preds[pos_topic].sum())
        pass_rate = n_passed / n_pos
        mean_s = float(topic_scores.mean())
        min_s = float(topic_scores.min())
        max_s = float(topic_scores.max())

        results[topic] = {
            "n_pos": n_pos,
            "n_passed": n_passed,
            "pass_rate": pass_rate,
            "mean_score": mean_s,
            "min_score": min_s,
            "max_score": max_s,
        }
        lines.append(
            f"  {topic:<22}: {n_pos:4d} pos | {n_passed:4d} passed | "
            f"{100 * pass_rate:5.1f}% pass rate | "
            f"mean={mean_s:.4f} min={min_s:.4f} max={max_s:.4f}"
        )

    # 未被任何话题覆盖的正样本
    covered_mask: np.ndarray = np.zeros(len(df), dtype=bool)
    for kw_list in topic_keywords.values():
        pattern = "|".join(kw_list)
        covered_mask |= titles.str.contains(pattern, case=False, regex=True, na=False).to_numpy()
    uncovered_pos = (~covered_mask) & (labels == 1)
    n_unc = int(uncovered_pos.sum())
    if n_unc > 0:
        unc_scores = scores[uncovered_pos]
        n_unc_pass = int(preds[uncovered_pos].sum())
        lines.append(
            f"  {'Other':22}: {n_unc:4d} pos | {n_unc_pass:4d} passed | "
            f"{100 * n_unc_pass / n_unc:5.1f}% pass rate | "
            f"mean={unc_scores.mean():.4f} min={unc_scores.min():.4f} max={unc_scores.max():.4f}"
        )
        results["Other"] = {
            "n_pos": n_unc,
            "n_passed": n_unc_pass,
            "pass_rate": n_unc_pass / n_unc,
            "mean_score": float(unc_scores.mean()),
            "min_score": float(unc_scores.min()),
            "max_score": float(unc_scores.max()),
        }

    lines.append("─" * 72)

    # Overall
    n_total_pos = int((labels == 1).sum())
    n_total_passed = int(preds[labels == 1].sum())
    lines.append(
        f"  {'TOTAL (all pos)':22}: {n_total_pos:4d} pos | {n_total_passed:4d} passed | "
        f"{100 * n_total_passed / max(n_total_pos, 1):5.1f}% recall"
    )
    lines.append("═" * 72)

    print("\n".join(lines), flush=True)
    return results


# =============================================================================
# 模型
# =============================================================================


class KNNInterestModel:
    """
    k-NN 兴趣模型。

    原理：
      训练时把全部正样本的 embedding 存成"兴趣库"（pos_embeddings）。
      推理时对新论文计算它与兴趣库中最相似 k 篇论文的平均余弦相似度，
      减去它与最相似若干负样本的惩罚项，作为最终得分。

    为何比聚类方案更好：
      KMeans 把 ~2756 篇正样本压缩成 9 个 cluster center，信息严重压缩。
      FRB 因为语义高度相似，在 embedding 空间形成极紧密的点云，
      KMeans 会把这团云切出多个 center，挤占 WD/Pulsar 等稀疏话题的空间。
      k-NN 直接用原始 embedding，每篇正样本都保留完整信息，
      只要新论文与任何一篇 WD/Pulsar 正样本相似，就能被召回。
    """

    def __init__(
        self,
        encoder_name: str = "sentence-transformers/all-mpnet-base-v2",
        k: int = 10,
        neg_k: int = 5,
        neg_weight: float = 0.5,
        neg_sample_size: int = 3000,
        batch_size: int = 256,
        threshold_beta: float = 1.0,
        random_state: int = 42,
        max_len: int = 512,
        embedding_dim: int | None = None,
    ):
        if not isinstance(encoder_name, str) or not encoder_name.strip():
            raise ValueError("encoder_name must be a non-empty string")
        for name, value, minimum in (
            ("k", k, 1),
            ("neg_k", neg_k, 0),
            ("neg_sample_size", neg_sample_size, 1),
            ("batch_size", batch_size, 1),
            ("max_len", max_len, 1),
        ):
            if type(value) is not int or value < minimum:
                raise ValueError(f"{name} must be an integer >= {minimum}")
        if (
            isinstance(neg_weight, bool)
            or not isinstance(neg_weight, (int, float))
            or not math.isfinite(float(neg_weight))
            or neg_weight < 0
        ):
            raise ValueError("neg_weight must be a finite non-negative number")
        if (
            isinstance(threshold_beta, bool)
            or not isinstance(threshold_beta, (int, float))
            or not math.isfinite(float(threshold_beta))
            or threshold_beta <= 0
        ):
            raise ValueError("threshold_beta must be a finite positive number")
        self.encoder_name = encoder_name
        self.k = k
        self.neg_k = neg_k
        self.neg_weight = neg_weight
        self.neg_sample_size = neg_sample_size
        self.batch_size = batch_size
        self.threshold_beta = threshold_beta
        self.random_state = random_state
        self.max_len = max_len

        if embedding_dim is not None and (type(embedding_dim) is not int or embedding_dim <= 0):
            raise ValueError("embedding_dim must be a positive integer or None")
        self.encoder: Any | None = None
        self.embed_dim = embedding_dim
        self._fp16 = torch.cuda.is_available()

        # 普通调用保持原有的立即加载语义；训练主流程已经持有全量 embedding，
        # 只传入维数即可避免把同一个大模型无意义地载入第二遍。
        if self.embed_dim is None:
            self._load_encoder()

        # 训练后填充
        self.pos_embeddings: np.ndarray | None = None  # (n_pos, D)
        self.neg_embeddings: np.ndarray | None = None  # (n_neg_sample, D)
        self.threshold: float = 0.5
        self.columns: dict[str, str | None] = {}

    # ------------------------------------------------------------------
    # Encoding（用于在没有外部缓存时进行按需编码）
    # ------------------------------------------------------------------

    def _load_encoder(self) -> Any:
        if self.encoder is not None:
            return self.encoder

        _log(f"Loading encoder: {self.encoder_name}")
        encoder_class = _training.load_sentence_transformer_class()
        encoder = encoder_class(self.encoder_name)
        actual_dim = int(encoder.get_sentence_embedding_dimension())
        if actual_dim <= 0:
            raise ValueError("encoder returned an invalid embedding dimension")
        if self.embed_dim is not None and self.embed_dim != actual_dim:
            raise ValueError(
                f"encoder dimension changed: expected {self.embed_dim}, received {actual_dim}"
            )
        self.encoder = encoder
        self.embed_dim = actual_dim
        if self._fp16:
            _log("Enabled fp16 encoding (CUDA detected)")
        return encoder

    def _embedding_dim(self) -> int:
        if self.embed_dim is None:
            self._load_encoder()
        if self.embed_dim is None:  # 帮助静态检查器理解上面的初始化保证。
            raise RuntimeError("encoder did not expose an embedding dimension")
        return self.embed_dim

    def encode(self, texts: list[str]) -> np.ndarray:
        embed_dim = self._embedding_dim()
        if not texts:
            return np.zeros((0, embed_dim), dtype=np.float32)
        encoder = self._load_encoder()
        kw: dict[str, Any] = {
            "batch_size": self.batch_size,
            "show_progress_bar": True,
            "convert_to_numpy": True,
            "normalize_embeddings": True,
        }
        if self._fp16:
            with torch.amp.autocast("cuda"):
                emb = encoder.encode(texts, **kw)
        else:
            emb = encoder.encode(texts, **kw)
        return _training.validate_embedding_matrix(
            emb,
            expected_rows=len(texts),
            expected_dim=embed_dim,
            name="encoded embeddings",
        )

    # ------------------------------------------------------------------
    # 得分计算
    # ------------------------------------------------------------------

    def _score_from_embeddings(self, query_emb: np.ndarray) -> np.ndarray:
        """
        query_emb: (n, D) normalized → scores: (n,) float32

        score_i = mean(top_k_pos_sims_i) − neg_weight * mean(top_k_neg_sims_i)

        直觉：
          top_k_pos_sims：新论文与最相似 k 篇兴趣论文的均值 → 正向拉力
          top_k_neg_sims：新论文与最相似若干不感兴趣论文的均值 → 负向惩罚
        """
        if self.pos_embeddings is None:
            raise RuntimeError("模型未训练")
        query_emb = _training.validate_embedding_matrix(
            query_emb,
            expected_rows=len(query_emb),
            expected_dim=self._embedding_dim(),
            name="query embeddings",
        )

        # (n_query, n_pos)
        pos_sims = query_emb @ self.pos_embeddings.T
        k = min(self.k, pos_sims.shape[1])
        top_k_pos = np.sort(pos_sims, axis=1)[:, -k:].mean(axis=1)  # (n,)

        if self.neg_embeddings is not None and self.neg_k > 0 and self.neg_weight > 0:
            neg_sims = query_emb @ self.neg_embeddings.T  # (n_query, n_neg)
            nk = min(self.neg_k, neg_sims.shape[1])
            top_k_neg = np.sort(neg_sims, axis=1)[:, -nk:].mean(axis=1)
            return cast(
                np.ndarray,
                (top_k_pos - self.neg_weight * top_k_neg).astype(np.float32),
            )

        return cast(np.ndarray, top_k_pos.astype(np.float32))

    # ------------------------------------------------------------------
    # 训练
    # ------------------------------------------------------------------

    def fit(
        self,
        train_df: pd.DataFrame,
        precomputed_embeddings: np.ndarray | None = None,
    ) -> None:
        self.columns = resolve_columns(train_df)
        label_col = self.columns["label"]
        title_col = self.columns["title"]
        abstract_col = self.columns["abstract"]

        if label_col is None:
            raise ValueError("训练数据必须包含 label 列（0/1）")

        df = train_df.copy()
        labels = _training.coerce_binary_labels(df[label_col])
        df[label_col] = labels

        if set(np.unique(labels)) != {0, 1}:
            raise ValueError("训练集必须同时包含 label=0 和 label=1")
        if title_col is None or abstract_col is None:
            raise RuntimeError("列解析器未返回必需的标题或摘要列")

        # ── 编码 ────────────────────────────────────────────────────────
        if precomputed_embeddings is not None:
            _log("使用预计算 embedding")
            all_emb = _training.validate_embedding_matrix(
                precomputed_embeddings,
                expected_rows=len(df),
                expected_dim=self.embed_dim,
                name="precomputed embeddings",
            )
            if self.embed_dim is None:
                self.embed_dim = int(all_emb.shape[1])
        else:
            _log("Encoding training texts…")
            all_emb = self.encode(_training.build_title_abstract_texts(df, title_col, abstract_col))

        pos_mask = labels == 1
        neg_mask = ~pos_mask
        pos_emb = all_emb[pos_mask]
        neg_emb = all_emb[neg_mask]

        _log(f"正样本: {pos_mask.sum()}，负样本: {neg_mask.sum()}")

        # ── 存储正样本库（全量，不做任何压缩）─────────────────────────
        self.pos_embeddings = pos_emb.astype(np.float32)

        # ── 负样本下采样（全量没必要，随机取 neg_sample_size 篇即可）──
        if len(neg_emb) > self.neg_sample_size:
            rng = np.random.RandomState(self.random_state)
            idx = rng.choice(len(neg_emb), self.neg_sample_size, replace=False)
            self.neg_embeddings = neg_emb[idx].astype(np.float32)
        else:
            self.neg_embeddings = neg_emb.astype(np.float32)

        _log(
            f"兴趣库规模: {len(self.pos_embeddings)} 篇正样本，{len(self.neg_embeddings)} 篇负样本"
        )

    # ------------------------------------------------------------------
    # 阈值选择（在验证集上）
    # ------------------------------------------------------------------

    def select_threshold(
        self,
        val_df: pd.DataFrame,
        precomputed_embeddings: np.ndarray | None = None,
        min_threshold: float = 0.35,
    ) -> tuple[float, float]:
        cols = resolve_columns(val_df)
        label_col = cols["label"]
        if label_col is None:
            raise ValueError("验证集必须包含 label 列")

        y_true = _training.coerce_binary_labels(val_df[label_col])
        y_score = self.predict_proba(val_df, precomputed_embeddings=precomputed_embeddings)

        threshold, fbeta = _training.best_fbeta_threshold(y_true, y_score, self.threshold_beta)

        if threshold < min_threshold:
            _log(f"  阈值 {threshold:.4f} 低于下限 {min_threshold:.4f}，已抬升")
            threshold = min_threshold

        self.threshold = threshold
        return threshold, fbeta

    # ------------------------------------------------------------------
    # 推理
    # ------------------------------------------------------------------

    def predict_proba(
        self,
        df: pd.DataFrame,
        precomputed_embeddings: np.ndarray | None = None,
    ) -> np.ndarray:
        """返回每篇论文的得分（作为 pseudo-probability 使用）。"""
        if self.pos_embeddings is None:
            raise RuntimeError("模型未训练")
        cols = resolve_columns(df)
        title_col = cols["title"]
        abstract_col = cols["abstract"]

        if len(df) == 0:
            return cast(np.ndarray, np.array([], dtype=np.float32))

        if title_col is None or abstract_col is None:
            raise RuntimeError("列解析器未返回必需的标题或摘要列")
        if precomputed_embeddings is None:
            emb = self.encode(_training.build_title_abstract_texts(df, title_col, abstract_col))
        else:
            emb = _training.validate_embedding_matrix(
                precomputed_embeddings,
                expected_rows=len(df),
                expected_dim=self._embedding_dim(),
                name="precomputed embeddings",
            )
        return self._score_from_embeddings(emb)

    # ------------------------------------------------------------------
    # 评估
    # ------------------------------------------------------------------

    def evaluate(
        self,
        df: pd.DataFrame,
        name: str = "Eval",
        precomputed_embeddings: np.ndarray | None = None,
    ) -> dict[str, float]:
        if self.pos_embeddings is None:
            raise RuntimeError("模型未训练")
        cols = resolve_columns(df)
        label_col = cols["label"]
        if label_col is None:
            raise ValueError("评估数据必须包含 label 列")

        y_true = _training.coerce_binary_labels(df[label_col])
        y_score = self.predict_proba(df, precomputed_embeddings=precomputed_embeddings)
        y_pred = (y_score >= self.threshold).astype(int)

        has_both = len(np.unique(y_true)) >= 2
        metrics: dict[str, float] = {
            "roc_auc": float(roc_auc_score(y_true, y_score)) if has_both else float("nan"),
            "pr_auc": float(average_precision_score(y_true, y_score)) if has_both else float("nan"),
            "f1": float(f1_score(y_true, y_pred, zero_division=0)),
            "precision_at_5": _training.precision_at_k(y_true, y_score, 5),
            "precision_at_10": _training.precision_at_k(y_true, y_score, 10),
            "precision_at_20": _training.precision_at_k(y_true, y_score, 20),
            "threshold": self.threshold,
        }

        print(f"\n{'=' * 72}\n[{name}] Metrics\n{'=' * 72}")
        for key, val in metrics.items():
            v = "nan" if isinstance(val, float) and np.isnan(val) else f"{val:.4f}"
            print(f"  {key:<22}: {v}")
        print(f"\n{'=' * 72}\n[{name}] Classification Report\n{'=' * 72}")
        print(
            classification_report(
                y_true,
                y_pred,
                labels=[0, 1],
                target_names=["negative", "positive"],
                digits=4,
                zero_division=0,
            )
        )
        return metrics

    # ------------------------------------------------------------------
    # 保存
    # ------------------------------------------------------------------

    def save(self, model_dir: Path) -> None:
        if self.pos_embeddings is None:
            raise RuntimeError("模型未训练，无法保存")
        model_dir.mkdir(parents=True, exist_ok=True)

        # 兴趣库 embedding
        np.save(model_dir / "pos_embeddings.npy", self.pos_embeddings)
        if self.neg_embeddings is not None:
            np.save(model_dir / "neg_embeddings.npy", self.neg_embeddings)

        # 元数据
        meta = {
            "encoder_name": self.encoder_name,
            "embed_dim": self._embedding_dim(),
            "n_pos": int(len(self.pos_embeddings)),
            "n_neg_stored": int(len(self.neg_embeddings)) if self.neg_embeddings is not None else 0,
            "k": self.k,
            "neg_k": self.neg_k,
            "neg_weight": self.neg_weight,
            "threshold": self.threshold,
            "threshold_beta": self.threshold_beta,
            "columns": self.columns,
        }
        (model_dir / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        # training_config.json（与推理层约定的格式）
        tcfg = {
            "model_type": "knn",
            "runtime_model_path": ".",
            "model_name": self.encoder_name,
            "max_len": self.max_len,
            "optimal_threshold": self.threshold,
            "input_mode": "title_abstract",
            "k": self.k,
            "neg_k": self.neg_k,
            "neg_weight": self.neg_weight,
            "train_version": "knn_v1",
        }
        (model_dir / "training_config.json").write_text(
            json.dumps(tcfg, indent=2), encoding="utf-8"
        )

        _log(f"模型已保存到: {model_dir}")
        _log(
            f"  兴趣库大小: {len(self.pos_embeddings)} 篇  "
            f"({self.pos_embeddings.nbytes / 1024 / 1024:.1f} MB)"
        )


# =============================================================================
# 主训练流程
# =============================================================================


def main(config: KNNConfig = CONFIG) -> None:
    _training.seed_python_numpy(config.random_seed)

    # ── 读取数据 ─────────────────────────────────────────────────────────────
    _log(f"读取数据: {config.input_path}")
    # arXiv ID 含前导零，禁止 pandas 将其推断为浮点数。
    df = _training.read_training_csv(config.input_path)
    columns = resolve_columns(df)
    label_col, title_col, abstract_col = (columns["label"], columns["title"], columns["abstract"])
    if label_col is None:
        raise ValueError("训练模式需要 label 列")
    df[label_col] = _training.coerce_binary_labels(df[label_col])
    if title_col is None or abstract_col is None:
        raise RuntimeError("列解析器未返回必需的标题或摘要列")

    # ── 保留原始行索引（用于切分后对齐预计算 embedding）────────────────────
    df = df.reset_index(drop=True)
    df["_orig_idx"] = np.arange(len(df))

    # ── 切分训练/验证集 ───────────────────────────────────────────────────────
    train_df, val_df = split_dataframe(
        df,
        config.val_size,
        config.split_mode,
        seed=config.random_seed,
        date_col=columns["date"],
        label_col=label_col,
    )

    pos_train = int((train_df[label_col] == 1).sum())
    neg_train = int((train_df[label_col] == 0).sum())

    _log("═" * 72)
    _log(f"  k-NN arXiv Interest Model  ({config.encoder_name})")
    _log("═" * 72)
    _log(
        f"训练集: {len(train_df)}  (正: {pos_train}，负: {neg_train}，比例 {neg_train // max(pos_train, 1)}:1)"
    )
    _log(f"验证集: {len(val_df)}")
    _log(
        f"参数: k={config.k}  neg_k={config.neg_k}  neg_weight={config.neg_weight}"
        f"  neg_sample_size={config.neg_sample_size}  beta={config.beta}"
    )
    _log(f"输出目录: {config.output_dir}")
    _log(f"Embedding 缓存目录: {config.resolved_emb_cache_dir}")
    _log("─" * 72)

    # ── ★ 预计算全量 embedding（带缓存）────────────────────────────────────
    # 对整个 df 一次性编码，之后按 _orig_idx 拆分给训练/验证集，避免重复编码
    all_emb = load_or_encode(
        df,
        title_col=title_col,
        abstract_col=abstract_col,
        encoder_name=config.encoder_name,
        cache_dir=config.resolved_emb_cache_dir,
        batch_size=config.batch_size,
    )

    train_emb = all_emb[train_df["_orig_idx"].to_numpy()]
    val_emb = all_emb[val_df["_orig_idx"].to_numpy()]

    # 去掉辅助列，避免干扰模型
    train_df = train_df.drop(columns=["_orig_idx"]).reset_index(drop=True)
    val_df = val_df.drop(columns=["_orig_idx"]).reset_index(drop=True)

    _log("Embedding 就绪，开始训练…")

    # ── 初始化模型（不含 encoder，embedding 已外部提供）────────────────────
    # 注意：KNNInterestModel.__init__ 会再次加载 encoder 用于推理时按需编码，
    # 但训练过程中我们传入 precomputed_embeddings 跳过重复编码。
    model = KNNInterestModel(
        encoder_name=config.encoder_name,
        k=config.k,
        neg_k=config.neg_k,
        neg_weight=config.neg_weight,
        neg_sample_size=config.neg_sample_size,
        batch_size=config.batch_size,
        threshold_beta=config.beta,
        random_state=config.random_seed,
        max_len=config.max_len,
        embedding_dim=int(all_emb.shape[1]),
    )

    # ── 训练（存储兴趣库）────────────────────────────────────────────────────
    model.fit(train_df, precomputed_embeddings=train_emb)

    # ── 阈值校准 ─────────────────────────────────────────────────────────────
    threshold, fbeta = model.select_threshold(
        val_df,
        precomputed_embeddings=val_emb,
        min_threshold=config.min_threshold,
    )
    _log(f"[Validation] threshold={threshold:.4f}  F{config.beta:.1f}={fbeta:.4f}")

    # ── 整体评估 ─────────────────────────────────────────────────────────────
    model.evaluate(train_df, name="Train", precomputed_embeddings=train_emb)
    model.evaluate(val_df, name="Validation", precomputed_embeddings=val_emb)

    # ── ★ Per-topic recall 分析（核心诊断：WD/Pulsar/Gaia 通过率）──────────
    val_scores = model.predict_proba(val_df, precomputed_embeddings=val_emb)
    evaluate_per_topic(
        df=val_df,
        scores=val_scores,
        threshold=threshold,
        title_col=title_col,
        label_col=label_col,
        name="Validation",
    )

    # ── 保存模型 ─────────────────────────────────────────────────────────────
    model.save(config.output_dir)

    # ── 保存验证集打分（方便事后与多兴趣模型对比）────────────────────────────
    val_preds = (val_scores >= threshold).astype(int)
    val_out = val_df.copy()
    val_out["knn_score"] = val_scores
    val_out["pred_label"] = val_preds
    val_out = val_out.sort_values("knn_score", ascending=False).reset_index(drop=True)
    val_out.insert(0, "rank", np.arange(1, len(val_out) + 1))
    out_path = config.output_dir / "validation_scored.csv"
    val_out.to_csv(out_path, index=False)
    _log(f"验证集打分已保存: {out_path}")

    # ── 最终提示 ──────────────────────────────────────────────────────────────
    n_pred_pos = int(val_preds.sum())
    n_true_pos = int((val_df[label_col] == 1).sum())
    _log("")
    _log("─" * 72)
    _log(f"验证集概览: 真实正样本={n_true_pos}  预测正样本={n_pred_pos}  threshold={threshold:.4f}")
    _log("─" * 72)
    _log("训练完成！切换到此模型请修改 plugins/arxiv_filter/config.json：")
    _log(f'  "model": {{"path": "{config.output_dir.name}", "threshold": {threshold:.4f}}}')
    _log("─" * 72)


if __name__ == "__main__":
    main()
