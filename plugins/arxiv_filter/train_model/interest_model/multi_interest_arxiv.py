"""
arXiv 多兴趣模型训练脚本
==========================

使用 sentence-transformers embedding + 多兴趣聚类 + 逻辑回归完成兴趣预测的训练。
提供与主训练脚本一致的配置与元数据输出约定。
"""

from __future__ import annotations

import importlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
import torch
from sklearn.cluster import KMeans
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    average_precision_score,
    classification_report,
    f1_score,
    precision_recall_curve,
    roc_auc_score,
)
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import normalize

# =============================================================================
# 训练配置
# =============================================================================


@dataclass(frozen=True)
class TrainingConfig:
    input_path: Path = Path(__file__).resolve().parents[1] / "arxiv_papers_with_abstract.csv"
    output_dir: Path = Path(__file__).with_name("best_model_interest")
    # 候选编码器（按推荐顺序）：
    #   "sentence-transformers/all-mpnet-base-v2"  — 通用，已本地缓存，当前默认
    #   "allenai/specter2_base"                    — 专为科学论文设计，需单独下载 (~440 MB)
    #   "sentence-transformers/all-MiniLM-L6-v2"  — 速度快但质量较低
    encoder_name: str = "sentence-transformers/all-mpnet-base-v2"
    # n_interests 选择依据（基于 2756 个正样本的实际话题分布）：
    #   n=6 的问题：FRB 占正样本 29.5%，但 KMeans 分给它 3/6 个 cluster；
    #               WD（4.1%）/ 纯 Pulsar（6.5%）/ Gaia（7.2%）无专属 cluster，
    #               导致这些话题的 max_sim 偏低，通过率极差（WD 16.7%，Gaia 7.1%）。
    #   n=9 的预期：FRB 仍占 2~3 个 cluster，但腾出空间给：
    #               ① 纯 Pulsar / 磁星 / 射电暂现源
    #               ② White Dwarf / 长周期暂现源 / 致密双星
    #               ③ Gaia / 测光巡天 / 恒星参数
    #               ④ 时域天文 / 变星 / 超新星
    #               ⑤ 软件工具 / 数据方法（防止"垃圾桶"cluster）
    #   如果重新训练后 WD/Pulsar 仍有遗漏，可尝试 n=11~12。
    n_interests: int = 6
    batch_size: int = 256
    val_size: float = 0.15
    # beta 影响阈值选取方向：
    #   beta=2.0 → 严重偏向 Recall，threshold 极低 → 推的数量过多（原始问题）
    #   beta=1.0 → F1，Precision/Recall 对等（当前默认）
    #   beta=0.5 → 偏向 Precision，threshold 较高 → 推的更少但更准
    beta: float = 2.0
    # min_threshold：F-beta 优化后的阈值下限，防止极端不平衡数据集上阈值被压得过低
    min_threshold: float = 0.40
    split_mode: str = "random"
    random_seed: int = 42
    max_len: int = 512


CONFIG = TrainingConfig()


def _load_sentence_transformer_class():
    return importlib.import_module("sentence_transformers").SentenceTransformer


# =============================================================================
# 基础工具
# =============================================================================


def _log(msg: str = "") -> None:
    print(f"{datetime.now().strftime('%H:%M:%S')}  {msg}", flush=True)


def set_seed(seed: int = 42) -> None:
    import random

    random.seed(seed)
    np.random.seed(seed)


def normalize_col_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name).strip().lower())


