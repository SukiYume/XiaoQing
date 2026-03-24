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
from ..config import (
    PendoConfig,
    LEDGER_EXPENSE_CATEGORIES,
    LEDGER_INCOME_CATEGORIES,
    LEDGER_PAYMENT_METHODS,
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
            "💰 **记账帮助**\n\n"
            "• /pendo ledger add - 交互式记账\n"
            "• /pendo ledger quick <金额> <描述> [cat:分类] [pay:方式] [in] - 快速记账\n"
            "• /pendo ledger list [范围] - 查看账目\n"
            "• /pendo ledger view <id> - 查看详情\n"
            "• /pendo ledger edit <id> <字段:值> - 编辑\n"
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
            return await self._step_description(text, data, session, context)
        elif step == "payment":
            return await self._step_payment(user_id, text, data, session, context, group_id)
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

        session["data"] = data
        session["step"] = "amount"
        await safe_create_session(context, initial_data=session, timeout=PendoConfig.SESSION_TIMEOUT_SECONDS)

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
        session["data"] = data
        session["step"] = "category"
        await safe_create_session(context, initial_data=session, timeout=PendoConfig.SESSION_TIMEOUT_SECONDS)

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
        session["data"] = data
        session["step"] = "description"
        await safe_create_session(context, initial_data=session, timeout=PendoConfig.SESSION_TIMEOUT_SECONDS)

        return {"status": "success", "message": "📝 请输入描述（简要说明，如\"午饭\"）："}

    async def _step_description(
        self, text: str, data: dict, session: dict, context: PendoContext
    ) -> CommandMessage:
        """步骤4: 输入描述"""
        if not text:
            return {"status": "info", "message": "❌ 描述不能为空，请输入："}

        data["title"] = text
        session["data"] = data
        session["step"] = "payment"
        await safe_create_session(context, initial_data=session, timeout=PendoConfig.SESSION_TIMEOUT_SECONDS)

        lines = ["💳 请选择支付方式：\n"]
        for i, pm in enumerate(LEDGER_PAYMENT_METHODS, 1):
            lines.append(f"{i}.{pm['name']}")

        lines.append("\n(输入编号或名称，直接回车默认微信)")
        return {"status": "success", "message": "  ".join(lines)}

    async def _step_payment(
        self, user_id: str, text: str, data: dict, session: dict,
        context: PendoContext, group_id: int | None
    ) -> CommandMessage:
        """步骤5: 选择支付方式，然后保存"""
        payment = "微信"  # 默认

        if text:
            # 尝试按编号匹配
            try:
                idx = int(text)
                if 1 <= idx <= len(LEDGER_PAYMENT_METHODS):
                    payment = LEDGER_PAYMENT_METHODS[idx - 1]["name"]
            except ValueError:
                # 按名称匹配
                for pm in LEDGER_PAYMENT_METHODS:
                    if pm["name"] == text or pm["id"] == text.lower():
                        payment = pm["name"]
                        break

        data["payment_method"] = payment

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
            payment_method=data.get("payment_method", "微信"),
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
            f"💳 支付：{data.get('payment_method', '微信')}\n"
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

        格式: /pendo ledger quick <金额> <描述> [cat:分类] [pay:方式] [in]
        默认为支出，加 in 标记为收入
        """
        if not text:
            return {
                "status": "error",
                "message": "❌ 用法: /pendo ledger quick <金额> <描述> [cat:分类] [pay:方式] [in]",
            }

        # 解析收支方向
        direction = "expense"
        if re.search(r"\bin\b", text):
            direction = "income"
            text = re.sub(r"\bin\b", "", text).strip()

        # 解析支付方式
        payment = "微信"
        pay_match = re.search(r"pay:(\S+)", text)
        if pay_match:
            payment = pay_match.group(1)
            text = text.replace(pay_match.group(0), "").strip()

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
            "payment_method": payment,
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
        - /pendo ledger list          -> 本月
        - /pendo ledger list today    -> 今天
        - /pendo ledger list week     -> 本周
        - /pendo ledger list 2026-03  -> 指定月份
        - /pendo ledger list 2026-03-01..2026-03-15 -> 范围
        """
        filter_str = (filter_str or "").strip()

        # 解析分页
        show_all = False
        page_num = 1
        filter_parts = filter_str.split()
        clean_parts = []
        for part in filter_parts:
            if part.lower() == "all":
                show_all = True
            elif part.startswith("page:"):
                try:
                    page_num = int(part.split(":")[1])
                except (IndexError, ValueError):
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

        if not items:
            return {"status": "success", "message": f"💰 **{range_label}账目**\n\n暂无记录"}

        # 按日期降序排序
        items.sort(key=lambda x: x.ledger_date or "", reverse=True)

        # 分页
        page_size = PendoConfig.LIST_PAGE_SIZE
        display_items, page_info, has_more = paginate(items, page_num, page_size, show_all)

        # 汇总
        total_income = sum(i.amount for i in items if i.direction == "income")
        total_expense = sum(i.amount for i in items if i.direction == "expense")

        message = f"💰 **{range_label}账目** (共{len(items)}笔){page_info}\n"
        message += f"💸 支出 ¥{total_expense:.2f} | 💰 收入 ¥{total_income:.2f}\n\n"

        for item in display_items:
            icon = _direction_icon(item.direction)
            cat_icon = _get_category_icon(item.ledger_category)
            sign = "+" if item.direction == "income" else "-"
            date_str = item.ledger_date or ""
            message += f"{icon} {sign}¥{item.amount:.2f} {cat_icon}{item.ledger_category}"
            if item.title:
                message += f" {item.title}"
            message += f"\n   📅{date_str} 💳{item.payment_method} `{item.id}`\n\n"

        if has_more and not show_all:
            message += f"... (使用 'all' 显示全部或 'page:{page_num + 1}' 查看下一页)\n"

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

        item = cast(LedgerItem, await self._db_get_and_check(item_id.strip(), user_id))

        icon = _direction_icon(item.direction)
        cat_icon = _get_category_icon(item.ledger_category)

        message = (
            f"💰 **账目详情**\n\n"
            f"{icon} {_direction_label(item.direction)} ¥{item.amount:.2f}\n"
            f"{cat_icon} 分类：{item.ledger_category}\n"
            f"📝 描述：{item.title or '无'}\n"
            f"💳 支付：{item.payment_method}\n"
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

        格式: /pendo ledger edit <id> <金额|描述|cat:分类|pay:方式|in/out>
        """
        parts = args.split(maxsplit=1)
        if len(parts) < 2:
            return {"status": "error", "message": "❌ 用法: /pendo ledger edit <id> <新内容>"}

        item_id = parts[0].strip()
        edit_str = parts[1]

        item = cast(LedgerItem, await self._db_get_and_check(item_id, user_id))

        updates: dict[str, Any] = {"type": ItemType.LEDGER.value}

        # 解析编辑内容
        if re.search(r"\bin\b", edit_str):
            updates["direction"] = "income"
            edit_str = re.sub(r"\bin\b", "", edit_str).strip()
        elif re.search(r"\bout\b", edit_str):
            updates["direction"] = "expense"
            edit_str = re.sub(r"\bout\b", "", edit_str).strip()

        pay_match = re.search(r"pay:(\S+)", edit_str)
        if pay_match:
            updates["payment_method"] = pay_match.group(1)
            edit_str = edit_str.replace(pay_match.group(0), "").strip()

        cat_match = re.search(r"cat:(\S+)", edit_str)
        if cat_match:
            updates["ledger_category"] = cat_match.group(1)
            edit_str = edit_str.replace(cat_match.group(0), "").strip()

        # 剩余内容：尝试解析为金额，否则当描述
        if edit_str:
            try:
                amount = float(edit_str.replace("￥", "").replace("¥", "").replace(",", ""))
                if amount > 0:
                    updates["amount"] = amount
            except ValueError:
                updates["title"] = edit_str

        await self._db_update_with_log(item_id, updates, user_id, action="edit_ledger")

        return {
            "status": "success",
            "message": f"✅ 已更新账目 `{item_id}`\n\n💡 /pendo undo 可撤销编辑",
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
        item = cast(LedgerItem, await self._db_get_and_check(item_id, user_id))

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
            f"📊 **{range_label}收支汇总**\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"💸 总支出：¥{total_expense:.2f}\n"
            f"💰 总收入：¥{total_income:.2f}\n"
            f"📊 结余：  {balance_sign}¥{balance:.2f}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
        )

        if expense_by_cat:
            sorted_cats = sorted(expense_by_cat.items(), key=lambda x: x[1], reverse=True)
            message += "📂 **支出分类：**\n"
            for i, (cat, amount) in enumerate(sorted_cats, 1):
                pct = (amount / total_expense * 100) if total_expense > 0 else 0
                icon = _get_category_icon(cat)
                message += f"  {i}. {icon} {cat}  ¥{amount:.2f} ({pct:.1f}%)\n"

        if income_by_cat:
            sorted_cats = sorted(income_by_cat.items(), key=lambda x: x[1], reverse=True)
            message += "\n📂 **收入分类：**\n"
            for i, (cat, amount) in enumerate(sorted_cats, 1):
                pct = (amount / total_income * 100) if total_income > 0 else 0
                icon = _get_category_icon(cat)
                message += f"  {i}. {icon} {cat}  ¥{amount:.2f} ({pct:.1f}%)\n"

        return {"status": "success", "message": message}

    # ============================================================
    # 工具方法
    # ============================================================

    def _parse_date_range(self, range_str: str) -> tuple[str, str, str]:
        """解析日期范围，返回 (start_date, end_date, label)"""
        now = datetime.now()

        if not range_str:
            # 默认本月
            start = now.replace(day=1).strftime("%Y-%m-%d")
            if now.month == 12:
                end = now.replace(year=now.year + 1, month=1, day=1).strftime("%Y-%m-%d")
            else:
                end = now.replace(month=now.month + 1, day=1).strftime("%Y-%m-%d")
            return start, end, f"{now.year}年{now.month}月"

        range_lower = range_str.lower()

        if range_lower == "today":
            date_str = now.strftime("%Y-%m-%d")
            next_day = (now + timedelta(days=1)).strftime("%Y-%m-%d")
            return date_str, next_day, "今日"

        if range_lower == "week":
            weekday = now.weekday()
            start = (now - timedelta(days=weekday)).strftime("%Y-%m-%d")
            end = (now + timedelta(days=7 - weekday)).strftime("%Y-%m-%d")
            return start, end, "本周"

        if range_lower.startswith("last"):
            match = re.match(r"last(\d+)d", range_lower)
            if match:
                days = int(match.group(1))
                start = (now - timedelta(days=days)).strftime("%Y-%m-%d")
                end = (now + timedelta(days=1)).strftime("%Y-%m-%d")
                return start, end, f"最近{days}天"

        # YYYY-MM 格式
        month_match = re.match(r"(\d{4})-(\d{2})$", range_str)
        if month_match:
            year, month = int(month_match.group(1)), int(month_match.group(2))
            start = f"{year}-{month:02d}-01"
            if month == 12:
                end = f"{year + 1}-01-01"
            else:
                end = f"{year}-{month + 1:02d}-01"
            return start, end, f"{year}年{month}月"

        # start..end 格式
        if ".." in range_str:
            parts = range_str.split("..")
            if len(parts) == 2:
                return parts[0], parts[1], f"{parts[0]} ~ {parts[1]}"

        # 单独日期
        if re.match(r"\d{4}-\d{2}-\d{2}$", range_str):
            next_day = (datetime.strptime(range_str, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
            return range_str, next_day, range_str

        # 无法解析，默认本月
        start = now.replace(day=1).strftime("%Y-%m-%d")
        if now.month == 12:
            end = now.replace(year=now.year + 1, month=1, day=1).strftime("%Y-%m-%d")
        else:
            end = now.replace(month=now.month + 1, day=1).strftime("%Y-%m-%d")
        return start, end, f"{now.year}年{now.month}月"
