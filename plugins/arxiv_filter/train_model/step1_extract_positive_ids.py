"""
Step 1: 从 apod 笔记中提取正样本 arXiv ID 和日期范围

扫描 APOD_ROOT 下所有 AstroPH-*.md 文件：
1. 提取 arxiv.org 链接中的 arXiv ID 作为正样本
2. 提取 ## YYYY-MM-DD 日期标题确定笔记覆盖的日期范围

输出 (cache/ 目录下):
- cache/positive_ids.csv: 正样本 ID 列表 (arXiv ID, source_file)
- cache/date_range.json: 笔记覆盖的日期范围 {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"}

更新方式: 直接重新运行即可，会自动扫描所有笔记文件。
"""

import os
import re
import csv
import json
from pathlib import Path

# ============================================================
# 配置
# ============================================================
APOD_ROOT = Path(os.environ.get("APOD_ROOT", r"D:/EscapeWeb/vitepress/docs/apod"))
CACHE_DIR = Path("cache")
OUTPUT_IDS_CSV = CACHE_DIR / "positive_ids.csv"
OUTPUT_DATE_RANGE = CACHE_DIR / "date_range.json"

# 匹配 arxiv.org/abs/XXXX.XXXXX 或 arxiv.org/pdf/XXXX.XXXXX
ARXIV_PATTERN = re.compile(r'arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5})')
# 匹配日期标题 ## YYYY-MM-DD
DATE_HEADER_PATTERN = re.compile(r'^## (\d{4}-\d{2}-\d{2})', re.MULTILINE)


def extract_from_file(filepath: Path) -> tuple[list[dict], list[str]]:
    """从单个 md 文件中提取 arXiv ID 和日期"""
    ids = []
    dates = []

    try:
        text = filepath.read_text(encoding='utf-8')
    except Exception as e:
        print(f"  [WARN] 无法读取 {filepath}: {e}")
        return ids, dates

    for match in ARXIV_PATTERN.finditer(text):
        ids.append({
            'arXiv ID': match.group(1),
            'source_file': str(filepath.relative_to(APOD_ROOT)),
        })

    for match in DATE_HEADER_PATTERN.finditer(text):
        dates.append(match.group(1))

    return ids, dates


def main():
    md_files = sorted(APOD_ROOT.rglob("AstroPH-*.md"))
    print(f"找到 {len(md_files)} 个笔记文件")

    all_ids = []
    all_dates = []

    for f in md_files:
        ids, dates = extract_from_file(f)
        if ids:
            print(f"  {f.relative_to(APOD_ROOT)}: {len(ids)} 篇论文, {len(dates)} 个日期")
        all_ids.extend(ids)
        all_dates.extend(dates)

    # 去重 ID（同一篇论文可能在多个地方被引用）
    seen = set()
    unique_ids = []
    for r in all_ids:
        if r['arXiv ID'] not in seen:
            seen.add(r['arXiv ID'])
            unique_ids.append(r)

    print(f"\n总计: {len(all_ids)} 条记录, 去重后 {len(unique_ids)} 个唯一 arXiv ID")

    # 确定日期范围
    if all_dates:
        date_start = min(all_dates)
        date_end = max(all_dates)
    else:
        date_start = "2020-12-05"
        date_end = "2026-03-05"
        print("[WARN] 未从笔记中提取到日期, 使用默认范围")

    print(f"日期范围: {date_start} ~ {date_end}")

    # 确保缓存目录存在
    CACHE_DIR.mkdir(exist_ok=True)

    # 保存正样本 ID CSV
    with open(OUTPUT_IDS_CSV, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=['arXiv ID', 'source_file'])
        writer.writeheader()
        writer.writerows(unique_ids)
    print(f"已保存正样本 ID 到 {OUTPUT_IDS_CSV}")

    # 保存日期范围
    date_range = {'start': date_start, 'end': date_end}
    with open(OUTPUT_DATE_RANGE, 'w', encoding='utf-8') as f:
        json.dump(date_range, f, ensure_ascii=False, indent=2)
    print(f"已保存日期范围到 {OUTPUT_DATE_RANGE}")


if __name__ == '__main__':
    main()
