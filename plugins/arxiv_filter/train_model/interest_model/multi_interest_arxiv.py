"""
arXiv 多兴趣模型训练脚本
==========================

使用 sentence-transformers embedding + 多兴趣聚类 + 逻辑回归完成兴趣预测的训练。

产物写入 best_model_interest/：artifacts.joblib、meta.json、interest_clusters.json、
validation_scored.csv，以及推理层读取的 training_config.json（model_type、
runtime_model_path、optimal_threshold、input_mode 等）。meta.json 和
interest_clusters.json 只供人查看。
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import cast

import joblib
import numpy as np
import pandas as pd
import torch
from sklearn.cluster import KMeans
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import normalize

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
    from plugins.arxiv_filter.inference.multi_interest_backend import ModelArtifacts
    from plugins.arxiv_filter.numerics import build_interest_features, interest_feature_names
    from plugins.arxiv_filter.train_model.interest_model import training_utils as _training
else:
    from ...inference.multi_interest_backend import ModelArtifacts
    from ...numerics import build_interest_features, interest_feature_names
    from . import training_utils as _training

_log = _training.timestamp_log

_SCRIPT_DIR = Path(__file__).resolve().parent
_TRAIN_DIR  = _SCRIPT_DIR.parent
_PLUGIN_DIR = _TRAIN_DIR.parent

# =============================================================================
# 训练配置
# =============================================================================


def _validate_hyperparameters(
    *, encoder_name: object, n_interests: object, batch_size: object, beta: object
) -> None:
    """配置对象和模型构造函数共用的超参数校验。"""

    _training.require_encoder_name(encoder_name)
    _training.require_int("n_interests", n_interests)
    _training.require_int("batch_size", batch_size)
    _training.require_finite("beta", beta, positive=True)


@dataclass(frozen=True, slots=True)
class TrainingConfig:
    input_path: Path = _TRAIN_DIR / "arxiv_papers_with_abstract.csv"
    output_dir: Path = field(default_factory=lambda: _PLUGIN_DIR / "best_model_interest")
    # 候选编码器（按推荐顺序）：
    #   "sentence-transformers/all-mpnet-base-v2"  — 通用，已本地缓存，当前默认
    #   "allenai/specter2_base"                    — 专为科学论文设计，需单独下载 (~440 MB)
    #   "sentence-transformers/all-MiniLM-L6-v2"  — 速度快但质量较低
    encoder_name: str = "sentence-transformers/all-mpnet-base-v2"
    # n_interests：兴趣中心个数。正样本里 FRB 占比很高，中心太少时低频话题（WD、纯 Pulsar、
    # Gaia）分不到专属中心，max_sim 偏低、通过率差；_balance_cluster_input 先压制 FRB 这类
    # 密集话题再聚类。重新训练后低频话题仍有遗漏时，可增大 n_interests 对照评估。
    n_interests: int   = 6
    batch_size : int   = 256
    val_size   : float = 0.15
    # beta 影响阈值选取方向：
    #   beta=2.0 → 偏向 Recall，最优阈值很低，推荐数量偏多，因此由 min_threshold 给阈值设下限
    #   beta=1.0 → F1，Precision/Recall 对等（可用于更克制的推荐实验）
    #   beta=0.5 → 偏向 Precision，threshold 较高 → 推的更少但更准
    beta: float = 2.0
    # min_threshold：F-beta 优化后的阈值下限，防止极端不平衡数据集上阈值被压得过低
    min_threshold: float = 0.40
    split_mode   : str   = "random"
    random_seed  : int   = 42
    max_len      : int   = 512

    def __post_init__(self) -> None:
        _validate_hyperparameters(
            encoder_name = self.encoder_name,
            n_interests  = self.n_interests,
            batch_size   = self.batch_size,
            beta         = self.beta,
        )
        _training.require_int("max_len", self.max_len)
        if not 0 < self.val_size < 1:
            raise ValueError("val_size must be between 0 and 1")
        _training.require_finite("min_threshold", self.min_threshold)
        if self.split_mode not in {"random", "time"}:
            raise ValueError("split_mode must be 'random' or 'time'")


CONFIG = TrainingConfig()


def resolve_columns(df: pd.DataFrame) -> dict[str, str | None]:
    """自动识别列名。"""
    return _training.resolve_columns(
        df,
        {
            "id"      : ["arxivid", "id", "paperid", "articleid"],
            "title"   : ["title", "papertitle"],
            "abstract": ["abstract", "summary", "description"],
            "label"   : ["label", "interest", "target", "y"],
            "date"    : [
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
        required_fields = {"title", "abstract"},
        error_prefix    = "无法自动识别列名",
    )


# =============================================================================
# 数学/评估工具
# =============================================================================


def top_keywords_for_texts(texts: list[str], topn: int = 6) -> str:
    texts = [t for t in texts if str(t).strip()]
    if not texts:
        return "misc"
    vec = TfidfVectorizer(
        stop_words    = "english",
        ngram_range   = (1, 2),
        max_features  = 5000,
        token_pattern = r"(?u)\b[a-zA-Z][a-zA-Z\-]{1,}\b",
    )
    try:
        X = vec.fit_transform(texts)
        scores = np.asarray(X.mean(axis=0)).ravel()
        feats    = np.asarray(vec.get_feature_names_out())
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
    seed: int             = 42,
    date_col: str | None  = None,
    label_col: str | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    return _training.split_dataframe(
        df,
        val_size,
        split_mode,
        seed                      = seed,
        date_col                  = date_col,
        label_col                 = label_col,
        log                       = _log,
        missing_date_error        = "split_mode=time 时需要可识别的日期列。",
        stratify_fallback_message = "警告：分层随机切分失败，自动退回普通随机切分。",
        invalid_dates_message     = "警告：有 {count} 条记录日期无法解析，将排在最前面进入训练集。",
        empty_training_error      = "验证集比例过大，导致训练集为空。",
        unknown_mode_error        = "未知 split_mode: {mode}",
    )


# =============================================================================
# 多兴趣模型
# =============================================================================


class MultiInterestArxivModel:
    def __init__(
        self,
        encoder_name: str     = "sentence-transformers/all-MiniLM-L6-v2",
        n_interests: int      = 6,
        batch_size: int       = 256,
        threshold_beta: float = 2.0,
        random_state: int     = 42,
        max_len: int | None   = None,
    ):
        _validate_hyperparameters(
            encoder_name = encoder_name,
            n_interests  = n_interests,
            batch_size   = batch_size,
            beta         = threshold_beta,
        )
        self.encoder_name   = encoder_name
        self.n_interests    = n_interests
        self.batch_size     = batch_size
        self.threshold_beta = threshold_beta
        self.random_state   = random_state

        _log(f"Loading encoder: {encoder_name}")
        self.encoder       = _training.load_sentence_encoder(encoder_name, max_len)
        self.embedding_dim = int(self.encoder.get_sentence_embedding_dimension())
        if self.embedding_dim <= 0:
            raise ValueError("encoder returned an invalid embedding dimension")
        self.artifacts: ModelArtifacts | None = None
        if torch.cuda.is_available():
            _log("Enabled fp16 encoding (CUDA detected)")

    def encode_texts(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.embedding_dim), dtype=np.float32)
        emb = _training.encode_normalized(self.encoder, texts, self.batch_size, show_progress=True)
        return _training.validate_embedding_matrix(
            emb,
            expected_rows = len(texts),
            expected_dim  = self.embedding_dim,
            name          = "encoded embeddings",
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
        target_total   = self.n_interests * 80
        max_per_coarse = max(15, target_total // n_coarse)

        _log(f"  均衡聚类输入 (粗粒度 k={n_coarse}，每簇上限={max_per_coarse})...")
        km_coarse = KMeans(n_clusters=n_coarse, random_state=self.random_state, n_init=5)
        coarse_labels = km_coarse.fit_predict(pos_embeddings)

        rng                 = np.random.RandomState(self.random_state)
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
        keywords: list[str]       = []
        sizes   : list[int]       = []
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
    # 训练
    # ------------------------------------------------------------------

    def fit(
        self,
        train_df: pd.DataFrame,
        precomputed_embeddings: np.ndarray | None = None,
    ) -> None:
        columns   = resolve_columns(train_df)
        label_col = columns["label"]
        if label_col is None:
            raise ValueError("训练数据必须包含 label 列，且取值为 0/1。")

        title_col, abstract_col = columns["title"], columns["abstract"]
        train_df            = train_df.copy()
        labels              = _training.coerce_binary_labels(train_df[label_col])
        train_df[label_col] = labels

        if set(np.unique(labels)) != {0, 1}:
            raise ValueError("训练集必须同时包含 label=0 和 label=1。")
        if title_col is None or abstract_col is None:
            raise RuntimeError("列解析器未返回必需的标题或摘要列")

        raw_texts = _training.build_paired_texts(train_df, title_col, abstract_col, "{t} {a}")
        titles    = train_df[title_col].fillna("").astype(str).tolist()

        if precomputed_embeddings is not None:
            _log("Using precomputed embeddings for training (skipping encoding).")
            embeddings = _training.validate_embedding_matrix(
                precomputed_embeddings,
                expected_rows = len(train_df),
                expected_dim  = self.embedding_dim,
                name          = "precomputed embeddings",
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
        X, _, _ = build_interest_features(embeddings, centers, pos_centroid, neg_centroid)

        clf = LogisticRegression(
            max_iter     = 3000,
            class_weight = "balanced",
            C            = 1.0,
            solver       = "lbfgs",
            random_state = self.random_state,
        )
        clf.fit(X, labels)

        self.artifacts = ModelArtifacts(
            encoder_name     = self.encoder_name,
            n_interests      = centers.shape[0],
            embedding_dim    = self.embedding_dim,
            interest_centers = centers,
            pos_centroid     = pos_centroid,
            neg_centroid     = neg_centroid,
            classifier       = clf,
            threshold        = 0.5,
            threshold_beta   = self.threshold_beta,
            cluster_keywords = kw,
            cluster_sizes    = sizes,
            cluster_examples = examples,
            feature_names    = interest_feature_names(centers.shape[0]),
            columns          = columns,
        )

    # ------------------------------------------------------------------
    # 阈值选择
    # ------------------------------------------------------------------

    def select_threshold_on_validation(
        self,
        val_df: pd.DataFrame,
        precomputed_embeddings: np.ndarray | None = None,
        min_threshold: float | None               = None,
    ) -> tuple[float, float]:
        if self.artifacts is None:
            raise RuntimeError("模型尚未训练或加载。")
        columns   = resolve_columns(val_df)
        label_col = columns["label"]
        if label_col is None:
            raise ValueError("验证集必须包含 label 列。")

        y_true  = _training.coerce_binary_labels(val_df[label_col])
        y_score = self.predict_proba(
            val_df,
            sort_output            = False,
            use_current_threshold  = False,
            precomputed_embeddings = precomputed_embeddings,
        )["interest_prob"].to_numpy()

        threshold, best_fb = _training.best_fbeta_threshold(y_true, y_score, self.threshold_beta)
        if min_threshold is not None and threshold < min_threshold:
            _log(
                f"[Validation] 阈值 {threshold:.4f} 低于 min_threshold={min_threshold:.4f}，"
                f"已强制抬升；最优候选 F{self.threshold_beta:.1f}={best_fb:.4f}"
            )
            threshold = min_threshold
            # 写入训练配置的分数必须对应最终部署阈值。
            best_fb = _training.fbeta_at_threshold(y_true, y_score, threshold, self.threshold_beta)
        self.artifacts = replace(self.artifacts, threshold=threshold)
        return threshold, best_fb

    # ------------------------------------------------------------------
    # 预测
    # ------------------------------------------------------------------

    def predict_proba(
        self,
        df: pd.DataFrame,
        sort_output: bool                         = True,
        use_current_threshold: bool               = True,
        precomputed_embeddings: np.ndarray | None = None,
    ) -> pd.DataFrame:
        if self.artifacts is None:
            raise RuntimeError("模型尚未训练或加载。")
        a       = self.artifacts
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
                expected_rows = len(df),
                expected_dim  = self.embedding_dim,
                name          = "precomputed embeddings",
            )

        X, best_interest, max_sim = build_interest_features(
            emb, a.interest_centers, a.pos_centroid, a.neg_centroid
        )
        proba     = a.classifier.predict_proba(X)[:, 1]
        threshold = a.threshold if use_current_threshold else 0.5

        if id_col is None:
            out.insert(0, "paper_id", np.arange(len(out)))

        out["interest_prob"]          = proba
        out["pred_label"]             = (proba >= threshold).astype(int)
        out["best_interest_id"]       = best_interest
        out["best_interest_keywords"] = [a.cluster_keywords[i] for i in best_interest]
        out["best_interest_sim"]      = max_sim

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
        name: str                                 = "Eval",
        precomputed_embeddings: np.ndarray | None = None,
    ) -> dict[str, float]:
        if self.artifacts is None:
            raise RuntimeError("模型尚未训练或加载。")
        columns   = resolve_columns(df)
        label_col = columns["label"]
        if label_col is None:
            raise ValueError("评估数据必须包含 label 列。")

        y_true = _training.coerce_binary_labels(df[label_col])
        scored = self.predict_proba(
            df, sort_output=False, precomputed_embeddings=precomputed_embeddings
        )
        y_score, y_pred = scored["interest_prob"].to_numpy(), scored["pred_label"].to_numpy()

        return _training.report_interest_metrics(
            y_true, y_score, y_pred, threshold=self.artifacts.threshold, name=name
        )

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
                "cluster_id"    : i,
                "size"          : a.cluster_sizes[i],
                "keywords"      : a.cluster_keywords[i],
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
        "model_type"           : "multi_interest",
        "runtime_model_path"   : ".",
        "model_name"           : config.encoder_name,
        "max_len"              : config.max_len,
        "optimal_threshold"    : float(optimal_threshold),
        "best_validation_fbeta": float(best_fbeta),
        "threshold_beta"       : float(config.beta),
        "input_mode"           : "title_abstract",
        "train_version"        : "multi_interest_v1",
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
    df        = _training.read_training_csv(config.input_path)
    columns   = resolve_columns(df)
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
        seed      = config.random_seed,
        date_col  = columns["date"],
        label_col = label_col,
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
        encoder_name   = config.encoder_name,
        n_interests    = config.n_interests,
        batch_size     = config.batch_size,
        threshold_beta = config.beta,
        random_state   = config.random_seed,
        max_len        = config.max_len,
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

    # 阈值下限防止 F-beta 优化出过低的阈值（导致推荐数量过多）；分数按最终阈值重算。
    threshold, best_fbeta = model.select_threshold_on_validation(
        val_df, precomputed_embeddings=val_emb, min_threshold=config.min_threshold
    )
    _log(
        f"[Validation] deployed threshold = {threshold:.4f}, F{config.beta:.1f} at threshold = {best_fbeta:.4f}"
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
