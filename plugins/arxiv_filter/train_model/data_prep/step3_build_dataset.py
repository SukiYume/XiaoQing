"""
Step 3: 构建最终训练数据集

1. 读取正样本 ID（来自 step 1，cache/positive_ids.csv）
2. 从月度缓存读取所有 astro-ph 论文（来自 step 2，cache/YYMM.json）
3. 标记正样本和负样本
4. 补充获取在笔记中但不在批量数据中的正样本论文（跨类别引用等边缘情况）
5. 输出最终训练数据集

输入:
- cache/positive_ids.csv: 正样本 ID（来自 step 1）
- cache/monthly/YYMM.json: 每月论文缓存（来自 step 2）

输出:
- arxiv_papers_with_abstract.csv: 最终训练数据 (arXiv ID, Title, Abstract, label)

更新方式: step 1, 2 运行后，直接运行本脚本重建数据集。

依赖: requests, feedparser, pandas
"""

import importlib
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import cast

import pandas as pd
import requests

feedparser = importlib.import_module("feedparser")

try:
    from core.bounded_http import (
        XML_MIME_POLICY,
        BodyLimits,
        BoundedHttpError,
        HttpStatusError,
        RedirectPolicy,
        XmlLimits,
        requests_request_bounded,
        validate_bounded_xml,
    )
except ModuleNotFoundError:  # Direct script execution outside the repository root.
    repository_root = Path(__file__).resolve().parents[4]
    if str(repository_root) not in sys.path:
        sys.path.insert(0, str(repository_root))
    from core.bounded_http import (
        XML_MIME_POLICY,
        BodyLimits,
        BoundedHttpError,
        HttpStatusError,
        RedirectPolicy,
        XmlLimits,
        requests_request_bounded,
        validate_bounded_xml,
    )

try:
    from .utils import clean_arxiv_id
except ImportError:  # Direct script execution.
    from utils import clean_arxiv_id

# ============================================================
# 配置
# ============================================================
DATA_PREP_DIR = Path(__file__).resolve().parent
TRAIN_MODEL_DIR = DATA_PREP_DIR.parent
CACHE_DIR = DATA_PREP_DIR / "cache"
POSITIVE_IDS_CSV = CACHE_DIR / "positive_ids.csv"
OUTPUT_CSV = TRAIN_MODEL_DIR / "arxiv_papers_with_abstract.csv"

ABSTRACT_CACHE_FILE = CACHE_DIR / "abstract_cache.json"  # 用于缓存补充获取的正样本摘要

BATCH_SIZE = 100
SLEEP_SECONDS = 3
API_TIMEOUT = 120
MAX_RETRIES = 3
ATOM_BODY_LIMITS = BodyLimits(
    max_wire_bytes=8 * 1024 * 1024,
    max_decoded_bytes=16 * 1024 * 1024,
    max_decompression_ratio=100,
)
ATOM_XML_LIMITS = XmlLimits(
    max_bytes=ATOM_BODY_LIMITS.max_decoded_bytes,
    max_depth=64,
    max_nodes=50_000,
    max_attributes=100_000,
    max_attribute_chars=2 * 1024 * 1024,
    max_name_chars=512,
    max_text_chars=12 * 1024 * 1024,
)
ARXIV_REDIRECT_POLICY = RedirectPolicy(
    max_hops=3,
    allowed_schemes=frozenset({"https"}),
    same_origin_only=True,
)


# ============================================================
# 工具函数
# ============================================================