def resolve_columns(df: pd.DataFrame) -> dict[str, str | None]:
    """自动识别列名。"""
    norm_map = {normalize_col_name(c): c for c in df.columns}

    def pick(candidates: list[str], required: bool) -> str | None:
        for c in candidates:
            if c in norm_map:
                return norm_map[c]
        if required:
            raise ValueError(f"无法自动识别列名。现有列：{list(df.columns)}，候选：{candidates}")
        return None

    return {
        "id": pick(["arxivid", "id", "paperid", "articleid"], required=False),
        "title": pick(["title", "papertitle"], required=True),
        "abstract": pick(["abstract", "summary", "description"], required=True),
        "label": pick(["label", "interest", "target", "y"], required=False),
        "date": pick(
            [
                "date",
                "created",
                "createddate",
                "submitted",
                "submissiondate",
                "published",
                "publisheddate",
                "updated",
                "updateddate",
                "timestamp",
            ],
            required=False,
        ),
    }


def clean_text(x: object) -> str:
    if pd.isna(x):
        return ""
    return re.sub(r"\s+", " ", str(x)).strip()


def _paired_texts(
    df: pd.DataFrame, title_col: str, abstract_col: str, template: str = "{t} {a}"
) -> list[str]:
    """统一构建文本对。template 中 {t} 和 {a} 分别代表标题和摘要。"""
    titles = df[title_col].fillna("").astype(str).tolist()
    abstracts = df[abstract_col].fillna("").astype(str).tolist()
    return [
        template.format(t=clean_text(t), a=clean_text(a)).strip()
        for t, a in zip(titles, abstracts, strict=True)
    ]


def build_model_texts(df: pd.DataFrame, title_col: str, abstract_col: str) -> list[str]:
    """给 embedding 模型输入的文本。"""
    return _paired_texts(df, title_col, abstract_col, "Title: {t}\nAbstract: {a}")


def build_raw_texts(df: pd.DataFrame, title_col: str, abstract_col: str) -> list[str]:
    """给关键词抽取用的原始文本。"""
    return _paired_texts(df, title_col, abstract_col, "{t} {a}")


# =============================================================================
# 数学/评估工具
# =============================================================================


def softmax_np(x: np.ndarray, axis: int = 1) -> np.ndarray:
    x = x - np.max(x, axis=axis, keepdims=True)
    e = np.exp(x)
    return e / (np.sum(e, axis=axis, keepdims=True) + 1e-12)


def precision_at_k(y_true: np.ndarray, y_score: np.ndarray, k: int) -> float:
    k = min(k, len(y_true))
    if k <= 0:
        return 0.0
    return float(np.mean(y_true[np.argsort(-y_score)[:k]]))


def best_fbeta_threshold(
    y_true: np.ndarray,
    y_score: np.ndarray,
    beta: float = 2.0,
) -> tuple[float, float]:
    """在 PR 曲线上找最优 F-beta 阈值。"""
    if len(np.unique(y_true)) < 2:
        return 0.5, 0.0

    precision, recall, thresholds = precision_recall_curve(y_true, y_score)
    if len(thresholds) == 0:
        return 0.5, 0.0

    beta_sq = beta * beta
    fbeta = ((1 + beta_sq) * precision[:-1] * recall[:-1]) / np.clip(
        (beta_sq * precision[:-1]) + recall[:-1], 1e-12, None
    )
    if np.all(np.isnan(fbeta)):
        return 0.5, 0.0
    best_idx = int(np.nanargmax(fbeta))
    return float(thresholds[best_idx]), float(fbeta[best_idx])


def top_keywords_for_texts(texts: list[str], topn: int = 6) -> str:
    texts = [t for t in texts if str(t).strip()]
    if not texts:
        return "misc"
    vec = TfidfVectorizer(
        stop_words="english",
        ngram_range=(1, 2),
        max_features=5000,
        token_pattern=r"(?u)\b[a-zA-Z][a-zA-Z\-]{1,}\b",
    )
    try:
        X = vec.fit_transform(texts)
        scores = np.asarray(X.mean(axis=0)).ravel()
        feats = np.asarray(vec.get_feature_names_out())
        keywords = feats[np.argsort(scores)[::-1]][:topn]
        keywords = [
            w
            for w, s in zip(keywords, scores[np.argsort(scores)[::-1]], strict=False)
            if s > 0
        ]
        return ", ".join(keywords) if keywords else "misc"
    except ValueError:
        return "misc"


