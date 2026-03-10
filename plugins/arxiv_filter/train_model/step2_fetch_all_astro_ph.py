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
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import feedparser

# 确保从脚本所在目录导入 utils
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from utils import clean_arxiv_id

# ============================================================
# 配置
# ============================================================
CACHE_DIR = Path("cache")
DATE_RANGE_FILE = CACHE_DIR / "date_range.json"

API_URL = "http://export.arxiv.org/api/query"
HEADERS = {"User-Agent": "arxiv-scraper/2.0 (SukiYume@users.noreply.github.com)"}
MAX_RESULTS = 2000       # 每页最大结果数
RETRY_LIMIT = 5          # 单次请求重试次数
DELAY = 3                # 请求间隔（秒）


# ============================================================
# 工具函数
# ============================================================

def load_date_range() -> tuple[str, str]:
    """加载日期范围"""
    with open(DATE_RANGE_FILE, 'r', encoding='utf-8') as f:
        dr = json.load(f)
    return dr['start'], dr['end']


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


def load_cache(yymm: int) -> list[dict] | None:
    """加载某月的缓存，不存在返回 None"""
    cache_file = MONTHLY_DIR / f"{yymm}.json"
    if cache_file.exists():
        with open(cache_file, 'r', encoding='utf-8') as f:
            return json.load(f)
    return None


def save_cache(yymm: int, papers: list[dict]):
    """保存某月的缓存"""
    MONTHLY_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = MONTHLY_DIR / f"{yymm}.json"
    with open(cache_file, 'w', encoding='utf-8') as f:
        json.dump(papers, f, ensure_ascii=False)


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

def fetch_month(api_start: str, api_end: str) -> list[dict]:
    """
    获取指定月份的所有 astrophysics 论文（标题 + 摘要）。
    自动分页，处理重试和速率限制。
    """
    papers = []
    offset = 0
    total_results = None

    while True:
        search_query = f'astrophysics AND submittedDate:[{api_start} TO {api_end}]'
        params = {
            'search_query': search_query,
            'start': offset,
            'max_results': MAX_RESULTS,
            'sortBy': 'submittedDate',
            'sortOrder': 'ascending',
        }

        # 带重试的请求
        feed = None
        for attempt in range(1, RETRY_LIMIT + 1):
            try:
                r = requests.get(API_URL, params=params, headers=HEADERS, timeout=120)
                r.raise_for_status()
                feed = feedparser.parse(r.content)
                if offset == 0 and 'opensearch_totalresults' in feed.feed:
                    total_results = int(feed.feed.opensearch_totalresults)
                break
            except Exception as e:
                wait = 10 * attempt
                print(f"      [重试 {attempt}/{RETRY_LIMIT}] {e}, 等待 {wait}s")
                if attempt == RETRY_LIMIT:
                    print(f"      跳过当前批次 (offset={offset})")
                    return papers
                time.sleep(wait)

        if not feed or not feed.entries:
            break

        # 解析结果
        batch = []
        for entry in feed.entries:
            arxiv_id = clean_arxiv_id(entry.id)
            # 跳过无效条目（API 无结果时返回错误条目）
            if not re.match(r'\d{4}\.\d{4,5}', arxiv_id):
                continue

            title = re.sub(r'\s+', ' ', entry.title.strip())
            abstract = re.sub(r'\s+', ' ', entry.get('summary', '').strip())
            batch.append({
                'arxiv_id': arxiv_id,
                'title': title,
                'abstract': abstract,
            })

        papers.extend(batch)

        if total_results and total_results > 0:
            pct = len(papers) / total_results * 100
            print(f"      {len(papers)}/{total_results} ({pct:.0f}%)", end='\r')

        # 没有更多结果
        if len(feed.entries) < MAX_RESULTS:
            break

        offset += MAX_RESULTS
        time.sleep(DELAY)

    return papers


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
            all_papers.extend(cached)
            cached_count += len(cached)
            print(f"  {progress} {label}: {len(cached):>5} 篇 (缓存)")
            continue

        # 从 API 获取
        print(f"  {progress} {label}: 获取中...")
        papers = fetch_month(api_start, api_end)

        if papers:
            save_cache(yymm, papers)
            all_papers.extend(papers)
            fetched_count += len(papers)
            print(f"  {progress} {label}: {len(papers):>5} 篇 (已保存)")
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
        if p['arxiv_id'] not in seen:
            seen.add(p['arxiv_id'])
            total_unique += 1
            try:
                ym = int(p['arxiv_id'][:4])
                month_counts[ym] = month_counts.get(ym, 0) + 1
            except (ValueError, IndexError):
                pass

    print(f"总计: {len(all_papers)} 条, 去重后 {total_unique} 条")

    print("\n各月论文数:")
    for ym in sorted(month_counts.keys()):
        print(f"  {yymm_to_label(ym)}: {month_counts[ym]:>5} 篇")

    print(f"\n缓存目录: {CACHE_DIR}/")


if __name__ == '__main__':
    main()
