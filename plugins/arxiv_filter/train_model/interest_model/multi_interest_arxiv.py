"""
arXiv 多兴趣模型训练脚本
==========================

使用 sentence-transformers embedding + 多兴趣聚类 + 逻辑回归完成兴趣预测的训练。
提供与主训练脚本一致的配置与元数据输出约定。
"""

from __future__ import annotations

import json
import math
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, cast

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
    roc_auc_score,
)
from sklearn.preprocessing import normalize

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
    from plugins.arxiv_filter.numerics import stable_softmax
    from plugins.arxiv_filter.train_model.interest_model import training_utils as _training
else:
    from ...numerics import stable_softmax
    from . import training_utils as _training

_log = _training.timestamp_log

_SCRIPT_DIR = Path(__file__).resolve().parent
_TRAIN_DIR = _SCRIPT_DIR.parent
_PLUGIN_DIR = _TRAIN_DIR.parent

# =============================================================================
# 训练配置
# =============================================================================


@dataclass(frozen=True, slots=True)
class TrainingConfig:
    input_path: Path = _TRAIN_DIR / "arxiv_papers_with_abstract.csv"
    output_dir: Path = field(default_factory=lambda: _PLUGIN_DIR / "best_model_interest")
    # 候选编码器（按推荐顺序）：
    #   "sentence-transformers/all-mpnet-base-v2"  — 通用，已本地缓存，当前默认
    #   "allenai/specter2_base"                    — 专为科学论文设计，需单独下载 (~440 MB)
    #   "sentence-transformers/all-MiniLM-L6-v2"  — 速度快但质量较低
    encoder_name: str = "sentence-transformers/all-mpnet-base-v2"
    # n_interests 选择依据（基于 2756 个正样本的实际话题分布）：
    #   n=6 的问题：FRB 占正样本 29.5%，但 KMeans 分给它 3/6 个 cluster；
    #               WD（4.1%）/ 纯 Pulsar（6.5%）/ Gaia（7.2%）无专属 cluster，
    #               导致这些话题的 max_sim 偏低，通过率极差（WD 16.7%，Gaia 7.1%）。
    #   若要降低上述偏差，可在对照实验中评估 n=9：FRB 仍占 2~3 个 cluster，但腾出空间给：
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

    def __post_init__(self) -> None:
        if not isinstance(self.encoder_name, str) or not self.encoder_name.strip():
            raise ValueError("encoder_name must be a non-empty string")
        for name in ("n_interests", "batch_size", "max_len"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if not 0 < self.val_size < 1:
            raise ValueError("val_size must be between 0 and 1")
        if not math.isfinite(self.beta) or self.beta <= 0:
            raise ValueError("beta must be a finite positive number")
        if not math.isfinite(self.min_threshold):
            raise ValueError("min_threshold must be finite")
        if self.split_mode not in {"random", "time"}:
            raise ValueError("split_mode must be 'random' or 'time'")


CONFIG = TrainingConfig()


def resolve_columns(df: pd.DataFrame) -> dict[str, str | None]:
    """自动识别列名。"""
    return _training.resolve_columns(
        df,
        {
            "id": ["arxivid", "id", "paperid", "articleid"],
            "title": ["title", "papertitle"],
            "abstract": ["abstract", "summary", "description"],
            "label": ["label", "interest", "target", "y"],
            "date": [
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
        },
        required_fields={"title", "abstract"},
        error_prefix="无法自动识别列名",
    )


# =============================================================================
# 数学/评估工具
# =============================================================================


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
            w for w, s in zip(keywords, scores[np.argsort(scores)[::-1]], strict=False) if s > 0
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
    return _training.split_dataframe(
        df,
        val_size,
        split_mode,
        seed=seed,
        date_col=date_col,
        label_col=label_col,
        log=_log,
        missing_date_error="split_mode=time 时需要可识别的日期列。",
        stratify_fallback_message="警告：分层随机切分失败，自动退回普通随机切分。",
        invalid_dates_message="警告：有 {count} 条记录日期无法解析，将被放到最前面参与时间排序。",
        empty_training_error="验证集比例过大，导致训练集为空。",
        unknown_mode_error="未知 split_mode: {mode}",
    )


# =============================================================================
# 模型 Artifacts
# =============================================================================


@dataclass(slots=True)
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
        if not isinstance(encoder_name, str) or not encoder_name.strip():
            raise ValueError("encoder_name must be a non-empty string")
        for name, value in (("n_interests", n_interests), ("batch_size", batch_size)):
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if (
            isinstance(threshold_beta, bool)
            or not isinstance(threshold_beta, (int, float))
            or not math.isfinite(float(threshold_beta))
            or threshold_beta <= 0
        ):
            raise ValueError("threshold_beta must be a finite positive number")
        self.encoder_name = encoder_name
        self.n_interests = n_interests
        self.batch_size = batch_size
        self.threshold_beta = threshold_beta
        self.random_state = random_state

        _log(f"Loading encoder: {encoder_name}")
        self.encoder = _training.load_sentence_transformer_class()(encoder_name)
        self.embedding_dim = int(self.encoder.get_sentence_embedding_dimension())
        if self.embedding_dim <= 0:
            raise ValueError("encoder returned an invalid embedding dimension")
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
        return _training.validate_embedding_matrix(
            emb,
            expected_rows=len(texts),
            expected_dim=self.embedding_dim,
            name="encoded embeddings",
        )

    # ------------------------------------------------------------------
    # 兴趣聚类
    # ------------------------------------------------------------------

    def _balance_cluster_input(
        self,
        pos_embeddings: np.ndarray,
    ) -> np.ndarray:
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
            return pos_embeddings

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
        return cast(np.ndarray, pos_embeddings[selected_arr])

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
        balanced_embeddings = self._balance_cluster_input(pos_embeddings)

        # ── 第二步：在均衡后的子集上确定 cluster centers ─────────────────────────
        if k == 1:
            centers = normalize(balanced_embeddings.mean(axis=0, keepdims=True))
        else:
            km = KMeans(n_clusters=k, random_state=self.random_state, n_init=20)
            km.fit(balanced_embeddings)
            centers = normalize(km.cluster_centers_)

        # ── 第三步：将全量正样本分配到最近的 center（统计信息反映真实分布）──────
        # centers 基于均衡子集确定（更公平），但 keywords/sizes/examples 来自全量
        sims_all = pos_embeddings @ centers.T
        all_labels = sims_all.argmax(axis=1)
        keywords: list[str] = []
        sizes: list[int] = []
        examples: list[list[str]] = []

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
        centers = np.asarray(interest_centers)
        if centers.ndim != 2 or centers.shape[0] == 0 or centers.shape[1] == 0:
            raise ValueError("interest_centers must be a non-empty two-dimensional matrix")
        embeddings = _training.validate_embedding_matrix(
            embeddings,
            expected_rows=len(embeddings),
            expected_dim=centers.shape[1],
            name="embeddings",
        )
        if np.asarray(pos_centroid).shape != (centers.shape[1],):
            raise ValueError("pos_centroid dimension does not match embeddings")
        if neg_centroid is not None and np.asarray(neg_centroid).shape != (centers.shape[1],):
            raise ValueError("neg_centroid dimension does not match embeddings")
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
            empty: np.ndarray = np.zeros((0, len(feature_names)), dtype=np.float32)
            return empty, np.zeros(0, dtype=np.int64), np.zeros(0, dtype=np.float32), feature_names

        sims = embeddings @ interest_centers.T
        max_sim = sims.max(axis=1)
        best_interest = sims.argmax(axis=1)

        sorted_sims = np.sort(sims, axis=1)
        top2 = sorted_sims[:, -2] if sims.shape[1] > 1 else sorted_sims[:, -1]

        probs = stable_softmax(sims, axis=1)
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
        labels = _training.coerce_binary_labels(train_df[label_col])
        train_df[label_col] = labels

        if set(np.unique(labels)) != {0, 1}:
            raise ValueError("训练集必须同时包含 label=0 和 label=1。")
        if title_col is None or abstract_col is None:
            raise RuntimeError("列解析器未返回必需的标题或摘要列")

        raw_texts = _training.build_paired_texts(train_df, title_col, abstract_col, "{t} {a}")
        titles = train_df[title_col].fillna("").astype(str).tolist()

        if precomputed_embeddings is not None:
            _log("Using precomputed embeddings for training (skipping encoding).")
            embeddings = _training.validate_embedding_matrix(
                precomputed_embeddings,
                expected_rows=len(train_df),
                expected_dim=self.embedding_dim,
                name="precomputed embeddings",
            )
        else:
            _log("Encoding training texts...")
            embeddings = self.encode_texts(
                _training.build_title_abstract_texts(train_df, title_col, abstract_col)
            )

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
        if self.artifacts is None:
            raise RuntimeError("模型尚未训练或加载。")
        columns = resolve_columns(val_df)
        label_col = columns["label"]
        if label_col is None:
            raise ValueError("验证集必须包含 label 列。")

        y_true = _training.coerce_binary_labels(val_df[label_col])
        y_score = self.predict_proba(
            val_df,
            sort_output=False,
            use_current_threshold=False,
            precomputed_embeddings=precomputed_embeddings,
        )["interest_prob"].to_numpy()

        threshold, best_fb = _training.best_fbeta_threshold(y_true, y_score, self.threshold_beta)
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
        if self.artifacts is None:
            raise RuntimeError("模型尚未训练或加载。")
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

        if title_col is None or abstract_col is None:
            raise RuntimeError("列解析器未返回必需的标题或摘要列")
        if precomputed_embeddings is None:
            emb = self.encode_texts(
                _training.build_title_abstract_texts(df, title_col, abstract_col)
            )
        else:
            emb = _training.validate_embedding_matrix(
                precomputed_embeddings,
                expected_rows=len(df),
                expected_dim=self.embedding_dim,
                name="precomputed embeddings",
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
        if self.artifacts is None:
            raise RuntimeError("模型尚未训练或加载。")
        columns = resolve_columns(df)
        label_col = columns["label"]
        if label_col is None:
            raise ValueError("评估数据必须包含 label 列。")

        y_true = _training.coerce_binary_labels(df[label_col])
        scored = self.predict_proba(
            df, sort_output=False, precomputed_embeddings=precomputed_embeddings
        )
        y_score, y_pred = scored["interest_prob"].to_numpy(), scored["pred_label"].to_numpy()

        has_both = len(np.unique(y_true)) >= 2
        metrics: dict[str, float] = {
            "roc_auc": float(roc_auc_score(y_true, y_score)) if has_both else float("nan"),
            "pr_auc": float(average_precision_score(y_true, y_score)) if has_both else float("nan"),
            "f1": float(f1_score(y_true, y_pred, zero_division=0)),
            "precision_at_5": _training.precision_at_k(y_true, y_score, 5),
            "precision_at_10": _training.precision_at_k(y_true, y_score, 10),
            "precision_at_20": _training.precision_at_k(y_true, y_score, 20),
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
        if self.artifacts is None:
            raise RuntimeError("模型尚未训练，无法保存。")
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
    _training.seed_python_numpy(config.random_seed)

    # arXiv ID 含前导零，禁止 pandas 将其推断为浮点数。
    df = _training.read_training_csv(config.input_path)
    columns = resolve_columns(df)
    label_col = columns["label"]
    if label_col is None:
        raise ValueError("训练模式需要 label 列。")

    title_col, abstract_col = columns["title"], columns["abstract"]
    df[label_col] = _training.coerce_binary_labels(df[label_col])
    if title_col is None or abstract_col is None:
        raise RuntimeError("列解析器未返回必需的标题或摘要列")

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
    train_emb = model.encode_texts(
        _training.build_title_abstract_texts(train_df, title_col, abstract_col)
    )
    _log("Encoding validation texts (once)...")
    val_emb = model.encode_texts(
        _training.build_title_abstract_texts(val_df, title_col, abstract_col)
    )
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