# =============================================================================
# 数据切分
# =============================================================================


def split_dataframe(
    df: pd.DataFrame,
    val_size: float,
    split_mode: str,
    seed: int = 42,
    date_col: str | None = None,
    label_col: str | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    df = df.copy()

    if split_mode == "time":
        if not date_col or date_col not in df.columns:
            raise ValueError("split_mode=time 时需要可识别的日期列。")
        dt = pd.to_datetime(df[date_col], errors="coerce")
        n_bad = dt.isna().sum()
        if n_bad:
            _log(f"警告：有 {n_bad} 条记录日期无法解析，将被放到最前面参与时间排序。")
        df["_dt"] = dt
        df = df.sort_values("_dt").reset_index(drop=True)
        n_val = max(1, int(round(len(df) * val_size)))
        n_train = len(df) - n_val
        if n_train <= 0:
            raise ValueError("验证集比例过大，导致训练集为空。")
        train_df = df.iloc[:n_train].drop(columns=["_dt"]).reset_index(drop=True)
        val_df = df.iloc[n_train:].drop(columns=["_dt"]).reset_index(drop=True)
        return train_df, val_df

    if split_mode == "random":
        try:
            if label_col is not None:
                return tuple(  # type: ignore[return-value]
                    d.reset_index(drop=True)
                    for d in train_test_split(
                        df, test_size=val_size, stratify=df[label_col], random_state=seed
                    )
                )
        except ValueError:
            _log("警告：分层随机切分失败，自动退回普通随机切分。")
        return tuple(  # type: ignore[return-value]
            d.reset_index(drop=True)
            for d in train_test_split(df, test_size=val_size, random_state=seed)
        )

    raise ValueError(f"未知 split_mode: {split_mode}")


# =============================================================================
# 模型 Artifacts
# =============================================================================


@dataclass
class ModelArtifacts:
    encoder_name: str
    n_interests: int
    embedding_dim: int
    interest_centers: np.ndarray
    pos_centroid: np.ndarray
    neg_centroid: np.ndarray | None
    classifier: LogisticRegression
    threshold: float
    threshold_beta: float
    cluster_keywords: list[str]
    cluster_sizes: list[int]
    cluster_examples: list[list[str]]
    feature_names: list[str]
    columns: dict[str, str | None]


# =============================================================================
# 多兴趣模型
# =============================================================================


class MultiInterestArxivModel:
    def __init__(
        self,
        encoder_name: str = "sentence-transformers/all-MiniLM-L6-v2",
        n_interests: int = 6,
        batch_size: int = 256,
        threshold_beta: float = 2.0,
        random_state: int = 42,
    ):
        self.encoder_name = encoder_name
        self.n_interests = n_interests
        self.batch_size = batch_size
        self.threshold_beta = threshold_beta
        self.random_state = random_state

        _log(f"Loading encoder: {encoder_name}")
        self.encoder = _load_sentence_transformer_class()(encoder_name)
        self.embedding_dim = int(self.encoder.get_sentence_embedding_dimension())
        self.artifacts: ModelArtifacts | None = None
        self._use_fp16 = torch.cuda.is_available()
        if self._use_fp16:
            _log("Enabled fp16 encoding (CUDA detected)")

    def encode_texts(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.embedding_dim), dtype=np.float32)
        kw: dict[str, Any] = {
            "batch_size": self.batch_size,
            "show_progress_bar": True,
            "convert_to_numpy": True,
            "normalize_embeddings": True,
        }
        if self._use_fp16:
            with torch.amp.autocast("cuda"):
                emb = self.encoder.encode(texts, **kw)
        else:
            emb = self.encoder.encode(texts, **kw)
        return emb.astype(np.float32)

    # ------------------------------------------------------------------
    # 兴趣聚类
    # ------------------------------------------------------------------

    def _balance_cluster_input(
        self,
        pos_embeddings: np.ndarray,
        pos_raw_texts: list[str],
        pos_titles: list[str],
    ) -> tuple[np.ndarray, list[str], list[str]]:
        """均衡 KMeans 的输入样本，防止高频紧凑话题（如 FRB）占据过多 cluster center。

        根本问题：FRB 论文在 embedding 空间里天然聚集（词汇高度相似），KMeans 会把
        这团密集点云切出多个 center，挤占 WD / Pulsar / Gaia 等低频话题的空间，导致
        这些话题的论文与任何 center 的相似度都偏低，无法越过阈值被推送。

        方法：
          1. 用粗粒度 KMeans (k ≈ n_interests × 4) 把正样本分成细小的话题簇；
          2. 对超过配额 max_per_coarse 的簇进行随机下采样（FRB 被有效压制）；
          3. 返回均衡后的子集用于后续最终聚类，确定更均衡的 cluster centers。

        注意：keywords / sizes / examples 依旧在全量正样本上统计，不受此影响。
        """
        n = len(pos_embeddings)
        if n < 200:
            # 样本太少时不做均衡，避免过度下采样
            return pos_embeddings, pos_raw_texts, pos_titles

        # 粗粒度聚类数：至少比最终 cluster 数多 1，至多 40，且不超过样本数的 1/10
        n_coarse = max(self.n_interests + 1, min(self.n_interests * 4, 40, n // 10))

        # 目标均衡后总量 ≈ n_interests × 80（每个最终 cluster 约 80 篇训练样本足矣）
        target_total = self.n_interests * 80
        max_per_coarse = max(15, target_total // n_coarse)

        _log(f"  均衡聚类输入 (粗粒度 k={n_coarse}，每簇上限={max_per_coarse})...")
        km_coarse = KMeans(n_clusters=n_coarse, random_state=self.random_state, n_init=5)
        coarse_labels = km_coarse.fit_predict(pos_embeddings)

        rng = np.random.RandomState(self.random_state)
        selected: list[int] = []
        for ci in range(n_coarse):
            idxs = np.where(coarse_labels == ci)[0]
            if len(idxs) <= max_per_coarse:
                selected.extend(idxs.tolist())
            else:
                sampled = rng.choice(idxs, size=max_per_coarse, replace=False)
                selected.extend(sampled.tolist())

        selected_arr = np.array(sorted(selected))
        _log(
            f"  均衡完成: {n} → {len(selected_arr)} 篇（FRB 等密集话题已被压制，稀疏话题得以保留）"
        )
        return (
            pos_embeddings[selected_arr],
            [pos_raw_texts[i] for i in selected_arr],
            [pos_titles[i] for i in selected_arr],
        )

    def _fit_interest_centers(
        self,
        pos_embeddings: np.ndarray,
        pos_raw_texts: list[str],
        pos_titles: list[str],
    ) -> tuple[np.ndarray, list[str], list[int], list[list[str]]]:
        if len(pos_embeddings) == 0:
            raise ValueError("训练集中没有 label=1 的正样本，无法构建多兴趣中心。")

        k = max(1, min(self.n_interests, len(pos_embeddings)))

        # ── 第一步：均衡化，防止 FRB 等高频话题垄断 cluster center ──────────────
        bal_emb, bal_texts, bal_titles = self._balance_cluster_input(
            pos_embeddings, pos_raw_texts, pos_titles
        )

        # ── 第二步：在均衡后的子集上确定 cluster centers ─────────────────────────
        if k == 1:
            centers = normalize(bal_emb.mean(axis=0, keepdims=True))
        else:
            km = KMeans(n_clusters=k, random_state=self.random_state, n_init=20)
            km.fit(bal_emb)
            centers = normalize(km.cluster_centers_)

        # ── 第三步：将全量正样本分配到最近的 center（统计信息反映真实分布）──────
        # centers 基于均衡子集确定（更公平），但 keywords/sizes/examples 来自全量
        sims_all = pos_embeddings @ centers.T
        all_labels = sims_all.argmax(axis=1)
        keywords, sizes, examples = [], [], []

        for ci in range(k):
            idxs = np.where(all_labels == ci)[0]
            sizes.append(len(idxs))
            keywords.append(top_keywords_for_texts([pos_raw_texts[i] for i in idxs]))
            if len(idxs) == 0:
                examples.append([])
            else:
                top5 = idxs[np.argsort(-sims_all[idxs, ci])[:5]]
                examples.append([pos_titles[j] for j in top5])

        return centers.astype(np.float32), keywords, sizes, examples

    # ------------------------------------------------------------------
    # 特征构建
    # ------------------------------------------------------------------

    @staticmethod
    def _build_features(
        embeddings: np.ndarray,
        interest_centers: np.ndarray,
        pos_centroid: np.ndarray,
        neg_centroid: np.ndarray | None,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, list[str]]:
        """返回 (feature_matrix, best_interest, max_sim, feature_names)"""
        n_k = interest_centers.shape[0]
        feature_names = [f"sim_interest_{i}" for i in range(n_k)] + [
            "max_sim",
            "mean_sim",
            "std_sim",
            "min_sim",
            "top1_top2_margin",
            "entropy",
            "concentration",
            "sim_to_pos_centroid",
            "sim_to_neg_centroid",
            "contrast_pos_neg",  # sim_pos − sim_neg：离正空间比负空间近多少
        ]

        if len(embeddings) == 0:
            empty = np.zeros((0, len(feature_names)), dtype=np.float32)
            return empty, np.zeros(0, dtype=np.int64), np.zeros(0, dtype=np.float32), feature_names

        sims = embeddings @ interest_centers.T
        max_sim = sims.max(axis=1)
        best_interest = sims.argmax(axis=1)

        sorted_sims = np.sort(sims, axis=1)
        top2 = sorted_sims[:, -2] if sims.shape[1] > 1 else sorted_sims[:, -1]

        probs = softmax_np(sims, axis=1)
        entropy = -np.sum(probs * np.log(probs + 1e-12), axis=1)

        sim_pos = (embeddings @ pos_centroid.reshape(-1, 1)).ravel()
        sim_neg = (
            (embeddings @ neg_centroid.reshape(-1, 1)).ravel()
            if neg_centroid is not None
            else np.zeros(len(embeddings), dtype=np.float32)
        )

        cols = [
            sims,
            max_sim[:, None],
            sims.mean(axis=1, keepdims=True),
            sims.std(axis=1, keepdims=True),
            sims.min(axis=1, keepdims=True),
            (sorted_sims[:, -1] - top2)[:, None],
            entropy[:, None],
            probs.max(axis=1, keepdims=True),
            sim_pos[:, None],
            sim_neg[:, None],
            (sim_pos - sim_neg)[:, None],  # contrast: 离正样本重心比负样本重心近多少
        ]
        X = np.concatenate(cols, axis=1).astype(np.float32)
        return X, best_interest, max_sim, feature_names

    # ------------------------------------------------------------------
    # 训练
    # ------------------------------------------------------------------

    def fit(
        self,
        train_df: pd.DataFrame,
        precomputed_embeddings: np.ndarray | None = None,
    ) -> None:
        columns = resolve_columns(train_df)
        label_col = columns["label"]
        if label_col is None:
            raise ValueError("训练数据必须包含 label 列，且取值为 0/1。")

        title_col, abstract_col = columns["title"], columns["abstract"]
        train_df = train_df.copy()
        train_df[label_col] = train_df[label_col].astype(int)
        labels = train_df[label_col].to_numpy()

        if len(np.unique(labels)) < 2:
            raise ValueError("训练集必须同时包含 label=0 和 label=1。")

        raw_texts = build_raw_texts(train_df, title_col, abstract_col)
        titles = train_df[title_col].fillna("").astype(str).tolist()

        if precomputed_embeddings is not None:
            _log("Using precomputed embeddings for training (skipping encoding).")
            embeddings = precomputed_embeddings
        else:
            _log("Encoding training texts...")
            embeddings = self.encode_texts(build_model_texts(train_df, title_col, abstract_col))

        pos_mask = labels == 1
        pos_emb, neg_emb = embeddings[pos_mask], embeddings[~pos_mask]

        _log(f"Fitting interest centers on {pos_mask.sum()} positive samples...")
        centers, kw, sizes, examples = self._fit_interest_centers(
            pos_emb,
            [t for t, m in zip(raw_texts, pos_mask, strict=True) if m],
            [t for t, m in zip(titles, pos_mask, strict=True) if m],
        )

        # 用各 cluster center 的均值代替全量正样本均值，避免 FRB 等高频话题
        # 因为论文数量多而主导 pos_centroid，导致 WD/Pulsar/Gaia 论文的
        # sim_to_pos_centroid 偏低。cluster-mean 给每个话题相同权重。
        pos_centroid = normalize(centers.mean(axis=0, keepdims=True)).ravel().astype(np.float32)
        neg_centroid = (
            normalize(neg_emb.mean(axis=0, keepdims=True)).ravel().astype(np.float32)
            if len(neg_emb)
            else None
        )

        _log("Building features & fitting LogisticRegression...")
        X, _, _, feature_names = self._build_features(
            embeddings, centers, pos_centroid, neg_centroid
        )

        clf = LogisticRegression(
            max_iter=3000,
            class_weight="balanced",
            C=1.0,
            solver="lbfgs",
            random_state=self.random_state,
        )
        clf.fit(X, labels)

        self.artifacts = ModelArtifacts(
            encoder_name=self.encoder_name,
            n_interests=centers.shape[0],
            embedding_dim=self.embedding_dim,
            interest_centers=centers,
            pos_centroid=pos_centroid,
            neg_centroid=neg_centroid,
            classifier=clf,
            threshold=0.5,
            threshold_beta=self.threshold_beta,
            cluster_keywords=kw,
            cluster_sizes=sizes,
            cluster_examples=examples,
            feature_names=feature_names,
            columns=columns,
        )

    # ------------------------------------------------------------------
    # 阈值选择
    # ------------------------------------------------------------------

    def select_threshold_on_validation(
        self,
        val_df: pd.DataFrame,
        precomputed_embeddings: np.ndarray | None = None,
    ) -> tuple[float, float]:
        assert self.artifacts is not None, "模型尚未训练或加载。"
        columns = resolve_columns(val_df)
        label_col = columns["label"]
        if label_col is None:
            raise ValueError("验证集必须包含 label 列。")

        y_true = val_df[label_col].astype(int).to_numpy()
        y_score = self.predict_proba(
            val_df,
            sort_output=False,
            use_current_threshold=False,
            precomputed_embeddings=precomputed_embeddings,
        )["interest_prob"].to_numpy()

        threshold, best_fb = best_fbeta_threshold(y_true, y_score, self.threshold_beta)
        self.artifacts.threshold = threshold
        return threshold, best_fb

    # ------------------------------------------------------------------
    # 预测
    # ------------------------------------------------------------------

    def predict_proba(
        self,
        df: pd.DataFrame,
        sort_output: bool = True,
        use_current_threshold: bool = True,
        precomputed_embeddings: np.ndarray | None = None,
    ) -> pd.DataFrame:
        assert self.artifacts is not None, "模型尚未训练或加载。"
        a = self.artifacts
        columns = resolve_columns(df)
        title_col, abstract_col, id_col = columns["title"], columns["abstract"], columns["id"]

        out = df.copy()
        if len(out) == 0:
            for c in [
                "interest_prob",
                "pred_label",
                "best_interest_id",
                "best_interest_keywords",
                "best_interest_sim",
            ]:
                out[c] = []
            return out

        emb = (
            precomputed_embeddings
            if precomputed_embeddings is not None
            else self.encode_texts(build_model_texts(df, title_col, abstract_col))
        )

        X, best_interest, max_sim, _ = self._build_features(
            emb, a.interest_centers, a.pos_centroid, a.neg_centroid
        )
        proba = a.classifier.predict_proba(X)[:, 1]
        threshold = a.threshold if use_current_threshold else 0.5

        if id_col is None:
            out.insert(0, "paper_id", np.arange(len(out)))

        out["interest_prob"] = proba
        out["pred_label"] = (proba >= threshold).astype(int)
        out["best_interest_id"] = best_interest
        out["best_interest_keywords"] = [a.cluster_keywords[i] for i in best_interest]
        out["best_interest_sim"] = max_sim

        if sort_output:
            out = out.sort_values("interest_prob", ascending=False).reset_index(drop=True)
            out.insert(0, "rank", np.arange(1, len(out) + 1))
        return out

    # ------------------------------------------------------------------
    # 评估
    # ------------------------------------------------------------------

    def evaluate(
        self,
        df: pd.DataFrame,
        name: str = "Eval",
        precomputed_embeddings: np.ndarray | None = None,
    ) -> dict[str, float]:
        assert self.artifacts is not None, "模型尚未训练或加载。"
        columns = resolve_columns(df)
        label_col = columns["label"]
        if label_col is None:
            raise ValueError("评估数据必须包含 label 列。")

        y_true = df[label_col].astype(int).to_numpy()
        scored = self.predict_proba(
            df, sort_output=False, precomputed_embeddings=precomputed_embeddings
        )
        y_score, y_pred = scored["interest_prob"].to_numpy(), scored["pred_label"].to_numpy()

        has_both = len(np.unique(y_true)) >= 2
        metrics: dict[str, float] = {
            "roc_auc": float(roc_auc_score(y_true, y_score)) if has_both else float("nan"),
            "pr_auc": float(average_precision_score(y_true, y_score)) if has_both else float("nan"),
            "f1": float(f1_score(y_true, y_pred, zero_division=0)),
            "precision_at_5": precision_at_k(y_true, y_score, 5),
            "precision_at_10": precision_at_k(y_true, y_score, 10),
            "precision_at_20": precision_at_k(y_true, y_score, 20),
            "threshold": float(self.artifacts.threshold),
        }

        print(f"\n{'=' * 72}\n[{name}] Metrics\n{'=' * 72}")
        for k, v in metrics.items():
            print(f"{k:20s}: {'nan' if isinstance(v, float) and np.isnan(v) else f'{v:.4f}'}")
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
        assert self.artifacts is not None, "模型尚未训练，无法保存。"
        a = self.artifacts
        model_dir.mkdir(parents=True, exist_ok=True)

        joblib.dump(asdict(a), model_dir / "artifacts.joblib")

        meta = {
            k: getattr(a, k)
            for k in [
                "encoder_name",
                "n_interests",
                "embedding_dim",
                "threshold",
                "threshold_beta",
                "feature_names",
            ]
        }
        (model_dir / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        clusters = [
            {
                "cluster_id": i,
                "size": a.cluster_sizes[i],
                "keywords": a.cluster_keywords[i],
                "example_titles": a.cluster_examples[i],
            }
            for i in range(a.n_interests)
        ]
        (model_dir / "interest_clusters.json").write_text(
            json.dumps(clusters, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        _log(f"模型已保存到: {model_dir}")


# =============================================================================
# 训练配置保存
# =============================================================================


def save_training_config(
    output_dir: Path,
    config: TrainingConfig,
    optimal_threshold: float,
    best_fbeta: float,
) -> None:
    payload = {
        "model_type": "multi_interest",
        "runtime_model_path": ".",
        "model_name": config.encoder_name,
        "max_len": config.max_len,
        "optimal_threshold": float(optimal_threshold),
        "best_validation_fbeta": float(best_fbeta),
        "threshold_beta": float(config.beta),
        "input_mode": "title_abstract",
        "train_version": "multi_interest_v1",
    }
    (output_dir / "training_config.json").write_text(
        json.dumps(payload, indent=2), encoding="utf-8"
    )


# =============================================================================
# CLI
# =============================================================================


def main(config: TrainingConfig = CONFIG) -> None:
    set_seed(config.random_seed)

    df = pd.read_csv(config.input_path)
    columns = resolve_columns(df)
    label_col = columns["label"]
    if label_col is None:
        raise ValueError("训练模式需要 label 列。")

    title_col, abstract_col = columns["title"], columns["abstract"]
    df[label_col] = df[label_col].astype(int)

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
    _log(f"  Multi-Interest arXiv Classifier ({config.encoder_name})")
    _log("═" * 72)
    _log(f"训练集: {len(train_df)}    验证集: {len(val_df)}")
    _log(
        f"正样本(训): {pos_train}    负样本(训): {neg_train}    Neg:Pos = {neg_train / max(pos_train, 1):.2f}:1"
    )
    _log(
        f"n_interests={config.n_interests}    beta={config.beta}    split_mode={config.split_mode}"
    )
    _log(f"output_dir={config.output_dir}")
    _log("─" * 72)

    model = MultiInterestArxivModel(
        encoder_name=config.encoder_name,
        n_interests=config.n_interests,
        batch_size=config.batch_size,
        threshold_beta=config.beta,
        random_state=config.random_seed,
    )

    # 预计算 embeddings（训练集和验证集各编码一次，后续全部复用）
    _log("Encoding training texts (once)...")
    train_emb = model.encode_texts(build_model_texts(train_df, title_col, abstract_col))
    _log("Encoding validation texts (once)...")
    val_emb = model.encode_texts(build_model_texts(val_df, title_col, abstract_col))
    _log("Embedding encoding complete. Reusing cached embeddings for all subsequent steps.")

    model.fit(train_df, precomputed_embeddings=train_emb)

    threshold, best_fbeta = model.select_threshold_on_validation(
        val_df, precomputed_embeddings=val_emb
    )
    # 应用阈值下限，防止 F-beta 优化出过低的阈值（导致推荐数量过多）
    if threshold < config.min_threshold:
        _log(
            f"[Validation] 阈值 {threshold:.4f} 低于 min_threshold={config.min_threshold:.4f}，已强制抬升。"
        )
        threshold = config.min_threshold
        assert model.artifacts is not None
        model.artifacts.threshold = threshold
    _log(
        f"[Validation] selected threshold = {threshold:.4f}, best F{config.beta:.1f} = {best_fbeta:.4f}"
    )

    _log(f"\n{'=' * 72}\nTrain Set Evaluation\n{'=' * 72}")
    model.evaluate(train_df, name="Train", precomputed_embeddings=train_emb)

    _log(f"\n{'=' * 72}\nValidation Set Evaluation\n{'=' * 72}")
    model.evaluate(val_df, name="Validation", precomputed_embeddings=val_emb)

    model.save(config.output_dir)
    save_training_config(config.output_dir, config, threshold, best_fbeta)

    val_output = config.output_dir / "validation_scored.csv"
    model.predict_proba(val_df, precomputed_embeddings=val_emb).to_csv(val_output, index=False)
    _log(f"验证集打分结果已保存到: {val_output}")

    if model.artifacts is not None:
        _log(f"\n{'=' * 72}\nInterest Clusters\n{'=' * 72}")
        for i, kw in enumerate(model.artifacts.cluster_keywords):
            _log(f"Cluster {i} | size={model.artifacts.cluster_sizes[i]} | keywords={kw}")
            for ex in model.artifacts.cluster_examples[i][:3]:
                _log(f"    - {ex}")


if __name__ == "__main__":
    main()
