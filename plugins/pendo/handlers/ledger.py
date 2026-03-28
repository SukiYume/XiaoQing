"""
记账(Ledger)处理器
处理记账相关的所有操作，不需要AI解析
"""

from typing import Any, TYPE_CHECKING, cast
from datetime import datetime, timedelta
import re
import logging
from ..models.item import ItemType, LedgerItem
from ..models.constants import ItemFields
from ..core.types import PendoContext, CommandMessage
from ..core.exceptions import MissingRequiredFieldException
from core.plugin_base import run_sync
from ..utils.db_ops import DbOpsMixin
from ..utils.error_handlers import handle_command_errors
from ..utils.session_utils import safe_create_session
from ..utils.formatters import ItemFormatter, paginate
from ..utils.time_utils import _parse_time_range_core
from ..config import (
    PendoConfig,
    LEDGER_EXPENSE_CATEGORIES,
    LEDGER_INCOME_CATEGORIES,
)

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from ..services.db import Database


def _get_category_icon(name: str) -> str:
    """根据分类名获取图标"""
    for cat in LEDGER_EXPENSE_CATEGORIES + LEDGER_INCOME_CATEGORIES:
        if cat["name"] == name:
            return cat["icon"]
    return "📌"


def _direction_label(direction: str) -> str:
    return "收入" if direction == "income" else "支出"


def _direction_icon(direction: str) -> str:
    return "💰" if direction == "income" else "💸"


