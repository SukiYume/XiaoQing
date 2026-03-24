"""
一次性脚本：将 plugins/pendo/data/old_bill.csv 导入 pendo.db 的 items 表（type=ledger）

CSV 格式：事件,收支,标签,时间,记录人,渠道,月份
字段映射：
  事件   -> title
  收支   -> amount (取绝对值) + direction (负=expense, 正=income)
  标签   -> ledger_category (按映射表转换)
  时间   -> ledger_date (YYYY/MM/DD -> YYYY-MM-DD)
  渠道   -> payment_method (CSV中全为空，留空字符串)
  记录人 -> 忽略
  月份   -> 忽略

标签映射规则：
  吃饭       -> 餐饮
  出行       -> 交通
  房租       -> 居住
  购物       -> 购物
  娱乐       -> 娱乐
  服务       -> 美容  (理发、洗牙等生活服务)
  工资       -> 工资
  利息       -> 理财
  其他       -> 其他
  周转       -> 其他
  信用卡还款 -> 跳过（不导入）

用法：
  python scripts/import_old_bill.py [--dry-run] [--owner-id OWNER_ID]
"""

import csv
import sqlite3
import uuid
import argparse
import os
from datetime import datetime

# 路径
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
CSV_PATH = os.path.join(PROJECT_ROOT, "plugins", "pendo", "data", "old_bill.csv")
DB_PATH = os.path.join(PROJECT_ROOT, "plugins", "pendo", "data", "pendo.db")

DEFAULT_OWNER_ID = "1000000001"

# 标签 -> ledger_category 映射
TAG_MAP = {
    "吃饭": "餐饮",
    "出行": "交通",
    "房租": "居住",
    "购物": "购物",
    "娱乐": "娱乐",
    "服务": "美容",
    "工资": "工资",
    "利息": "理财",
    "其他": "其他",
    "周转": "其他",
}

# 需要跳过的标签
SKIP_TAGS = {"信用卡还款"}


def parse_date(date_str: str) -> str:
    """将 YYYY/MM/DD 转换为 YYYY-MM-DD"""
    return date_str.replace("/", "-")


def generate_id() -> str:
    return str(uuid.uuid4())[:8]


def import_csv(dry_run: bool = False, owner_id: str = DEFAULT_OWNER_ID):
    print(f"CSV 文件: {CSV_PATH}")
    print(f"数据库:   {DB_PATH}")
    print(f"Owner ID: {owner_id}")
    print(f"模式:     {'预览(dry-run)' if dry_run else '正式导入'}")
    print()

    if not os.path.exists(CSV_PATH):
        print(f"❌ CSV 文件不存在: {CSV_PATH}")
        return

    rows_to_insert = []
    skipped = 0
    errors = []
    now = datetime.now().isoformat()

    with open(CSV_PATH, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)

        for line_num, row in enumerate(reader, start=2):
            tag = (row.get("标签") or "").strip()

            # 跳过信用卡还款
            if tag in SKIP_TAGS:
                skipped += 1
                continue

            # 解析金额和方向
            amount_str = (row.get("收支") or "").strip()
            try:
                amount_raw = float(amount_str)
            except ValueError:
                errors.append(f"行 {line_num}: 无法解析金额 '{amount_str}'")
                continue

            direction = "income" if amount_raw > 0 else "expense"
            amount = abs(amount_raw)

            # 映射分类
            ledger_category = TAG_MAP.get(tag)
            if ledger_category is None:
                errors.append(f"行 {line_num}: 未知标签 '{tag}'，归为'其他'")
                ledger_category = "其他"

            # 解析日期
            date_str = (row.get("时间") or "").strip()
            if not date_str:
                errors.append(f"行 {line_num}: 缺少日期，跳过")
                continue
            ledger_date = parse_date(date_str)

            # 标题
            title = (row.get("事件") or "").strip()

            item_id = generate_id()

            rows_to_insert.append((
                item_id,
                "ledger",           # type
                title,              # title
                "",                 # content
                "[]",               # tags (JSON)
                "未分类",           # category (基类字段，不用)
                now,                # created_at
                now,                # updated_at
                owner_id,           # owner_id
                "{}",               # context (JSON)
                "private",          # visibility
                "[]",               # attachments (JSON)
                "{}",               # ai_meta (JSON)
                0,                  # deleted
                None,               # deleted_at
                amount,             # amount
                direction,          # direction
                ledger_category,    # ledger_category
                "",                 # payment_method (CSV中无数据)
                ledger_date,        # ledger_date
                "",                 # remark
            ))

    # 统计
    print(f"解析完成:")
    print(f"  待导入: {len(rows_to_insert)} 条")
    print(f"  跳过(信用卡还款): {skipped} 条")
    if errors:
        print(f"  警告: {len(errors)} 条")
        for e in errors[:10]:
            print(f"    ⚠️ {e}")
        if len(errors) > 10:
            print(f"    ... 还有 {len(errors) - 10} 条警告")
    print()

    # 预览前 5 条
    print("前 5 条预览:")
    for r in rows_to_insert[:5]:
        d_icon = "💰" if r[16] == "income" else "💸"
        print(f"  {d_icon} ¥{r[15]:.2f} [{r[17]}] {r[2]} ({r[19]})")
    print()

    if dry_run:
        print("(dry-run 模式，未写入数据库)")
        return

    # 写入数据库
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    sql = """
    INSERT INTO items (
        id, type, title, content, tags, category,
        created_at, updated_at, owner_id, context, visibility,
        attachments, ai_meta, deleted, deleted_at,
        amount, direction, ledger_category, payment_method, ledger_date, remark
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """

    try:
        cursor.executemany(sql, rows_to_insert)
        conn.commit()
        print(f"✅ 成功导入 {cursor.rowcount} 条记账记录到 pendo.db")
    except Exception as e:
        conn.rollback()
        print(f"❌ 导入失败: {e}")
    finally:
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="导入旧账单 CSV 到 pendo.db")
    parser.add_argument("--dry-run", action="store_true", help="预览模式，不实际写入")
    parser.add_argument("--owner-id", default=DEFAULT_OWNER_ID, help=f"用户ID (默认: {DEFAULT_OWNER_ID})")
    args = parser.parse_args()

    import_csv(dry_run=args.dry_run, owner_id=args.owner_id)
