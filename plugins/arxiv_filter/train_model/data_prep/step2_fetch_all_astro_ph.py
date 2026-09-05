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

import importlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

feedparser = importlib.import_module("feedparser")

try:
    from core.atomic_store import atomic_write_text
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
    from core.atomic_store import atomic_write_text
    from core.bounded_http import (
        XML_MIME_POLICY,
        BodyLimits,
        RedirectPolicy,
        XmlLimits,
        requests_request_bounded,
        validate_bounded_xml,
    )

_utils_module  = importlib.import_module(f"{__package__}.utils" if __package__ else "utils")
clean_arxiv_id = _utils_module.clean_arxiv_id

# ============================================================
# 配置
# ============================================================
DATA_PREP_DIR   = Path(__file__).resolve().parent
CACHE_DIR       = DATA_PREP_DIR / "cache"
DATE_RANGE_FILE = CACHE_DIR / "date_range.json"

API_URL = "https://export.arxiv.org/api/query"
HEADERS = {
    "User-Agent": os.environ.get(
        "ARXIV_USER_AGENT",
        "XiaoQingBot-arxiv-research/2.0 (+https://github.com/SukiYume/XiaoQing)",
    )
}
MAX_RESULTS      = 2000  # 每页最大结果数
RETRY_LIMIT      = 5  # 单次请求重试次数
DELAY            = 3  # 请求间隔（秒）
ATOM_BODY_LIMITS = BodyLimits(
    max_wire_bytes          = 16 * 1024 * 1024,
    max_decoded_bytes       = 32 * 1024 * 1024,
    max_decompression_ratio = 100,
)
ATOM_XML_LIMITS = XmlLimits(
    max_bytes           = ATOM_BODY_LIMITS.max_decoded_bytes,
    max_depth           = 64,
    max_nodes           = 100_000,
    max_attributes      = 200_000,
    max_attribute_chars = 4 * 1024 * 1024,
    max_name_chars      = 512,
    max_text_chars      = 24 * 1024 * 1024,
)
ARXIV_REDIRECT_POLICY = RedirectPolicy(
    max_hops         = 3,
    allowed_schemes  = frozenset({"https"}),
    same_origin_only = True,
)


# ============================================================
# 工具函数
# ============================================================


def load_date_range(path: Path | None = None) -> tuple[str, str]:
    """加载日期范围"""
    with open(path or DATE_RANGE_FILE, encoding="utf-8") as f:
        payload = json.load(f)
    if not isinstance(payload, dict):
        raise ValueError("date_range.json must contain a JSON object")
    start, end = payload.get("start"), payload.get("end")
    if not isinstance(start, str) or not isinstance(end, str):
        raise ValueError("date_range.json must contain string start/end fields")
    start_date = datetime.strptime(start, "%Y-%m-%d")
    end_date   = datetime.strptime(end, "%Y-%m-%d")
    if start_date > end_date:
        raise ValueError("date range start must not be after end")
    return start, end


def generate_monthly_ranges(start_str: str, end_str: str) -> list[tuple[str, str, int]]:
    """
    生成月度查询范围。

    Returns:
        [(api_start, api_end, yymm), ...]
        api_start/api_end 格式: YYYYMMDDHHMM（arXiv API 要求）
        yymm: 月份标识（如 2012 = 2020-12）
    """
    start = datetime.strptime(start_str, "%Y-%m-%d").replace(tzinfo=UTC)
    end_day = datetime.strptime(end_str, "%Y-%m-%d").replace(tzinfo=UTC)
    if start > end_day:
        raise ValueError("date range start must not be after end")

    # 输入是自然日范围。arXiv API 使用分钟精度，因此末日必须扩展到 23:59；
    # 首月则从真实起始日开始，不能为了按月缓存而混入更早的负样本。
    end = end_day + timedelta(days=1) - timedelta(minutes=1)

    ranges = []
    current = datetime(start.year, start.month, 1, tzinfo=UTC)

    while current <= end:
        if current.month == 12:
            next_month = datetime(current.year + 1, 1, 1, tzinfo=UTC)
        else:
            next_month = datetime(current.year, current.month + 1, 1, tzinfo=UTC)

        month_start = max(current, start)
        month_end = next_month - timedelta(minutes=1)
        if month_end > end:
            month_end = end

        yymm      = (current.year - 2000) * 100 + current.month
        api_start = month_start.strftime("%Y%m%d%H%M")
        api_end   = month_end.strftime("%Y%m%d%H%M")

        ranges.append((api_start, api_end, yymm))
        current = next_month

    return ranges