class LedgerHandler(DbOpsMixin):
    """记账处理器

    支持交互式记账(多轮对话)和快速记账：
    - add: 交互式多轮记账
    - quick: 单条命令快速记账
    - list: 查看账目列表
    - view: 查看账目详情
    - edit: 编辑账目
    - delete: 删除账目
    - summary: 收支汇总统计
    """

    def __init__(self, db: "Database"):
        self.db = db

    @handle_command_errors
    async def handle(
        self, user_id: str, args: str, context: PendoContext, group_id: int | None = None
    ) -> CommandMessage:
        """处理记账相关命令"""
        if not args or not args.strip():
            return {"status": "success", "message": self._show_usage()}

        parts = args.split(maxsplit=1)
        command = parts[0].lower()
        rest = parts[1] if len(parts) > 1 else ""

        handlers = {
            "add": lambda: self.start_add_session(user_id, context, group_id),
            "quick": lambda: self.quick_add(user_id, rest, context, group_id),
            "list": lambda: self.list_ledger(user_id, rest, context),
            "view": lambda: self.view_ledger(user_id, rest, context),
            "edit": lambda: self.edit_ledger(user_id, rest, context),
            "delete": lambda: self.delete_ledger(user_id, rest, context),
            "summary": lambda: self.summary(user_id, rest, context),
        }

        handler = handlers.get(command)
        if handler:
            return await handler()
        else:
            return {"status": "error", "message": f"❌ 未知命令: {command}\n\n{self._show_usage()}"}

    def _show_usage(self) -> str:
        return (
            "💰 记账命令\n"
            "管理账目、查看收支和做快速记录。\n\n"
            "可用命令:\n"
            "• /pendo ledger add - 交互式记账\n"
            "• /pendo ledger quick <金额> <描述> [cat:分类] [in] - 快速记账\n"
            "• /pendo ledger list [范围] [dir:in/out] [cat:分类] [amount:N或N..M] [ex] - 查看账目\n"
            "  范围: today/week/month/year/last7d/2026-03/start..end\n"
            "  ex: 额外显示各分类最大单笔\n"
            "• /pendo ledger view <id> - 查看详情\n"
            "• /pendo ledger edit <id> <字段:值> ... - 编辑\n"
            "  字段: amount: title: cat: dir: date: remark:\n"
            "• /pendo ledger delete <id> - 删除\n"
            "• /pendo ledger summary [范围] - 收支汇总"
        )

    # ============================================================
    # 交互式记账 (多轮对话)
    # ============================================================

    async def start_add_session(
        self, user_id: str, context: PendoContext, group_id: int | None = None
    ) -> CommandMessage:
        """开始交互式记账会话"""
        await safe_create_session(
            context,
            initial_data={
                "type": PendoConfig.SESSION_TYPE_LEDGER_ADD,
                "owner_id": user_id,
                "group_id": group_id,
                "step": "direction",
                "data": {},
            },
            timeout=PendoConfig.SESSION_TIMEOUT_SECONDS,
        )

        return {
            "status": "success",
            "message": (
                "📝 开始记账，请选择类型：\n\n"
                "1️⃣ 支出\n"
                "2️⃣ 收入\n\n"
                "(输入 1 或 2，或直接输入\"支出\"/\"收入\")\n"
                "💡 输入\"退出\"可取消"
            ),
        }

    async def handle_session_step(
        self, user_id: str, text: str, session: dict[str, Any], context: PendoContext
    ) -> CommandMessage:
        """处理记账会话的每一步"""
        step = session.get("step", "direction")
        data = session.get("data", {})
        group_id = session.get("group_id")

        text = text.strip()

        if step == "direction":
            return await self._step_direction(text, data, session, context)
        elif step == "amount":
            return await self._step_amount(text, data, session, context)
        elif step == "category":
            return await self._step_category(text, data, session, context)
        elif step == "description":
            return await self._step_description(user_id, text, data, session, context, group_id)
        else:
            return {"status": "error", "message": "❌ 会话状态异常"}

    async def _step_direction(
        self, text: str, data: dict, session: dict, context: PendoContext
    ) -> CommandMessage:
        """步骤1: 选择收入/支出"""
        if text in ("1", "支出", "expense"):
            data["direction"] = "expense"
        elif text in ("2", "收入", "income"):
            data["direction"] = "income"
        else:
            return {"status": "info", "message": "请输入 1(支出) 或 2(收入)"}

        session.set("data", data)
        session.set("step", "amount")

        return {"status": "success", "message": "💰 请输入金额（数字）："}

    async def _step_amount(
        self, text: str, data: dict, session: dict, context: PendoContext
    ) -> CommandMessage:
        """步骤2: 输入金额"""
        try:
            amount = float(text.replace("￥", "").replace("¥", "").replace(",", "").strip())
            if amount <= 0:
                return {"status": "info", "message": "❌ 金额必须大于0，请重新输入："}
        except ValueError:
            return {"status": "info", "message": "❌ 请输入有效的数字金额："}

        data["amount"] = amount
        session.set("data", data)
        session.set("step", "category")

        # 根据收支方向显示不同的分类
        categories = (
            LEDGER_EXPENSE_CATEGORIES if data["direction"] == "expense" else LEDGER_INCOME_CATEGORIES
        )

        lines = ["📂 请选择分类：\n"]
        for i, cat in enumerate(categories, 1):
            lines.append(f"{i}.{cat['icon']}{cat['name']}")
            if i % 4 == 0:
                lines.append("")  # 每4个换行

        lines.append("\n(输入编号或分类名)")
        return {"status": "success", "message": "\n".join(lines)}

    async def _step_category(
        self, text: str, data: dict, session: dict, context: PendoContext
    ) -> CommandMessage:
        """步骤3: 选择分类"""
        categories = (
            LEDGER_EXPENSE_CATEGORIES if data["direction"] == "expense" else LEDGER_INCOME_CATEGORIES
        )

        category_name = None

        # 尝试按编号匹配
        try:
            idx = int(text)
            if 1 <= idx <= len(categories):
                category_name = categories[idx - 1]["name"]
        except ValueError:
            pass

        # 尝试按名称匹配
        if not category_name:
            for cat in categories:
                if cat["name"] == text or cat["id"] == text.lower():
                    category_name = cat["name"]
                    break

        if not category_name:
            return {"status": "info", "message": "❌ 无效的分类，请输入编号或分类名："}

        data["ledger_category"] = category_name
        session.set("data", data)
        session.set("step", "description")

        return {"status": "success", "message": "📝 请输入描述（简要说明，如\"午饭\"）："}

    async def _step_description(
        self, user_id: str, text: str, data: dict, session: dict,
        context: PendoContext, group_id: int | None
    ) -> CommandMessage:
        """步骤4: 输入描述，然后保存"""
        if not text:
            return {"status": "info", "message": "❌ 描述不能为空，请输入："}

        data["title"] = text

        # 结束会话并保存
        from ..utils.session_utils import safe_end_session
        await safe_end_session(context)

        return await self._save_ledger_item(user_id, data, group_id)

    async def _save_ledger_item(
        self, user_id: str, data: dict, group_id: int | None = None
    ) -> CommandMessage:
        """保存记账条目"""
        now = datetime.now()
        ledger_date = data.get("ledger_date") or now.strftime("%Y-%m-%d")

        item = LedgerItem(
            owner_id=user_id,
            title=data.get("title", ""),
            content=data.get("remark", ""),
            amount=data.get("amount", 0.0),
            direction=data.get("direction", "expense"),
            ledger_category=data.get("ledger_category", "其他"),
            ledger_date=ledger_date,
            remark=data.get("remark", ""),
            context={"group_id": group_id} if group_id else {},
            created_at=now.isoformat(),
            updated_at=now.isoformat(),
        )

        item_id = await self._db_create_with_log(item, owner_id=user_id, action="create_ledger")

        amount = data.get("amount", 0.0)
        direction = data.get("direction", "expense")
        cat_icon = _get_category_icon(data.get("ledger_category", "其他"))

        message = (
            f"✅ 记账成功！\n\n"
            f"{_direction_icon(direction)} {_direction_label(direction)} ¥{amount:.2f}\n"
            f"{cat_icon} 分类：{data.get('ledger_category', '其他')}\n"
            f"📝 描述：{data.get('title', '')}\n"
            f"📅 日期：{ledger_date}\n"
            f"🔖 ID：`{item_id}`"
        )

        return {"status": "success", "message": message, "item_id": item_id}

    # ============================================================
    # 快速记账
    # ============================================================

    async def quick_add(
        self, user_id: str, text: str, context: PendoContext, group_id: int | None = None
    ) -> CommandMessage:
        """快速记账

        格式: /pendo ledger quick <金额> <描述> [cat:分类] [in]
        默认为支出，加 in 标记为收入
        """
        if not text:
            return {
                "status": "error",
                "message": "❌ 用法: /pendo ledger quick <金额> <描述> [cat:分类] [in]",
            }

        # 解析收支方向
        direction = "expense"
        if re.search(r"\bin\b", text):
            direction = "income"
            text = re.sub(r"\bin\b", "", text).strip()

        # 解析分类
        ledger_cat = "其他"
        cat_match = re.search(r"cat:(\S+)", text)
        if cat_match:
            ledger_cat = cat_match.group(1)
            text = text.replace(cat_match.group(0), "").strip()

        # 解析金额和描述
        parts = text.split(maxsplit=1)
        if not parts:
            return {"status": "error", "message": "❌ 请提供金额"}

        try:
            amount = float(parts[0].replace("￥", "").replace("¥", "").replace(",", ""))
            if amount <= 0:
                return {"status": "error", "message": "❌ 金额必须大于0"}
        except ValueError:
            return {"status": "error", "message": f"❌ 无法识别金额: {parts[0]}"}

        title = parts[1] if len(parts) > 1 else ""

        data = {
            "amount": amount,
            "direction": direction,
            "ledger_category": ledger_cat,
            "title": title,
        }

        return await self._save_ledger_item(user_id, data, group_id)

    # ============================================================
    # 列表
    # ============================================================

    async def list_ledger(
        self, user_id: str, filter_str: str, context: PendoContext
    ) -> CommandMessage:
        """查看账目列表

        格式:
        - /pendo ledger list                    -> 本月
        - /pendo ledger list today              -> 今天
        - /pendo ledger list week               -> 本周
        - /pendo ledger list year               -> 今年
        - /pendo ledger list 2026-03            -> 指定月份
        - /pendo ledger list 2026-03-01..2026-03-15 -> 范围

        过滤参数 (可组合使用):
        - dir:in/out 或 dir:income/expense      -> 按收支方向筛选
        - cat:分类名                             -> 按分类筛选
        - amount:N                              -> 金额 >= N
        - amount:N..M                           -> 金额在 N 到 M 之间

        其他:
        - ex       -> 额外显示每个分类的最大单笔
        - all      -> 显示全部（不分页）
        - page:N   -> 显示第N页
        """
        filter_str = (filter_str or "").strip()

        # 解析所有参数
        show_all = False
        page_num = 1
        show_extra = False
        dir_filter = None
        cat_filter = None
        amount_min = None
        amount_max = None

        filter_parts = filter_str.split()
        clean_parts = []
        for part in filter_parts:
            pl = part.lower()
            if pl == "all":
                show_all = True
            elif pl == "ex":
                show_extra = True
            elif part.startswith("page:"):
                try:
                    page_num = int(part.split(":")[1])
                except (IndexError, ValueError):
                    pass
            elif part.startswith("dir:"):
                val = part[4:].lower()
                dir_filter = "income" if val in ("in", "income", "收入") else "expense"
            elif part.startswith("cat:"):
                cat_filter = part[4:]
            elif part.startswith("amount:"):
                rng = part[7:]
                if ".." in rng:
                    lo, hi = rng.split("..", 1)
                    try:
                        amount_min = float(lo)
                        amount_max = float(hi)
                    except ValueError:
                        pass
                else:
                    try:
                        amount_min = float(rng)
                    except ValueError:
                        pass
            else:
                clean_parts.append(part)
        range_str = " ".join(clean_parts)

        start_date, end_date, range_label = self._parse_date_range(range_str)

        items = cast(
            list[LedgerItem],
            await run_sync(
                self.db.items.query_items_by_date_range,
                user_id,
                ItemType.LEDGER.value,
                "ledger_date",
                start_date,
                end_date,
            ),
        )

        # 应用额外过滤
        if dir_filter:
            items = [i for i in items if i.direction == dir_filter]
        if cat_filter:
            items = [i for i in items if i.ledger_category == cat_filter]
        if amount_min is not None:
            items = [i for i in items if i.amount >= amount_min]
        if amount_max is not None:
            items = [i for i in items if i.amount <= amount_max]

        # 构建过滤描述
        filter_labels = []
        if dir_filter:
            filter_labels.append("收入" if dir_filter == "income" else "支出")
        if cat_filter:
            filter_labels.append(f"分类:{cat_filter}")
        if amount_min is not None and amount_max is not None:
            filter_labels.append(f"¥{amount_min:.0f}~{amount_max:.0f}")
        elif amount_min is not None:
            filter_labels.append(f"≥¥{amount_min:.0f}")
        filter_suffix = f" [{', '.join(filter_labels)}]" if filter_labels else ""

        if not items:
            return {"status": "success", "message": f"💰 **{range_label}账目**{filter_suffix}\n\n暂无记录"}

        # 按日期降序排序
        items.sort(key=lambda x: x.ledger_date or "", reverse=True)

        # 分页
        page_size = PendoConfig.LIST_PAGE_SIZE
        display_items, page_info, has_more = paginate(items, page_num, page_size, show_all)

        # 汇总
        total_income = sum(i.amount for i in items if i.direction == "income")
        total_expense = sum(i.amount for i in items if i.direction == "expense")

        message = (
            f"💰 {range_label}账目{filter_suffix}\n"
            f"共 {len(items)} 笔{page_info}\n"
            f"💸 支出 ¥{total_expense:.2f} | 💰 收入 ¥{total_income:.2f}\n"
            "━━━━━━━━━━━━━━━━━━\n"
        )

        for item in display_items:
            icon = _direction_icon(item.direction)
            cat_icon = _get_category_icon(item.ledger_category)
            sign = "+" if item.direction == "income" else "-"
            date_str = item.ledger_date or ""
            message += f"• {icon} {sign}¥{item.amount:.2f}  {cat_icon}{item.ledger_category}"
            if item.title:
                message += f" {item.title}"
            message += f"\n  📅 {date_str} | ID `{item.id}`\n\n"

        if has_more and not show_all:
            message += f"... (使用 'all' 显示全部或 'page:{page_num + 1}' 查看下一页)\n"

        # ex 模式：各分类最大单笔
        if show_extra:
            max_by_cat: dict[str, LedgerItem] = {}
            for item in items:
                cat = item.ledger_category or "其他"
                if cat not in max_by_cat or item.amount > max_by_cat[cat].amount:
                    max_by_cat[cat] = item
            if max_by_cat:
                message += "\n📊 **各分类最大单笔**\n"
                sorted_cats = sorted(max_by_cat.items(), key=lambda x: x[1].amount, reverse=True)
                for cat, item in sorted_cats:
                    icon = _direction_icon(item.direction)
                    cat_icon = _get_category_icon(cat)
                    sign = "+" if item.direction == "income" else "-"
                    date_str = item.ledger_date or ""
                    title_part = f" {item.title}" if item.title else ""
                    message += f"  • {cat_icon}{cat}: {icon}{sign}¥{item.amount:.2f}{title_part} ({date_str})\n"

        return {"status": "success", "message": message}

    # ============================================================
    # 查看详情
    # ============================================================

    async def view_ledger(
        self, user_id: str, item_id: str, context: PendoContext
    ) -> CommandMessage:
        """查看单个账目详情"""
        if not item_id:
            raise MissingRequiredFieldException("id")

        item_id = item_id.strip()
        item, wrong_type = await self._db_get_typed_item_or_message(
            item_id, user_id, ItemType.LEDGER.value, "账目"
        )
        if wrong_type:
            return wrong_type
        item = cast(LedgerItem, item)

        icon = _direction_icon(item.direction)
        cat_icon = _get_category_icon(item.ledger_category)

        message = (
            f"💰 **账目详情**\n\n"
            f"{icon} {_direction_label(item.direction)} ¥{item.amount:.2f}\n"
            f"{cat_icon} 分类：{item.ledger_category}\n"
            f"📝 描述：{item.title or '无'}\n"
            f"📅 日期：{item.ledger_date or '未知'}\n"
            f"🔖 ID：`{item.id}`\n"
            f"⏰ 创建：{ItemFormatter.format_datetime(item.created_at)}"
        )

        if item.remark:
            message += f"\n💬 备注：{item.remark}"

        return {"status": "success", "message": message}

    # ============================================================
    # 编辑
    # ============================================================

    async def edit_ledger(
        self, user_id: str, args: str, context: PendoContext
    ) -> CommandMessage:
        """编辑账目

        格式: /pendo ledger edit <id> [amount:金额] [title:描述] [cat:分类] [dir:in/out] [date:日期] [remark:备注]
        所有字段均通过 key:value 显式指定
        """
        parts = args.split(maxsplit=1)
        if len(parts) < 2:
            return {
                "status": "error",
                "message": (
                    "❌ 用法: /pendo ledger edit <id> <字段:值> ...\n\n"
                    "可修改字段：\n"
                    "• amount:金额 - 修改金额\n"
                    "• title:描述 - 修改描述\n"
                    "• cat:分类 - 修改分类\n"
                    "• dir:in/out - 修改收支方向\n"
                    "• date:YYYY-MM-DD - 修改日期\n"
                    "• remark:备注 - 修改备注\n\n"
                    "示例: /pendo ledger edit abc123 amount:50 cat:交通"
                ),
            }

        item_id = parts[0].strip()
        edit_str = parts[1]

        item, wrong_type = await self._db_get_typed_item_or_message(
            item_id, user_id, ItemType.LEDGER.value, "账目"
        )
        if wrong_type:
            return wrong_type
        item = cast(LedgerItem, item)

        updates: dict[str, Any] = {"type": ItemType.LEDGER.value}
        field_labels: list[str] = []

        # 定义字段解析规则: (regex_pattern, db_field, label, validator_or_None)
        field_parsers: list[tuple[str, str, str, Any]] = [
            (r"amount:(\S+)", "amount", "金额", "_parse_amount"),
            (r"title:(\S+)", "title", "描述", None),
            (r"cat:(\S+)", "ledger_category", "分类", None),
            (r"dir:(in|out|income|expense)", "direction", "方向", "_parse_direction"),
            (r"date:(\d{4}-\d{2}-\d{2})", "ledger_date", "日期", None),
            (r"remark:(\S+)", "remark", "备注", None),
        ]

        for pattern, db_field, label, validator in field_parsers:
            match = re.search(pattern, edit_str)
            if match:
                value = match.group(1)
                if validator == "_parse_amount":
                    try:
                        amount = float(value.replace("￥", "").replace("¥", "").replace(",", ""))
                        if amount <= 0:
                            return {"status": "error", "message": "❌ 金额必须大于0"}
                        updates[db_field] = amount
                        field_labels.append(f"{label} → ¥{amount:.2f}")
                    except ValueError:
                        return {"status": "error", "message": f"❌ 无效金额: {value}"}
                elif validator == "_parse_direction":
                    direction = "income" if value in ("in", "income") else "expense"
                    updates[db_field] = direction
                    field_labels.append(f"{label} → {_direction_label(direction)}")
                else:
                    updates[db_field] = value
                    field_labels.append(f"{label} → {value}")

        if len(updates) <= 1:  # only "type" key
            return {
                "status": "error",
                "message": (
                    "❌ 未识别到有效字段\n\n"
                    "可用字段: amount: title: cat: dir: date: remark:\n"
                    "示例: /pendo ledger edit abc123 amount:50 cat:交通"
                ),
            }

        await self._db_update_with_log(item_id, updates, user_id, action="edit_ledger")

        changes = "\n".join(f"  • {fl}" for fl in field_labels)
        return {
            "status": "success",
            "message": f"✅ 已更新账目 `{item_id}`\n\n{changes}\n\n💡 /pendo undo 可撤销编辑",
        }

    # ============================================================
    # 删除
    # ============================================================

    async def delete_ledger(
        self, user_id: str, item_id: str, context: PendoContext
    ) -> CommandMessage:
        """删除账目"""
        if not item_id:
            raise MissingRequiredFieldException("id")

        item_id = item_id.strip()
        item, wrong_type = await self._db_get_typed_item_or_message(
            item_id, user_id, ItemType.LEDGER.value, "账目"
        )
        if wrong_type:
            return wrong_type
        item = cast(LedgerItem, item)

        await self._db_soft_delete_with_log(item_id, user_id, item_type=ItemType.LEDGER.value)

        return {
            "status": "success",
            "message": (
                f"🗑️ 已删除: {_direction_label(item.direction)} ¥{item.amount:.2f} {item.title or ''}\n\n"
                f"💡 5分钟内可用 /pendo undo 撤销"
            ),
        }

    # ============================================================
    # 收支汇总
    # ============================================================

    async def summary(
        self, user_id: str, range_str: str, context: PendoContext
    ) -> CommandMessage:
        """收支汇总统计"""
        range_str = (range_str or "").strip()
        start_date, end_date, range_label = self._parse_date_range(range_str)

        items = cast(
            list[LedgerItem],
            await run_sync(
                self.db.items.query_items_by_date_range,
                user_id,
                ItemType.LEDGER.value,
                "ledger_date",
                start_date,
                end_date,
            ),
        )

        if not items:
            return {"status": "success", "message": f"📊 **{range_label}收支汇总**\n\n暂无记录"}

        # 计算总额
        total_income = sum(i.amount for i in items if i.direction == "income")
        total_expense = sum(i.amount for i in items if i.direction == "expense")
        balance = total_income - total_expense

        # 按分类统计支出
        expense_by_cat: dict[str, float] = {}
        for i in items:
            if i.direction == "expense":
                cat = i.ledger_category or "其他"
                expense_by_cat[cat] = expense_by_cat.get(cat, 0.0) + i.amount

        # 按分类统计收入
        income_by_cat: dict[str, float] = {}
        for i in items:
            if i.direction == "income":
                cat = i.ledger_category or "其他"
                income_by_cat[cat] = income_by_cat.get(cat, 0.0) + i.amount

        # 格式化
        balance_sign = "+" if balance >= 0 else ""
        message = (
            f"📊 {range_label}收支汇总\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"💸 总支出: ¥{total_expense:.2f}\n"
            f"💰 总收入: ¥{total_income:.2f}\n"
            f"📊 结余: {balance_sign}¥{balance:.2f}\n"
        )

        if expense_by_cat:
            top_expense_cat, top_expense_amount = max(expense_by_cat.items(), key=lambda x: x[1])
            message += f"📂 最大支出分类: {top_expense_cat} ¥{top_expense_amount:.2f}\n"
        if income_by_cat:
            top_income_cat, top_income_amount = max(income_by_cat.items(), key=lambda x: x[1])
            message += f"📥 主要收入来源: {top_income_cat} ¥{top_income_amount:.2f}\n"

        message += "━━━━━━━━━━━━━━━━━━\n"

        if expense_by_cat:
            sorted_cats = sorted(expense_by_cat.items(), key=lambda x: x[1], reverse=True)
            message += "📂 支出分类\n"
            for i, (cat, amount) in enumerate(sorted_cats, 1):
                pct = (amount / total_expense * 100) if total_expense > 0 else 0
                icon = _get_category_icon(cat)
                message += f"  {i}. {icon} {cat}  ¥{amount:.2f} ({pct:.1f}%)\n"

        if income_by_cat:
            sorted_cats = sorted(income_by_cat.items(), key=lambda x: x[1], reverse=True)
            message += "\n📂 收入分类\n"
            for i, (cat, amount) in enumerate(sorted_cats, 1):
                pct = (amount / total_income * 100) if total_income > 0 else 0
                icon = _get_category_icon(cat)
                message += f"  {i}. {icon} {cat}  ¥{amount:.2f} ({pct:.1f}%)\n"

        return {"status": "success", "message": message}

    # ============================================================
    # 工具方法
    # ============================================================

    def _parse_date_range(self, range_str: str) -> tuple[str, str, str]:
        """解析日期范围，返回 (start_date, end_date, label)。

        委托 _parse_time_range_core 做实际计算，仅负责：
        1. 关键字语义对齐（week→本周、month→本月，保证日历周/月而非滚动区间）
        2. 生成人类可读标签
        """
        now = datetime.now()
        rs = (range_str or "").strip()
        rl = rs.lower()

        # 生成标签
        if not rs or rl in ("month", "本月"):
            label = f"{now.year}年{now.month}月"
        elif rl in ("today", "今天"):
            label = "今日"
        elif rl in ("week", "本周"):
            label = "本周"
        elif rl in ("year", "今年"):
            label = f"{now.year}年全年"
        elif m := re.match(r"last(\d+)d", rl):
            label = f"最近{m.group(1)}天"
        elif re.fullmatch(r"\d{4}", rs):
            label = f"{rs}年"
        elif m2 := re.fullmatch(r"(\d{4})-(\d{2})", rs):
            label = f"{m2.group(1)}年{int(m2.group(2))}月"
        elif ".." in rs:
            s, e = rs.split("..", 1)
            label = f"{s} ~ {e}"
        elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", rs):
            label = rs
        else:
            label = f"{now.year}年{now.month}月"

        # 语义对齐：week/month 使用日历含义（本周/本月）而非滚动区间
        normalized = {"week": "本周", "month": "本月"}.get(rl, rs or "本月")

        start_dt, end_dt = _parse_time_range_core(normalized, now)
        return start_dt.strftime("%Y-%m-%d"), end_dt.strftime("%Y-%m-%d"), label