def load_abstract_cache() -> dict[str, dict[str, str]]:
    """加载补充摘要缓存"""
    if os.path.exists(ABSTRACT_CACHE_FILE):
        with open(ABSTRACT_CACHE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_abstract_cache(cache: dict[str, dict[str, str]]) -> None:
    """保存补充摘要缓存"""
    with open(ABSTRACT_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False)


def compute_positive_coverage(df_output: pd.DataFrame, positive_ids: set[str]) -> dict[str, int]:
    dataset_ids = set(df_output["arXiv ID"].astype(str).apply(clean_arxiv_id))
    covered_positive_ids = len(dataset_ids & positive_ids)
    missing_positive_ids = len(positive_ids - dataset_ids)
    return {
        "covered_positive_ids": covered_positive_ids,
        "missing_positive_ids": missing_positive_ids,
        "dataset_positive_rows": int((df_output["label"] == 1).sum()),
        "expected_positive_ids": len(positive_ids),
    }


def ensure_positive_coverage(df_output: pd.DataFrame, positive_ids: set[str]) -> dict[str, int]:
    coverage = compute_positive_coverage(df_output, positive_ids)
    if coverage["missing_positive_ids"] > 0:
        raise RuntimeError(
            f"Final dataset is missing positive IDs: {coverage['missing_positive_ids']} missing positive samples"
        )
    return coverage


# ============================================================
# arXiv API（用于补充获取缺失的正样本）
# ============================================================


def fetch_abstracts_batch(arxiv_ids: list[str]) -> dict[str, dict[str, str]]:
    """通过 arXiv API 批量获取论文标题和摘要"""
    url = "https://export.arxiv.org/api/query"
    params = {
        "id_list": ",".join(arxiv_ids),
        "max_results": len(arxiv_ids),
    }

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests_request_bounded(
                "GET",
                url,
                limits=ATOM_BODY_LIMITS,
                mime_policy=XML_MIME_POLICY,
                redirect_policy=ARXIV_REDIRECT_POLICY,
                headers={"User-Agent": "arxiv-training-data-builder/2.0"},
                request_kwargs={"params": params, "timeout": API_TIMEOUT},
            )
            xml_body = validate_bounded_xml(response, limits=ATOM_XML_LIMITS)
            feed = feedparser.parse(xml_body)
            break
        except HttpStatusError as e:
            if e.status == 400:
                # 可能有无效 ID，尝试逐个获取
                return _fetch_one_by_one(arxiv_ids)
            print(f"  [重试 {attempt}/{MAX_RETRIES}] HTTP {e.status}: {e}")
            if attempt < MAX_RETRIES:
                time.sleep(SLEEP_SECONDS * attempt)
            else:
                return {}
        except (requests.RequestException, BoundedHttpError) as e:
            print(f"  [重试 {attempt}/{MAX_RETRIES}] 请求异常: {e}")
            if attempt < MAX_RETRIES:
                time.sleep(SLEEP_SECONDS * attempt)
            else:
                return {}

    results: dict[str, dict[str, str]] = {}
    for entry in feed.entries:
        aid = clean_arxiv_id(entry.id)
        title = re.sub(r"\s+", " ", entry.title.strip())
        abstract = re.sub(r"\s+", " ", entry.summary.strip())
        results[aid] = {"title": title, "abstract": abstract}

    return results


def _fetch_one_by_one(arxiv_ids: list[str]) -> dict[str, dict[str, str]]:
    """逐个获取（当批量获取返回 400 时的回退策略）"""
    results: dict[str, dict[str, str]] = {}
    for aid in arxiv_ids:
        try:
            batch = fetch_abstracts_batch_single(aid)
            results.update(batch)
        except Exception:
            print(f"    [跳过无效 ID] {aid}")
        time.sleep(1)
    return results


def fetch_abstracts_batch_single(arxiv_id: str) -> dict[str, dict[str, str]]:
    """获取单篇论文"""
    url = "https://export.arxiv.org/api/query"
    params = {"id_list": arxiv_id, "max_results": 1}

    response = requests_request_bounded(
        "GET",
        url,
        limits=ATOM_BODY_LIMITS,
        mime_policy=XML_MIME_POLICY,
        redirect_policy=ARXIV_REDIRECT_POLICY,
        headers={"User-Agent": "arxiv-training-data-builder/2.0"},
        request_kwargs={"params": params, "timeout": API_TIMEOUT},
    )
    xml_body = validate_bounded_xml(response, limits=ATOM_XML_LIMITS)
    feed = feedparser.parse(xml_body)
    results: dict[str, dict[str, str]] = {}
    for entry in feed.entries:
        aid = clean_arxiv_id(entry.id)
        title = re.sub(r"\s+", " ", entry.title.strip())
        abstract = re.sub(r"\s+", " ", entry.summary.strip())
        results[aid] = {"title": title, "abstract": abstract}
    return results


# ============================================================
# 主逻辑
# ============================================================


def main():
    # ── 1. 读取正样本 ID ──────────────────────────────────────
    print("=" * 60)
    print("读取正样本 ID")
    print("=" * 60)
    df_pos = pd.read_csv(POSITIVE_IDS_CSV, dtype={"arXiv ID": str})
    df_pos["arXiv ID"] = df_pos["arXiv ID"].apply(clean_arxiv_id)
    positive_ids = set(df_pos["arXiv ID"])
    print(f"正样本: {len(positive_ids)} 个唯一 ID")

    # ── 2. 从月度缓存读取论文 ─────────────────────────────────
    print("\n" + "=" * 60)
    print("从缓存加载论文数据")
    print("=" * 60)
    all_records = []
    monthly_dir = CACHE_DIR / "monthly"
    cache_files = sorted(monthly_dir.glob("[0-9][0-9][0-9][0-9].json"))
    for cf in cache_files:
        with open(cf, encoding="utf-8") as f:
            month_data = json.load(f)
        all_records.extend(month_data)
        print(f"  {cf.stem}: {len(month_data)} 篇")
    print(f"总计加载: {len(all_records)} 篇")

    df_all = pd.DataFrame(all_records)
    df_all["arxiv_id"] = df_all["arxiv_id"].apply(clean_arxiv_id)
    # 去重
    before = len(df_all)
    df_all = df_all.drop_duplicates(subset=["arxiv_id"])
    if len(df_all) < before:
        print(f"去重: {before} -> {len(df_all)}")
    print(f"批量数据: {len(df_all)} 篇论文")

    # ── 3. 标记标签 ───────────────────────────────────────────
    print("\n" + "=" * 60)
    print("标记标签")
    print("=" * 60)
    df_all["label"] = df_all["arxiv_id"].apply(lambda x: 1 if x in positive_ids else 0)

    found_positive = set(df_all[df_all["label"] == 1]["arxiv_id"])
    missing_positive = positive_ids - found_positive

    n_pos = (df_all["label"] == 1).sum()
    n_neg = (df_all["label"] == 0).sum()
    print(f"在批量数据中标记的正样本: {n_pos}")
    print(f"负样本: {n_neg}")
    print(f"未在批量数据中找到的正样本: {len(missing_positive)}")

    # ── 4. 补充获取缺失的正样本 ──────────────────────────────
    if missing_positive:
        print("\n" + "=" * 60)
        print(f"补充获取 {len(missing_positive)} 个缺失正样本")
        print("=" * 60)

        # 先查缓存
        cache = load_abstract_cache()
        still_missing = [aid for aid in missing_positive if aid not in cache]
        from_cache = len(missing_positive) - len(still_missing)
        if from_cache > 0:
            print(f"  从缓存中恢复: {from_cache} 个")

        # API 获取剩余的
        if still_missing:
            print(f"  需要从 API 获取: {len(still_missing)} 个")
            missing_list = sorted(still_missing)

            for i in range(0, len(missing_list), BATCH_SIZE):
                batch = missing_list[i : i + BATCH_SIZE]
                batch_num = i // BATCH_SIZE + 1
                total_batches = (len(missing_list) + BATCH_SIZE - 1) // BATCH_SIZE
                print(
                    f"    批次 {batch_num}/{total_batches}: {len(batch)} 篇...", end="", flush=True
                )

                results = fetch_abstracts_batch(batch)
                cache.update(results)
                print(f" 成功 {len(results)}/{len(batch)}")

                if i + BATCH_SIZE < len(missing_list):
                    time.sleep(SLEEP_SECONDS)

            save_abstract_cache(cache)
            print(f"  缓存已更新 ({len(cache)} 条)")

        # 构建补充数据行
        supplementary = []
        for aid in missing_positive:
            if aid in cache:
                info = cache[aid]
                supplementary.append(
                    {
                        "arxiv_id": aid,
                        "title": info["title"],
                        "abstract": info["abstract"],
                        "label": 1,
                    }
                )

        if supplementary:
            df_supp = pd.DataFrame(supplementary)
            df_all = pd.concat([df_all, df_supp], ignore_index=True)
            print(f"  补充添加 {len(supplementary)} 个正样本")

        not_found = len(missing_positive) - len(supplementary)
        if not_found > 0:
            print(f"  [警告] {not_found} 个正样本未能获取到数据")

    # ── 5. 输出最终数据集 ─────────────────────────────────────
    print("\n" + "=" * 60)
    print("输出最终数据集")
    print("=" * 60)

    # 统一列名
    df_output = df_all.rename(
        columns={
            "arxiv_id": "arXiv ID",
            "title": "Title",
            "abstract": "Abstract",
        }
    )

    # 保留目标列
    keep_cols = ["arXiv ID", "Title", "Abstract", "label"]
    df_output = df_output[[c for c in keep_cols if c in df_output.columns]].copy()

    # 过滤无摘要的记录
    before = len(df_output)
    abstract_series = cast(pd.Series, df_output["Abstract"])
    df_output = cast(pd.DataFrame, df_output.loc[abstract_series.fillna("").str.len() > 10].copy())
    removed = before - len(df_output)
    if removed > 0:
        print(f"  移除 {removed} 条无摘要记录")

    # 去重
    before = len(df_output)
    df_output = cast(pd.DataFrame, df_output.drop_duplicates(subset=["arXiv ID"]))
    dupes = before - len(df_output)
    if dupes > 0:
        print(f"  去重 {dupes} 条")

    coverage = ensure_positive_coverage(df_output, positive_ids)
    print(
        "  正样本覆盖: {covered}/{expected} (label=1 行数: {rows})".format(
            covered=coverage["covered_positive_ids"],
            expected=coverage["expected_positive_ids"],
            rows=coverage["dataset_positive_rows"],
        )
    )

    # 保存
    df_output.to_csv(OUTPUT_CSV, index=False)

    n_pos = (df_output["label"] == 1).sum()
    n_neg = (df_output["label"] == 0).sum()
    total = len(df_output)

    print(f"\n最终数据集: {total} 条")
    print(f"  正样本: {n_pos} ({n_pos / total * 100:.2f}%)")
    print(f"  负样本: {n_neg} ({n_neg / total * 100:.2f}%)")
    print(f"\n已保存到 {OUTPUT_CSV}")

    # 显示正样本示例
    print("\n正样本示例 (前 3 条):")
    for _, row in df_output[df_output["label"] == 1].head(3).iterrows():
        print(f"  ID: {row['arXiv ID']}")
        print(f"  Title: {str(row['Title'])[:80]}...")
        print()


if __name__ == "__main__":
    main()