def yymm_to_label(yymm: int) -> str:
    """YYMM → '2020-12' 形式"""
    year  = 2000 + yymm // 100
    month = yymm % 100
    return f"{year}-{month:02d}"


# ============================================================
# 缓存管理
# ============================================================

MONTHLY_DIR = CACHE_DIR / "monthly"


@dataclass(frozen=True, slots=True)
class FetchResult:
    papers: list[dict[str, str]]
    completed: bool
    next_offset: int
    total_results: int | None


def load_cache(yymm: int) -> list[dict[str, str]] | None:
    """加载某月的缓存，不存在返回 None"""
    cache_file = MONTHLY_DIR / f"{yymm}.json"
    if cache_file.exists():
        with open(cache_file, encoding="utf-8") as f:
            payload = json.load(f)
        if isinstance(payload, dict):
            payload = payload.get("papers")
        if not isinstance(payload, list) or any(
            not isinstance(record, dict)
            or any(
                not isinstance(record.get(field), str)
                for field in ("arxiv_id", "title", "abstract")
            )
            for record in payload
        ):
            raise ValueError(f"invalid monthly cache: {cache_file}")
        return payload
    return None


def save_cache(
    yymm: int, papers: list[dict[str, str]], *, api_start: str = "", api_end: str = ""
) -> None:
    """保存某月的缓存"""
    MONTHLY_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = MONTHLY_DIR / f"{yymm}.json"
    atomic_write_text(
        cache_file,
        json.dumps(
            {
                "papers": papers,
                "query_range": [api_start, api_end],
                "completed": True,
            },
            ensure_ascii=False,
        ),
    )


def _partial_path(yymm: int) -> Path:
    return MONTHLY_DIR / f"{yymm}.partial.json"


def load_checkpoint(yymm: int, *, api_start: str = "", api_end: str = "") -> FetchResult:
    path = _partial_path(yymm)
    if not path.exists():
        return FetchResult([], False, 0, None)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"invalid checkpoint: {path}")
    if payload.get("query_range", ["", ""]) != [api_start, api_end]:
        return FetchResult([], False, 0, None)
    papers        = payload.get("papers")
    completed     = payload.get("completed")
    next_offset   = payload.get("next_offset")
    total_results = payload.get("total_results")
    if (
        not isinstance(papers, list)
        or any(
            not isinstance(record, dict)
            or any(
                not isinstance(record.get(field), str)
                for field in ("arxiv_id", "title", "abstract")
            )
            for record in papers
        )
        or type(completed) is not bool
        or type(next_offset) is not int
        or next_offset < 0
        or (total_results is not None and (type(total_results) is not int or total_results < 0))
    ):
        raise ValueError(f"invalid checkpoint: {path}")
    return FetchResult(papers, completed, next_offset, total_results)


def save_checkpoint(
    yymm: int, result: FetchResult, *, api_start: str = "", api_end: str = ""
) -> None:
    MONTHLY_DIR.mkdir(parents=True, exist_ok=True)
    path = _partial_path(yymm)
    atomic_write_text(
        path,
        json.dumps(
            {
                "query_range": [api_start, api_end],
                "papers": result.papers,
                "next_offset": result.next_offset,
                "total_results": result.total_results,
                "completed": result.completed,
            },
            ensure_ascii=False,
        ),
    )


def is_month_finalized(yymm: int, *, api_start: str = "", api_end: str = "") -> bool:
    """
    当月是否已定型（不再需要更新）。
    条件：缓存存在 且 该月已过去（当月数据可能有新增，需重新获取）。
    """
    today        = datetime.now(UTC)
    current_yymm = (today.year - 2000) * 100 + today.month
    identity     = MONTHLY_DIR / f"{yymm}.json"
    if not identity.is_file():
        return False
    # 查询边界变化后从头抓取；历史无身份缓存也通过此路径重新生成。
    try:
        payload = json.loads(identity.read_text(encoding="utf-8"))
        matches = (
            isinstance(payload, dict)
            and payload.get("completed") is True
            and payload.get("query_range") == [api_start, api_end]
        )
    except (OSError, ValueError):
        return False
    return matches and (MONTHLY_DIR / f"{yymm}.json").is_file() and yymm < current_yymm


# ============================================================
# arXiv API 查询
# ============================================================


