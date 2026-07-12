"""
Step 2: 获取日期范围内所有 astro-ph 论文（标题 + 摘要）

使用 arXiv REST API 按月查询天体物理类论文。通过月度缓存实现增量更新，
已完成的历史月份不再重新获取，当月数据每次运行时重新拉取。

查询方式参照 GetArXiv.ipynb:
  search_query = 'astrophysics AND submittedDate:[start TO end]'
  分页 + feedparser 解析

输入:
  cache/date_range.json — 日期范围（来自 step 1）

输出:
  cache/monthly/YYMM.json — 每月缓存

首次运行耗时较长，后续运行仅获取新增月份。

依赖: requests, feedparser
  pip install requests feedparser
"""

import json
import importlib
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

feedparser = importlib.import_module("feedparser")

try:
    from core.bounded_http import (
        XML_MIME_POLICY,
        BodyLimits,
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
CACHE_DIR = DATA_PREP_DIR / "cache"
DATE_RANGE_FILE = CACHE_DIR / "date_range.json"

API_URL = "https://export.arxiv.org/api/query"
HEADERS = {
    "User-Agent": os.environ.get(
        "ARXIV_USER_AGENT",
        "XiaoQingBot-arxiv-research/2.0 (+https://github.com/xiaoqing-bot/xiaoqing)",
    )
}
MAX_RESULTS = 2000  # 每页最大结果数
RETRY_LIMIT = 5  # 单次请求重试次数
DELAY = 3  # 请求间隔（秒）
ATOM_BODY_LIMITS = BodyLimits(
    max_wire_bytes=16 * 1024 * 1024,
    max_decoded_bytes=32 * 1024 * 1024,
    max_decompression_ratio=100,
)
ATOM_XML_LIMITS = XmlLimits(
    max_bytes=ATOM_BODY_LIMITS.max_decoded_bytes,
    max_depth=64,
    max_nodes=100_000,
    max_attributes=200_000,
    max_attribute_chars=4 * 1024 * 1024,
    max_name_chars=512,
    max_text_chars=24 * 1024 * 1024,
)
ARXIV_REDIRECT_POLICY = RedirectPolicy(
    max_hops=3,
    allowed_schemes=frozenset({"https"}),
    same_origin_only=True,
)


# ============================================================
# 工具函数
# ============================================================


def load_date_range() -> tuple[str, str]:
    """加载日期范围"""
    with open(DATE_RANGE_FILE, "r", encoding="utf-8") as f:
        dr = json.load(f)
    return dr["start"], dr["end"]


def generate_monthly_ranges(start_str: str, end_str: str) -> list[tuple[str, str, int]]:
    """
    生成月度查询范围。

    Returns:
        [(api_start, api_end, yymm), ...]
        api_start/api_end 格式: YYYYMMDDHHMM（arXiv API 要求）
        yymm: 月份标识（如 2012 = 2020-12）
    """
    start = datetime.strptime(start_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end = datetime.strptime(end_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)

    ranges = []
    current = datetime(start.year, start.month, 1, tzinfo=timezone.utc)

    while current <= end:
        if current.month == 12:
            next_month = datetime(current.year + 1, 1, 1, tzinfo=timezone.utc)
        else:
            next_month = datetime(current.year, current.month + 1, 1, tzinfo=timezone.utc)

        month_end = next_month - timedelta(seconds=1)
        if month_end > end:
            month_end = end

        yymm = (current.year - 2000) * 100 + current.month
        api_start = current.strftime("%Y%m%d%H%M")
        api_end = month_end.strftime("%Y%m%d%H%M")

        ranges.append((api_start, api_end, yymm))
        current = next_month

    return ranges


def yymm_to_label(yymm: int) -> str:
    """YYMM → '2020-12' 形式"""
    year = 2000 + yymm // 100
    month = yymm % 100
    return f"{year}-{month:02d}"


# ============================================================
# 缓存管理
# ============================================================

MONTHLY_DIR = CACHE_DIR / "monthly"


@dataclass
class FetchResult:
    papers: list[dict[str, str]]
    completed: bool
    next_offset: int
    total_results: int | None


def load_cache(yymm: int) -> list[dict[str, str]] | None:
    """加载某月的缓存，不存在返回 None"""
    cache_file = MONTHLY_DIR / f"{yymm}.json"
    if cache_file.exists():
        with open(cache_file, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


def save_cache(yymm: int, papers: list[dict[str, str]]):
    """保存某月的缓存"""
    MONTHLY_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = MONTHLY_DIR / f"{yymm}.json"
    temp_file = cache_file.with_suffix(".json.tmp")
    temp_file.write_text(json.dumps(papers, ensure_ascii=False), encoding="utf-8")
    temp_file.replace(cache_file)


def _partial_path(yymm: int) -> Path:
    return MONTHLY_DIR / f"{yymm}.partial.json"


def load_checkpoint(yymm: int) -> dict:
    path = _partial_path(yymm)
    if not path.exists():
        return {"papers": [], "next_offset": 0, "total_results": None, "completed": False}
    return json.loads(path.read_text(encoding="utf-8"))


def save_checkpoint(yymm: int, result: FetchResult) -> None:
    MONTHLY_DIR.mkdir(parents=True, exist_ok=True)
    path = _partial_path(yymm)
    temp = path.with_suffix(".json.tmp")
    temp.write_text(
        json.dumps(
            {
                "papers": result.papers,
                "next_offset": result.next_offset,
                "total_results": result.total_results,
                "completed": result.completed,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    temp.replace(path)


def is_month_finalized(yymm: int) -> bool:
    """
    当月是否已定型（不再需要更新）。
    条件：缓存存在 且 该月已过去（当月数据可能有新增，需重新获取）。
    """
    today = datetime.now(timezone.utc)
    current_yymm = (today.year - 2000) * 100 + today.month
    return load_cache(yymm) is not None and yymm < current_yymm


# ============================================================
# arXiv API 查询
# ============================================================


def fetch_month(
    api_start: str,
    api_end: str,
    *,
    initial_papers: list[dict[str, str]] | None = None,
    start_offset: int = 0,
) -> FetchResult:
    """
    获取指定月份的所有 astrophysics 论文（标题 + 摘要）。
    自动分页，处理重试和速率限制。
    """
    papers = list(initial_papers or [])
    offset = start_offset
    total_results = None

    while True:
        search_query = f"astrophysics AND submittedDate:[{api_start} TO {api_end}]"
        params = {
            "search_query": search_query,
            "start": offset,
            "max_results": MAX_RESULTS,
            "sortBy": "submittedDate",
            "sortOrder": "ascending",
        }

        # 带重试的请求
        feed = None
        for attempt in range(1, RETRY_LIMIT + 1):
            try:
                response = requests_request_bounded(
                    "GET",
                    API_URL,
                    limits=ATOM_BODY_LIMITS,
                    mime_policy=XML_MIME_POLICY,
                    redirect_policy=ARXIV_REDIRECT_POLICY,
                    headers=HEADERS,
                    request_kwargs={"params": params, "timeout": 120},
                )
                xml_body = validate_bounded_xml(response, limits=ATOM_XML_LIMITS)
                feed = feedparser.parse(xml_body)
                if offset == 0 and "opensearch_totalresults" in feed.feed:
                    total_results = int(feed.feed.opensearch_totalresults)
                break
            except Exception as e:
                wait = 10 * attempt
                print(f"      [重试 {attempt}/{RETRY_LIMIT}] {e}, 等待 {wait}s")
                if attempt == RETRY_LIMIT:
                    print(f"      跳过当前批次 (offset={offset})")
                    return FetchResult(papers, False, offset, total_results)
                time.sleep(wait)

        if not feed or not feed.entries:
            completed = total_results is None or offset >= total_results
            return FetchResult(papers, completed, offset, total_results)

        # 解析结果
        batch = []
        for entry in feed.entries:
            arxiv_id = clean_arxiv_id(entry.id)
            # 跳过无效条目（API 无结果时返回错误条目）
            if not re.match(r"\d{4}\.\d{4,5}", arxiv_id):
                continue

            title = re.sub(r"\s+", " ", entry.title.strip())
            abstract = re.sub(r"\s+", " ", entry.get("summary", "").strip())
            batch.append(
                {
                    "arxiv_id": arxiv_id,
                    "title": title,
                    "abstract": abstract,
                }
            )

        papers.extend(batch)

        if total_results and total_results > 0:
            pct = len(papers) / total_results * 100
            print(f"      {len(papers)}/{total_results} ({pct:.0f}%)", end="\r")

        # 没有更多结果
        offset += len(feed.entries)
        if len(feed.entries) < MAX_RESULTS:
            return FetchResult(papers, True, offset, total_results)

        time.sleep(DELAY)


# ============================================================
# 主逻辑
# ============================================================


def main():
    # 1. 加载日期范围
    print("=" * 60)
    print("Step 2: 获取 astro-ph 论文（arXiv API）")
    print("=" * 60)
    start_str, end_str = load_date_range()
    print(f"日期范围: {start_str} ~ {end_str}")

    monthly_ranges = generate_monthly_ranges(start_str, end_str)
    print(f"共 {len(monthly_ranges)} 个月\n")

    # 2. 按月获取
    all_papers = []
    fetched_count = 0
    cached_count = 0

    for i, (api_start, api_end, yymm) in enumerate(monthly_ranges):
        label = yymm_to_label(yymm)
        progress = f"[{i + 1}/{len(monthly_ranges)}]"

        # 检查缓存
        if is_month_finalized(yymm):
            cached = load_cache(yymm)
            if cached is None:
                continue
            all_papers.extend(cached)
            cached_count += len(cached)
            print(f"  {progress} {label}: {len(cached):>5} 篇 (缓存)")
            continue

        # 从 API 获取
        print(f"  {progress} {label}: 获取中...")
        checkpoint = load_checkpoint(yymm)
        result = fetch_month(
            api_start,
            api_end,
            initial_papers=checkpoint.get("papers", []),
            start_offset=int(checkpoint.get("next_offset", 0)),
        )

        if result.completed:
            save_cache(yymm, result.papers)
            _partial_path(yymm).unlink(missing_ok=True)
            all_papers.extend(result.papers)
            fetched_count += len(result.papers)
            print(f"  {progress} {label}: {len(result.papers):>5} 篇 (完整缓存已发布)")
        elif result.papers:
            save_checkpoint(yymm, result)
            print(
                f"  {progress} {label}: {len(result.papers):>5} 篇 "
                f"(partial, 下次从 offset={result.next_offset} 续传)"
            )
        else:
            print(f"  {progress} {label}: 无数据")

    # 3. 汇总统计
    print(f"\n{'=' * 60}")
    print("汇总")
    print("=" * 60)
    print(f"从缓存加载: {cached_count} 条")
    print(f"新获取:     {fetched_count} 条")

    seen = set()
    total_unique = 0
    month_counts: dict[int, int] = {}
    for p in all_papers:
        if p["arxiv_id"] not in seen:
            seen.add(p["arxiv_id"])
            total_unique += 1
            try:
                ym = int(p["arxiv_id"][:4])
                month_counts[ym] = month_counts.get(ym, 0) + 1
            except (ValueError, IndexError):
                pass

    print(f"总计: {len(all_papers)} 条, 去重后 {total_unique} 条")

    print("\n各月论文数:")
    for ym in sorted(month_counts.keys()):
        print(f"  {yymm_to_label(ym)}: {month_counts[ym]:>5} 篇")

    print(f"\n缓存目录: {CACHE_DIR}/")


if __name__ == "__main__":
    main()
