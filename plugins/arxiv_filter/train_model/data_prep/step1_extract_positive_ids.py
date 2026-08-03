"""
Step 1: 从 apod 笔记中提取正样本 arXiv ID 和日期范围

扫描 APOD_ROOT 下所有 AstroPH-*.md 文件：
1. 提取 arxiv.org 链接中的 arXiv ID 作为正样本
2. 提取 ## YYYY-MM-DD 日期标题确定笔记覆盖的日期范围

输出 (cache/ 目录下):
- cache/positive_ids.csv: 正样本 ID 列表 (arXiv ID, source_file)
- cache/date_range.json: 笔记覆盖的日期范围 {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"}

更新方式: 直接重新运行即可，会自动扫描所有笔记文件。

默认读取本机 APOD 笔记目录，也可以通过 APOD_ROOT 环境变量覆盖。
"""

import csv
import io
import json
import os
import re
import sys
from datetime import date
from pathlib import Path

try:
    from core.atomic_store import atomic_write_text
except ModuleNotFoundError:  # 允许在仓库根目录之外直接执行脚本。
    repository_root = Path(__file__).resolve().parents[4]
    if str(repository_root) not in sys.path:
        sys.path.insert(0, str(repository_root))
    from core.atomic_store import atomic_write_text

# ============================================================
# 配置
# ============================================================
DEFAULT_APOD_ROOT = Path(r"D:/EscapeWeb/vitepress/docs/apod")
DATA_PREP_DIR = Path(__file__).resolve().parent
CACHE_DIR = DATA_PREP_DIR / "cache"
OUTPUT_IDS_CSV = CACHE_DIR / "positive_ids.csv"
OUTPUT_DATE_RANGE = CACHE_DIR / "date_range.json"

# 匹配 arxiv.org/abs/XXXX.XXXXX 或 arxiv.org/pdf/XXXX.XXXXX
ARXIV_PATTERN = re.compile(r"arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5})")
# 匹配日期标题 ## YYYY-MM-DD
DATE_HEADER_PATTERN = re.compile(r"^## (\d{4}-\d{2}-\d{2})", re.MULTILINE)


def resolve_apod_root() -> Path:
    apod_root = Path(os.environ.get("APOD_ROOT", DEFAULT_APOD_ROOT)).expanduser()
    if not apod_root.exists():
        raise SystemExit(f"APOD_ROOT 不存在: {apod_root}")
    if not apod_root.is_dir():
        raise SystemExit(f"APOD_ROOT 不是目录: {apod_root}")
    return apod_root


def extract_from_file(filepath: Path, apod_root: Path) -> tuple[list[dict[str, str]], list[str]]:
    """从单个 md 文件中提取 arXiv ID 和日期"""
    ids: list[dict[str, str]] = []
    dates: list[str] = []

    try:
        text = filepath.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        print(f"  [WARN] 无法读取 {filepath}: {exc}")
        return ids, dates

    for match in ARXIV_PATTERN.finditer(text):
        ids.append(
            {
                "arXiv ID": match.group(1),
                "source_file": str(filepath.relative_to(apod_root)),
            }
        )

    for match in DATE_HEADER_PATTERN.finditer(text):
        raw_date = match.group(1)
        try:
            date.fromisoformat(raw_date)
        except ValueError:
            print(f"  [WARN] 忽略非法日期标题: {filepath}: {raw_date}")
            continue
        dates.append(raw_date)

    return ids, dates


def main() -> None:
    apod_root = resolve_apod_root()
    md_files = sorted(apod_root.rglob("AstroPH-*.md"))
    print(f"找到 {len(md_files)} 个笔记文件")

    all_ids = []
    all_dates = []

    for f in md_files:
        ids, dates = extract_from_file(f, apod_root)
        if ids:
            print(f"  {f.relative_to(apod_root)}: {len(ids)} 篇论文, {len(dates)} 个日期")
        all_ids.extend(ids)
        all_dates.extend(dates)

    # 去重 ID（同一篇论文可能在多个地方被引用）
    seen = set()
    unique_ids = []
    for r in all_ids:
        if r["arXiv ID"] not in seen:
            seen.add(r["arXiv ID"])
            unique_ids.append(r)

    print(f"\n总计: {len(all_ids)} 条记录, 去重后 {len(unique_ids)} 个唯一 arXiv ID")

    # 确定日期范围
    if not all_dates:
        raise RuntimeError("未从笔记中提取到有效日期，拒绝使用过期的硬编码范围")
    date_start = min(all_dates)
    date_end = max(all_dates)

    print(f"日期范围: {date_start} ~ {date_end}")

    # 确保缓存目录存在
    CACHE_DIR.mkdir(exist_ok=True)

    # 保存正样本 ID CSV
    csv_buffer = io.StringIO(newline="")
    writer = csv.DictWriter(csv_buffer, fieldnames=["arXiv ID", "source_file"])
    writer.writeheader()
    writer.writerows(unique_ids)
    atomic_write_text(OUTPUT_IDS_CSV, csv_buffer.getvalue())
    print(f"已保存正样本 ID 到 {OUTPUT_IDS_CSV}")

    # 保存日期范围
    date_range = {"start": date_start, "end": date_end}
    atomic_write_text(
        OUTPUT_DATE_RANGE,
        json.dumps(date_range, ensure_ascii=False, indent=2),
    )
    print(f"已保存日期范围到 {OUTPUT_DATE_RANGE}")


if __name__ == "__main__":
    main()