def fetch_month(
    api_start: str,
    api_end: str,
    *,
    initial_papers: list[dict[str, str]] | None = None,
    start_offset: int                           = 0,
    initial_total_results: int | None           = None,
) -> FetchResult:
    """
    获取指定月份的所有 astrophysics 论文（标题 + 摘要）。
    自动分页，处理重试和速率限制。
    """
    if type(start_offset) is not int or start_offset < 0:
        raise ValueError("start_offset must be a non-negative integer")
    if initial_total_results is not None and (
        type(initial_total_results) is not int or initial_total_results < 0
    ):
        raise ValueError("initial_total_results must be a non-negative integer or None")
    papers        = list(initial_papers or [])
    offset        = start_offset
    total_results = initial_total_results

    while True:
        search_query = f"astrophysics AND submittedDate:[{api_start} TO {api_end}]"
        params       = {
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
                    limits          = ATOM_BODY_LIMITS,
                    mime_policy     = XML_MIME_POLICY,
                    redirect_policy = ARXIV_REDIRECT_POLICY,
                    headers         = HEADERS,
                    request_kwargs  = {"params": params, "timeout": 120},
                )
                xml_body = validate_bounded_xml(response, limits=ATOM_XML_LIMITS)
                feed = feedparser.parse(xml_body)
                if total_results is None and "opensearch_totalresults" in feed.feed:
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

            title    = re.sub(r"\s+", " ", entry.title.strip())
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

        # 以 API 返回的原始条目数推进 offset；无效条目只影响本地记录数，不影响分页游标。
        batch_count = len(feed.entries)
        offset += batch_count
        completed = (
            offset >= total_results if total_results is not None else batch_count < MAX_RESULTS
        )
        if completed:
            return FetchResult(papers, True, offset, total_results)

        time.sleep(DELAY)


# ============================================================
# 主逻辑
# ============================================================


def main() -> None:
    # 1. 加载日期范围
    print("=" * 60)
    print("Step 2: 获取 astro-ph 论文（arXiv API）")
    print("=" * 60)
    start_str, end_str = load_date_range()
    print(f"日期范围: {start_str} ~ {end_str}")

    monthly_ranges = generate_monthly_ranges(start_str, end_str)
    print(f"共 {len(monthly_ranges)} 个月\n")

    # 2. 按月获取
    all_papers    = []
    fetched_count = 0
    cached_count  = 0

    for i, (api_start, api_end, yymm) in enumerate(monthly_ranges):
        label    = yymm_to_label(yymm)
        progress = f"[{i + 1}/{len(monthly_ranges)}]"

        # 检查缓存
        if is_month_finalized(yymm, api_start=api_start, api_end=api_end):
            cached = load_cache(yymm)
            if cached is None:
                continue
            all_papers.extend(cached)
            cached_count += len(cached)
            print(f"  {progress} {label}: {len(cached):>5} 篇 (缓存)")
            continue

        # 从 API 获取
        print(f"  {progress} {label}: 获取中...")
        checkpoint = load_checkpoint(yymm, api_start=api_start, api_end=api_end)
        result = fetch_month(
            api_start,
            api_end,
            initial_papers        = checkpoint.papers,
            start_offset          = checkpoint.next_offset,
            initial_total_results = checkpoint.total_results,
        )

        if result.completed:
            save_cache(yymm, result.papers, api_start=api_start, api_end=api_end)
            _partial_path(yymm).unlink(missing_ok=True)
            all_papers.extend(result.papers)
            fetched_count += len(result.papers)
            print(f"  {progress} {label}: {len(result.papers):>5} 篇 (完整缓存已发布)")
        elif result.papers:
            save_checkpoint(yymm, result, api_start=api_start, api_end=api_end)
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

    seen                         = set()
    total_unique                 = 0
    month_counts: dict[int, int] = {}
    for p in all_papers:
        if p["arxiv_id"] not in seen:
            seen.add(p["arxiv_id"])
            total_unique += 1
            try:
                ym               = int(p["arxiv_id"][:4])
                month_counts[ym] = month_counts.get(ym, 0) + 1
            except ValueError:
                pass

    print(f"总计: {len(all_papers)} 条, 去重后 {total_unique} 条")

    print("\n各月论文数:")
    for ym in sorted(month_counts.keys()):
        print(f"  {yymm_to_label(ym)}: {month_counts[ym]:>5} 篇")

    print(f"\n缓存目录: {CACHE_DIR}/")


if __name__ == "__main__":
    main()
