"""处理交互式记账、账目查询、编辑与汇总。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final, cast

from core.plugin_base import run_sync

from ..config import (
    LEDGER_EXPENSE_CATEGORIES,
    LEDGER_INCOME_CATEGORIES,
    PendoConfig,
)
from ..core.exceptions import MissingRequiredFieldException
from ..core.types import CommandMessage, PendoContext, SessionData
from ..models.item import ItemType, LedgerItem
from ..utils.db_ops import DbOpsMixin
from ..utils.error_handlers import handle_command_errors
from ..utils.formatters import ItemFormatter, paginate
from ..utils.session_utils import safe_create_session, safe_end_session
from ..utils.time_utils import (
    TimezoneHelper,
    _parse_time_range_core,
    get_user_local_wall_time,
)
from ..utils.validators import (
    LEDGER_TRANSACTION_TYPES,
    ledger_amount_to_cents,
    ledger_cents_to_amount,
    normalize_ledger_fields,
    parse_ledger_transaction_type,
)

if TYPE_CHECKING:
    from ..services.db import Database


_CATEGORY_ICONS: Final = {
    str(category["name"]): str(category["icon"])
    for category in LEDGER_EXPENSE_CATEGORIES + LEDGER_INCOME_CATEGORIES
}
_TRANSACTION_TYPE_LABELS: Final = {
    "income": "收入",
    "expense": "支出",
    "transfer": "转账",
}
_TRANSACTION_TYPE_ICONS: Final = {
    "income": "💰",
    "expense": "💸",
    "transfer": "🔁",
}
_COMMON_LEDGER_ACCOUNTS: Final = ("现金", "微信", "支付宝", "银行卡", "信用卡")
_FIELD_RE: Final = re.compile(r'(?<!\S)(?P<key>[A-Za-z_]{2,}):(?!//)(?P<value>"[^"]*"|“[^”]*”|\S+)')
_QUICK_LEDGER_FIELDS: Final = frozenset(
    {
        "type",
        "cat",
        "category",
        "date",
        "account",
        "from",
        "to",
        "counter",
        "merchant",
        "payee",
        "currency",
        "remark",
    }
)
_LIST_LEDGER_FIELDS: Final = frozenset(
    {
        "page",
        "type",
        "account",
        "to",
        "merchant",
        "cat",
        "amount",
    }
)
_EDIT_LEDGER_FIELD_MAP: Final = {
    "amount": "amount",
    "title": "title",
    "cat": "ledger_category",
    "category": "ledger_category",
    "date": "ledger_date",
    "account": "account_name",
    "from": "account_name",
    "to": "counter_account_name",
    "counter": "counter_account_name",
    "merchant": "merchant",
    "payee": "merchant",
    "remark": "remark",
    "currency": "currency",
}
_EDIT_LEDGER_FIELDS: Final = frozenset(_EDIT_LEDGER_FIELD_MAP) | {"type"}
_NONEMPTY_EDIT_FIELDS: Final = frozenset(
    {"amount", "cat", "category", "date", "account", "from", "currency", "type"}
)
_EDIT_LEDGER_LABELS: Final = {
    "title": "摘要",
    "ledger_category": "分类",
    "ledger_date": "日期",
    "account_name": "账户",
    "counter_account_name": "转入",
    "merchant": "商户",
    "remark": "备注",
    "currency": "币种",
}
_SESSION_REQUIRED_FIELDS: Final = {
    "amount": frozenset(),
    "description": frozenset({"amount"}),
    "transaction_type": frozenset({"amount", "title"}),
    "account": frozenset({"amount", "title", "transaction_type"}),
    "counter_account": frozenset({"amount", "title", "transaction_type", "account_name"}),
    "category": frozenset({"amount", "title", "transaction_type", "account_name"}),
    "merchant": frozenset(
        {"amount", "title", "transaction_type", "account_name", "ledger_category"}
    ),
}


@dataclass(frozen=True, slots=True)
class LedgerListFilters:
    """命令行账目列表筛选条件。金额统一保存为分。"""

    range_text: str = ""
    show_all: bool = False
    show_extra: bool = False
    page: int = 1
    transaction_type: str | None = None
    category: str | None = None
    account: str | None = None
    counter_account: str | None = None
    merchant: str | None = None
    amount_min_cents: int | None = None
    amount_max_cents: int | None = None


@dataclass(slots=True)
class LedgerTotals:
    """账目汇总；所有计算使用整数分，避免浮点累计误差。"""

    income_cents: int = 0
    expense_cents: int = 0
    transfer_cents: int = 0
    income_by_category: dict[str, int] = field(default_factory=dict)
    expense_by_category: dict[str, int] = field(default_factory=dict)


def _get_category_icon(name: str) -> str:
    return _CATEGORY_ICONS.get(name, "📌")


def _transaction_type_label(transaction_type: str) -> str:
    return _TRANSACTION_TYPE_LABELS.get(transaction_type, "支出")


def _transaction_type_icon(transaction_type: str) -> str:
    return _TRANSACTION_TYPE_ICONS.get(transaction_type, "💸")


def _extract_field_args(text: str) -> tuple[str, dict[str, str]]:
    fields: dict[str, str] = {}
    spans: list[tuple[int, int]] = []
    for match in _FIELD_RE.finditer(text):
        key = match.group("key").lower()
        value = match.group("value").strip()
        if key in fields:
            raise ValueError(f"字段不能重复: {key}")
        if (value.startswith('"') and value.endswith('"')) or (
            value.startswith("“") and value.endswith("”")
        ):
            value = value[1:-1]
        fields[key] = value.strip()
        spans.append(match.span())
    if not spans:
        return text.strip(), fields

    parts: list[str] = []
    last = 0
    for start, end in spans:
        parts.append(text[last:start])
        last = end
    parts.append(text[last:])
    return " ".join("".join(parts).split()), fields


def _parse_positive_amount(value: str) -> tuple[float, int]:
    """把用户金额一次规范为展示用元值和计算用整数分。"""
    cents = ledger_amount_to_cents(value)
    return ledger_cents_to_amount(cents), cents


def _format_cents(cents: int) -> str:
    return f"{cents / 100:.2f}"


def _format_filter_cents(cents: int) -> str:
    return _format_cents(cents).rstrip("0").rstrip(".")


def _transaction_sign(transaction_type: str) -> str:
    if transaction_type == "income":
        return "+"
    if transaction_type == "transfer":
        return "↔ "
    return "-"


def _valid_session_state(step: str, data: dict[str, Any]) -> bool:
    """验证交互步骤的前置字段，防止损坏会话跳步或触发 KeyError。"""
    required = _SESSION_REQUIRED_FIELDS.get(step)
    if required is None or any(key not in data for key in required):
        return False

    transaction_type = data.get("transaction_type")
    if (
        step in {"account", "counter_account", "category", "merchant"}
        and transaction_type not in LEDGER_TRANSACTION_TYPES
    ):
        return False
    if step == "counter_account" and transaction_type != "transfer":
        return False
    if step == "category" and transaction_type == "transfer":
        return False
    if step == "merchant" and transaction_type == "transfer":
        return bool(data.get("counter_account_name"))
    return True


class LedgerHandler(DbOpsMixin):
    """记账处理器

    支持交互式记账和快速记账：
    - add: 无参数时进入多轮交互；有参数时按快速记账解析
    - quick: 单条命令快速记账
    - list: 查看账目列表
    - view: 查看账目详情
    - edit: 编辑账目
    - delete: 删除账目
    - summary: 收支汇总统计
    """

    def __init__(self, db: Database):
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

        if command == "add":
            if rest.strip():
                return await self.quick_add(user_id, rest, context, group_id)
            return await self.start_add_session(user_id, context, group_id)
        if command == "quick":
            return await self.quick_add(user_id, rest, context, group_id)
        if command == "list":
            return await self.list_ledger(user_id, rest, context)
        if command == "view":
            return await self.view_ledger(user_id, rest, context)
        if command == "edit":
            return await self.edit_ledger(user_id, rest, context)
        if command == "delete":
            return await self.delete_ledger(user_id, rest, context)
        if command == "summary":
            return await self.summary(user_id, rest, context)
        return {"status": "error", "message": f"❌ 未知命令: {command}\n\n{self._show_usage()}"}

    @staticmethod
    def _show_usage() -> str:
        return (
            "💰 记账命令\n"
            "管理账目、查看收支和做快速记录。\n\n"
            "可用命令:\n"
            "• /pendo ledger add - 交互式记账\n"
            "• /pendo ledger add <金额> <描述> [cat:分类] [in|out|transfer] [account:账户] [to:账户] [merchant:商户] - 快捷记账\n"
            "• /pendo ledger quick <金额> <描述> [cat:分类] [type:类型] [account:账户] [to:账户] [merchant:商户] [date:日期] [remark:备注] [currency:CNY] - 快速记账\n"
            "• /pendo ledger list [范围] [type:expense/income/transfer] [account:账户] [cat:分类] [amount:N或N..M] [ex] - 查看账目\n"
            "  范围: today/week/month/year/last7d/2026-03/start..end\n"
            "  ex: 额外显示各分类最大单笔\n"
            "• /pendo ledger view <id> - 查看详情\n"
            "• /pendo ledger edit <id> <字段:值> ... - 编辑\n"
            "  字段: amount: title: cat: type: account: to: merchant: date: remark: currency:\n"
            "• /pendo ledger delete <id> - 删除\n"
            "• /pendo ledger summary [范围] - 收支汇总"
        )

    # 交互式记账

    async def start_add_session(
        self, user_id: str, context: PendoContext, group_id: int | None = None
    ) -> CommandMessage:
        """开始交互式记账会话"""
        created = await safe_create_session(
            context,
            initial_data={
                "type": PendoConfig.SESSION_TYPE_LEDGER_ADD,
                "group_id": group_id,
                "step": "amount",
                "data": {},
            },
            timeout=PendoConfig.SESSION_TIMEOUT_SECONDS,
        )
        if not created:
            return {"status": "error", "message": "❌ 无法创建记账会话，请稍后重试"}

        return {
            "status": "success",
            "message": (
                "📝 开始记账，请先输入金额：\n\n"
                "例如：28.5 / ¥88 / 1,200\n"
                "后面只需要输入描述，类型、账户和分类可直接选数字。\n\n"
                "💡 如果想一条命令完成，也可以用：/pendo ledger add 28.5 午饭 cat:餐饮 account:微信\n"
                '💡 输入"退出"可取消'
            ),
        }

    async def handle_session_step(
        self, user_id: str, text: str, session: SessionData, context: PendoContext
    ) -> CommandMessage:
        """处理记账会话的每一步"""
        raw_step = session.get("step")
        raw_data = session.get("data")
        raw_group_id = session.get("group_id")
        if not isinstance(raw_step, str) or not isinstance(raw_data, dict):
            await safe_end_session(context)
            return {"status": "error", "message": "❌ 记账会话状态损坏，请重新开始"}

        data: dict[str, Any] = dict(raw_data)
        # 兼容旧会话时主动丢弃冗余身份和已删除流程留下的内部标记。
        data.pop("owner_id", None)
        data.pop("_compact_entry", None)
        if not _valid_session_state(raw_step, data):
            await safe_end_session(context)
            return {"status": "error", "message": "❌ 记账会话状态损坏，请重新开始"}
        group_id = (
            raw_group_id
            if isinstance(raw_group_id, int) and not isinstance(raw_group_id, bool)
            else None
        )
        text = text.strip()

        if raw_step == "transaction_type":
            return self._step_transaction_type(text, data, session)
        if raw_step == "amount":
            return self._step_amount(text, data, session)
        if raw_step == "account":
            return self._step_account(text, data, session)
        if raw_step == "counter_account":
            return self._step_counter_account(text, data, session)
        if raw_step == "category":
            return self._step_category(text, data, session)
        if raw_step == "merchant":
            return await self._step_merchant(user_id, text, data, context, group_id)
        if raw_step == "description":
            return self._step_description(text, data, session)

        await safe_end_session(context)
        return {"status": "error", "message": "❌ 记账会话状态损坏，请重新开始"}

    def _step_transaction_type(
        self, text: str, data: dict[str, Any], session: SessionData
    ) -> CommandMessage:
        """步骤3: 选择交易类型"""
        parsed_type = parse_ledger_transaction_type(text)
        if text == "1":
            parsed_type = "expense"
        elif text == "2":
            parsed_type = "income"
        elif text == "3":
            parsed_type = "transfer"

        if parsed_type is not None:
            data["transaction_type"] = parsed_type
        else:
            return {"status": "info", "message": "请输入 1(支出)、2(收入) 或 3(转账)"}

        session.set("data", data)
        session.set("step", "account")
        return {"status": "success", "message": self._build_account_prompt(default_allowed=True)}

    def _step_account(
        self, text: str, data: dict[str, Any], session: SessionData
    ) -> CommandMessage:
        """步骤4: 输入账户。"""
        account, error = self._parse_account_choice(text, default_allowed=True)
        if error:
            return {"status": "info", "message": error}
        data["account_name"] = account
        session.set("data", data)

        if data.get("transaction_type") == "transfer":
            session.set("step", "counter_account")
            return {
                "status": "success",
                "message": self._build_account_prompt(
                    default_allowed=False,
                    title="➡️ 请选择转入账户（必须和转出账户不同）：",
                ),
            }

        session.set("step", "category")
        return {
            "status": "success",
            "message": self._build_category_prompt(data["transaction_type"]),
        }

    def _step_counter_account(
        self, text: str, data: dict[str, Any], session: SessionData
    ) -> CommandMessage:
        """步骤5: 输入转账目标账户。"""
        counter, error = self._parse_account_choice(text, default_allowed=False)
        if error:
            return {"status": "info", "message": error}
        if counter == data.get("account_name"):
            return {"status": "info", "message": "❌ 转入账户不能和转出账户相同，请重新输入："}
        data["counter_account_name"] = counter
        data["ledger_category"] = "转账"
        session.set("data", data)
        session.set("step", "merchant")
        return {
            "status": "success",
            "message": "🏷️ 请输入商户/对方（可输入 0 或 跳过）：",
        }

    def _step_amount(self, text: str, data: dict[str, Any], session: SessionData) -> CommandMessage:
        """步骤1: 输入金额"""
        try:
            amount, amount_cents = _parse_positive_amount(text)
        except (ArithmeticError, ValueError):
            return {"status": "info", "message": "❌ 请输入大于0的有效金额："}

        data["amount"] = amount
        data["amount_cents"] = amount_cents
        session.set("data", data)
        session.set("step", "description")
        return {"status": "success", "message": '📝 请输入描述（简要说明，如"午饭"）：'}

    def _step_category(
        self, text: str, data: dict[str, Any], session: SessionData
    ) -> CommandMessage:
        """步骤5: 选择分类"""
        tx_type = data.get("transaction_type", "expense")
        categories = LEDGER_EXPENSE_CATEGORIES if tx_type == "expense" else LEDGER_INCOME_CATEGORIES

        value = text.strip()
        category_name: str | None = None
        if value in {"0", "默认", "跳过"}:
            category_name = "其他"
        elif value.isdecimal() and 1 <= int(value) <= len(categories):
            category_name = categories[int(value) - 1]["name"]
        else:
            category_name = next(
                (
                    category["name"]
                    for category in categories
                    if category["name"] == value or category["id"] == value.lower()
                ),
                None,
            )

        if not category_name:
            return {"status": "info", "message": "❌ 无效的分类，请输入编号或分类名："}

        data["ledger_category"] = category_name
        session.set("data", data)
        session.set("step", "merchant")
        return {
            "status": "success",
            "message": "🏷️ 请输入商户/对方（可输入 0 或 跳过）：",
        }

    async def _step_merchant(
        self,
        user_id: str,
        text: str,
        data: dict[str, Any],
        context: PendoContext,
        group_id: int | None,
    ) -> CommandMessage:
        """最后一步: 输入商户/对方并保存。"""
        merchant = text.strip()
        if merchant and merchant not in {"0", "跳过", "默认", "无", "-"}:
            data["merchant"] = merchant
        await safe_end_session(context)
        return await self._save_ledger_item(user_id, data, group_id)

    def _step_description(
        self,
        text: str,
        data: dict[str, Any],
        session: SessionData,
    ) -> CommandMessage:
        """步骤2: 输入描述，然后进入类型选择"""
        if not text:
            return {"status": "info", "message": "❌ 描述不能为空，请输入："}

        data["title"] = text
        session.set("data", data)
        session.set("step", "transaction_type")
        return {
            "status": "success",
            "message": (
                "📌 请选择收支类型：\n\n"
                "1️⃣ 支出\n"
                "2️⃣ 收入\n"
                "3️⃣ 转账\n\n"
                '(输入 1/2/3，或直接输入"支出"/"收入"/"转账")'
            ),
        }

    @staticmethod
    def _build_category_prompt(transaction_type: str) -> str:
        """根据交易类型构建分类选择提示。"""
        categories = (
            LEDGER_EXPENSE_CATEGORIES if transaction_type == "expense" else LEDGER_INCOME_CATEGORIES
        )

        lines = ["📂 请选择分类：\n"]
        lines.append("0. 📌其他（默认）")
        for i, cat in enumerate(categories, 1):
            lines.append(f"{i}.{cat['icon']}{cat['name']}")
            if i % 4 == 0:
                lines.append("")
        lines.append("\n(输入编号或分类名)")
        return "\n".join(lines)

    @staticmethod
    def _build_account_prompt(
        default_allowed: bool,
        title: str = "🏦 请选择账户/钱包：",
    ) -> str:
        lines = [title, ""]
        if default_allowed:
            lines.append("0. 现金（默认）")
        for index, account in enumerate(_COMMON_LEDGER_ACCOUNTS, 1):
            lines.append(f"{index}. {account}")
        lines.append("\n(输入编号，或直接输入自定义账户名)")
        return "\n".join(lines)

    @staticmethod
    def _parse_account_choice(text: str, default_allowed: bool) -> tuple[str, str | None]:
        value = text.strip()
        if default_allowed and (not value or value in {"0", "跳过", "默认", "现金"}):
            return "现金", None
        if not value or value in {"0", "跳过", "默认"}:
            return "", "❌ 转账必须填写转入账户，可输入编号或账户名："

        try:
            index = int(value)
        except ValueError:
            return value, None

        if 1 <= index <= len(_COMMON_LEDGER_ACCOUNTS):
            return _COMMON_LEDGER_ACCOUNTS[index - 1], None
        return "", "❌ 无效账户编号，请输入列表编号或账户名："

    async def _save_ledger_item(
        self, user_id: str, data: dict[str, Any], group_id: int | None = None
    ) -> CommandMessage:
        """保存记账条目"""
        now = await get_user_local_wall_time(user_id, self.db)
        item_data = {
            "owner_id": user_id,
            "title": data.get("title", ""),
            "content": data.get("remark", ""),
            "amount": data.get("amount"),
            "amount_cents": data.get("amount_cents"),
            "currency": data.get("currency"),
            "transaction_type": data.get("transaction_type"),
            "ledger_category": data.get("ledger_category"),
            "ledger_date": data.get("ledger_date") or now.strftime("%Y-%m-%d"),
            "account_name": data.get("account_name"),
            "counter_account_name": data.get("counter_account_name"),
            "merchant": data.get("merchant"),
            "remark": data.get("remark", ""),
            "context": {"group_id": group_id} if group_id is not None else {},
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
        }
        try:
            normalized = normalize_ledger_fields(item_data, partial=False)
        except ValueError as exc:
            return {"status": "error", "message": f"❌ {exc}"}

        item = LedgerItem(
            owner_id=user_id,
            title=normalized.get("title", ""),
            content=normalized.get("content", ""),
            amount=normalized["amount"],
            amount_cents=normalized["amount_cents"],
            currency=normalized["currency"],
            transaction_type=normalized["transaction_type"],
            ledger_category=normalized["ledger_category"],
            ledger_date=normalized["ledger_date"],
            account_name=normalized["account_name"],
            counter_account_name=normalized.get("counter_account_name", ""),
            merchant=normalized.get("merchant", ""),
            remark=normalized.get("remark", ""),
            context=normalized.get("context", {}),
            created_at=normalized["created_at"],
            updated_at=normalized["updated_at"],
        )

        item_id = await self._db_create_with_log(item, owner_id=user_id, action="create_ledger")

        amount = normalized["amount"]
        transaction_type = normalized["transaction_type"]
        cat_icon = _get_category_icon(normalized["ledger_category"])
        account_line = f"🏦 账户：{normalized['account_name']}"
        if transaction_type == "transfer":
            account_line += f" → {normalized.get('counter_account_name', '')}"
        merchant = normalized.get("merchant", "")

        message = (
            f"✅ 记账成功！\n\n"
            f"{_transaction_type_icon(transaction_type)} {_transaction_type_label(transaction_type)} ¥{amount:.2f}\n"
            f"{cat_icon} 分类：{normalized['ledger_category']}\n"
            f"{account_line}\n"
            f"📝 摘要：{normalized.get('title', '')}\n"
            f"{'🏷️ 商户：' + merchant + chr(10) if merchant else ''}"
            f"📅 日期：{normalized['ledger_date']}\n"
            f"🔖 ID：`{item_id}`"
        )

        return {"status": "success", "message": message, "item_id": item_id}

    # 快速记账

    async def quick_add(
        self, user_id: str, text: str, context: PendoContext, group_id: int | None = None
    ) -> CommandMessage:
        """快速记账

        格式: /pendo ledger quick <金额> <描述> [cat:分类] [in|transfer] [account:账户] [to:账户]
        默认为支出，加 in 标记为收入，加 transfer 标记为转账
        """
        data, error = self._parse_quick_ledger_data(text)
        if error:
            return {"status": "error", "message": error}

        return await self._save_ledger_item(user_id, data, group_id)

    @staticmethod
    def _parse_quick_ledger_data(text: str) -> tuple[dict[str, Any], str | None]:
        if not text:
            return {}, (
                "❌ 用法: /pendo ledger quick <金额> <描述> "
                "[cat:分类] [in|transfer] [account:账户] [to:账户]"
            )

        try:
            text, fields = _extract_field_args(text)
        except ValueError as exc:
            return {}, f"❌ {exc}"
        unknown_fields = sorted(fields.keys() - _QUICK_LEDGER_FIELDS)
        if unknown_fields:
            return {}, f"❌ 不支持的字段: {', '.join(unknown_fields)}"

        raw_type = fields.get("type")
        transaction_type = (
            parse_ledger_transaction_type(raw_type or "") if raw_type is not None else "expense"
        )
        if transaction_type is None:
            return {}, f"❌ 无效交易类型: {raw_type or '(空)'}"
        tokens = text.split()
        remaining_tokens: list[str] = []
        for token in tokens:
            parsed_type = parse_ledger_transaction_type(token)
            if parsed_type is not None:
                transaction_type = parsed_type
            else:
                remaining_tokens.append(token)
        text = " ".join(remaining_tokens)

        # 解析金额和描述
        parts = text.split(maxsplit=1)
        if not parts:
            return {}, "❌ 请提供金额"

        try:
            amount, amount_cents = _parse_positive_amount(parts[0])
        except (ArithmeticError, ValueError):
            return {}, f"❌ 无法识别金额: {parts[0]}"

        title = parts[1].strip() if len(parts) > 1 else ""
        if not title:
            return {}, "❌ 请提供账目描述"

        data = {
            "amount": amount,
            "amount_cents": amount_cents,
            "transaction_type": transaction_type,
            "ledger_category": fields.get("cat") or fields.get("category"),
            "ledger_date": fields.get("date"),
            "account_name": fields.get("account") or fields.get("from"),
            "counter_account_name": fields.get("to") or fields.get("counter"),
            "merchant": fields.get("merchant") or fields.get("payee"),
            "currency": fields.get("currency"),
            "title": title,
            "remark": fields.get("remark", ""),
        }

        return data, None

    @staticmethod
    def _parse_amount_filter(value: str | None) -> tuple[int | None, int | None]:
        """解析列表金额下限或闭区间，返回整数分。"""
        if value is None:
            return None, None
        if not value:
            raise ValueError("金额筛选值不能为空")
        if ".." not in value:
            try:
                return _parse_positive_amount(value)[1], None
            except (ArithmeticError, ValueError) as exc:
                raise ValueError(f"无效金额: {value}") from exc

        lower_text, upper_text = value.split("..", 1)
        if not lower_text or not upper_text:
            raise ValueError(f"无效金额范围: {value}")
        try:
            lower = _parse_positive_amount(lower_text)[1]
            upper = _parse_positive_amount(upper_text)[1]
        except (ArithmeticError, ValueError) as exc:
            raise ValueError(f"无效金额范围: {value}") from exc
        if lower > upper:
            raise ValueError(f"金额范围下限不能大于上限: {value}")
        return lower, upper

    @classmethod
    def _parse_list_filters(cls, filter_str: str) -> LedgerListFilters:
        """解析列表参数；字段值支持中英文引号包裹空格。"""
        residual, fields = _extract_field_args((filter_str or "").strip())
        unknown_fields = sorted(fields.keys() - _LIST_LEDGER_FIELDS)
        if unknown_fields:
            raise ValueError(f"不支持的筛选字段: {', '.join(unknown_fields)}")
        empty_fields = sorted(
            key for key in ("account", "to", "merchant", "cat") if key in fields and not fields[key]
        )
        if empty_fields:
            raise ValueError(f"筛选字段不能为空: {', '.join(empty_fields)}")

        tokens = residual.split()
        flags = {token.lower() for token in tokens}
        range_text = " ".join(token for token in tokens if token.lower() not in {"all", "ex"})

        raw_page = fields.get("page", "1")
        if not raw_page.isdecimal() or int(raw_page) < 1:
            raise ValueError(f"无效页码: {raw_page}")

        raw_type = fields.get("type")
        transaction_type = (
            parse_ledger_transaction_type(raw_type or "") if raw_type is not None else None
        )
        if raw_type is not None and transaction_type is None:
            raise ValueError(f"无效交易类型: {raw_type}")

        amount_min, amount_max = cls._parse_amount_filter(fields.get("amount"))

        return LedgerListFilters(
            range_text=range_text,
            show_all="all" in flags,
            show_extra="ex" in flags,
            page=int(raw_page),
            transaction_type=transaction_type,
            category=fields.get("cat"),
            account=fields.get("account"),
            counter_account=fields.get("to"),
            merchant=fields.get("merchant"),
            amount_min_cents=amount_min,
            amount_max_cents=amount_max,
        )

    async def _load_ledger_items(
        self, user_id: str, start_date: str, end_date: str
    ) -> list[LedgerItem]:
        items = await run_sync(
            self.db.query_items_by_date_range,
            user_id,
            ItemType.LEDGER.value,
            "ledger_date",
            start_date,
            end_date,
        )
        return cast(list[LedgerItem], items)

    @staticmethod
    def _apply_list_filters(
        items: list[LedgerItem], filters: LedgerListFilters
    ) -> list[LedgerItem]:
        if filters.transaction_type:
            items = [item for item in items if item.transaction_type == filters.transaction_type]
        if filters.category:
            items = [item for item in items if item.ledger_category == filters.category]
        if filters.account:
            items = [
                item
                for item in items
                if filters.account in {item.account_name, item.counter_account_name}
            ]
        if filters.counter_account:
            items = [item for item in items if item.counter_account_name == filters.counter_account]
        if filters.merchant:
            items = [item for item in items if item.merchant == filters.merchant]
        if filters.amount_min_cents is not None:
            items = [item for item in items if item.amount_cents >= filters.amount_min_cents]
        if filters.amount_max_cents is not None:
            items = [item for item in items if item.amount_cents <= filters.amount_max_cents]
        return items

    @staticmethod
    def _build_filter_suffix(filters: LedgerListFilters) -> str:
        labels: list[str] = []
        if filters.transaction_type:
            labels.append(_transaction_type_label(filters.transaction_type))
        if filters.category:
            labels.append(f"分类:{filters.category}")
        if filters.account:
            labels.append(f"账户:{filters.account}")
        if filters.counter_account:
            labels.append(f"转入:{filters.counter_account}")
        if filters.merchant:
            labels.append(f"商户:{filters.merchant}")
        if filters.amount_min_cents is not None and filters.amount_max_cents is not None:
            labels.append(
                f"¥{_format_filter_cents(filters.amount_min_cents)}"
                f"~{_format_filter_cents(filters.amount_max_cents)}"
            )
        elif filters.amount_min_cents is not None:
            labels.append(f"≥¥{_format_filter_cents(filters.amount_min_cents)}")
        return f" [{', '.join(labels)}]" if labels else ""

    @staticmethod
    def _summarize_items(items: list[LedgerItem]) -> LedgerTotals:
        totals = LedgerTotals()
        for item in items:
            category = item.ledger_category or "其他"
            if item.transaction_type == "income":
                totals.income_cents += item.amount_cents
                totals.income_by_category[category] = (
                    totals.income_by_category.get(category, 0) + item.amount_cents
                )
            elif item.transaction_type == "expense":
                totals.expense_cents += item.amount_cents
                totals.expense_by_category[category] = (
                    totals.expense_by_category.get(category, 0) + item.amount_cents
                )
            elif item.transaction_type == "transfer":
                totals.transfer_cents += item.amount_cents
        return totals

    @staticmethod
    def _format_list_item(item: LedgerItem) -> str:
        transaction_type = item.transaction_type or "expense"
        account = item.account_name or "现金"
        counter = item.counter_account_name or ""
        account_text = (
            f"{account} → {counter}" if transaction_type == "transfer" and counter else account
        )
        merchant_text = f" | 🏷️ {item.merchant}" if item.merchant else ""
        title_text = f" {item.title}" if item.title else ""
        return (
            f"• {_transaction_type_icon(transaction_type)} "
            f"{_transaction_sign(transaction_type)}¥{_format_cents(item.amount_cents)}  "
            f"{_get_category_icon(item.ledger_category)}{item.ledger_category}{title_text}\n"
            f"  📅 {item.ledger_date or ''} | 🏦 {account_text}{merchant_text} | ID `{item.id}`"
        )

    @staticmethod
    def _format_category_maxima(items: list[LedgerItem]) -> str:
        maxima: dict[str, LedgerItem] = {}
        for item in items:
            category = item.ledger_category or "其他"
            current = maxima.get(category)
            if current is None or item.amount_cents > current.amount_cents:
                maxima[category] = item
        if not maxima:
            return ""

        lines = ["📊 **各分类最大单笔**"]
        for category, item in sorted(
            maxima.items(), key=lambda pair: (-pair[1].amount_cents, pair[0])
        ):
            transaction_type = item.transaction_type or "expense"
            title_text = f" {item.title}" if item.title else ""
            lines.append(
                f"  • {_get_category_icon(category)}{category}: "
                f"{_transaction_type_icon(transaction_type)}"
                f"{_transaction_sign(transaction_type)}¥{_format_cents(item.amount_cents)}"
                f"{title_text} ({item.ledger_date or ''})"
            )
        return "\n".join(lines)

    # 账目列表

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
        - type:expense/income/transfer          -> 按交易类型筛选
        - account:账户 / to:账户 / merchant:商户 -> 按账户/对方账户/商户筛选
        - cat:分类名                             -> 按分类筛选
        - amount:N                              -> 金额 >= N
        - amount:N..M                           -> 金额在 N 到 M 之间

        其他:
        - ex       -> 额外显示每个分类的最大单笔
        - all      -> 显示全部（不分页）
        - page:N   -> 显示第N页
        """
        try:
            filters = self._parse_list_filters(filter_str)
            user_now = await get_user_local_wall_time(user_id, self.db)
            start_date, end_date, range_label = self._parse_date_range(filters.range_text, user_now)
        except ValueError as exc:
            return {"status": "error", "message": f"❌ {exc}"}

        items = await self._load_ledger_items(user_id, start_date, end_date)
        items = self._apply_list_filters(items, filters)
        filter_suffix = self._build_filter_suffix(filters)

        if not items:
            return {
                "status": "success",
                "message": f"💰 **{range_label}账目**{filter_suffix}\n\n暂无记录",
            }

        items.sort(
            key=lambda item: (
                item.ledger_date or "",
                item.created_at or "",
                item.updated_at or "",
                str(item.id),
            ),
            reverse=True,
        )
        display_raw, page_info, has_more = paginate(
            items, filters.page, PendoConfig.LIST_PAGE_SIZE, filters.show_all
        )
        display_items = cast(list[LedgerItem], display_raw)
        if not display_items and filters.page > 1:
            return {"status": "error", "message": f"❌ 第 {filters.page} 页没有账目"}

        totals = self._summarize_items(items)
        formatted_items = "\n\n".join(self._format_list_item(item) for item in display_items)
        message = (
            f"💰 {range_label}账目{filter_suffix}\n"
            f"共 {len(items)} 笔{page_info}\n"
            f"💸 支出 ¥{_format_cents(totals.expense_cents)} | "
            f"💰 收入 ¥{_format_cents(totals.income_cents)} | "
            f"🔁 转账 ¥{_format_cents(totals.transfer_cents)}\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"{formatted_items}\n"
        )

        if has_more:
            message += f"... (使用 'all' 显示全部或 'page:{filters.page + 1}' 查看下一页)\n"
        if filters.show_extra:
            message += f"\n{self._format_category_maxima(items)}\n"

        return {"status": "success", "message": message}

    # 查看详情

    async def view_ledger(
        self, user_id: str, item_id: str, context: PendoContext
    ) -> CommandMessage:
        """查看单个账目详情"""
        if not item_id:
            raise MissingRequiredFieldException("id")

        item_id = item_id.strip()
        if error := self._single_token_error(
            item_id, "❌ 账目详情只接受一个ID\n例如: /pendo ledger view abc12345"
        ):
            return error
        item, wrong_type = await self._db_get_typed_item_or_message(
            item_id, user_id, ItemType.LEDGER.value, "账目"
        )
        if wrong_type:
            return wrong_type
        item = cast(LedgerItem, item)
        display_timezone = await run_sync(
            TimezoneHelper.get_user_timezone,
            user_id,
            self.db,
        )

        tx_type = item.transaction_type or "expense"
        icon = _transaction_type_icon(tx_type)
        cat_icon = _get_category_icon(item.ledger_category)
        account = item.account_name or "现金"
        counter = item.counter_account_name or ""
        account_text = f"{account} → {counter}" if tx_type == "transfer" and counter else account

        message = (
            f"💰 **账目详情**\n\n"
            f"{icon} {_transaction_type_label(tx_type)} "
            f"¥{_format_cents(item.amount_cents)} {item.currency or 'CNY'}\n"
            f"{cat_icon} 分类：{item.ledger_category}\n"
            f"🏦 账户：{account_text}\n"
            f"📝 描述：{item.title or '无'}\n"
            f"📅 日期：{item.ledger_date or '未知'}\n"
            f"🔖 ID：`{item.id}`\n"
            f"⏰ 创建：{ItemFormatter.format_datetime(item.created_at, tz=display_timezone)}"
        )

        if item.merchant:
            message += f"\n🏷️ 商户：{item.merchant}"
        if item.remark:
            message += f"\n💬 备注：{item.remark}"

        return {"status": "success", "message": message}

    @staticmethod
    def _parse_edit_updates(edit_text: str) -> dict[str, Any]:
        """解析显式编辑字段，拒绝静默忽略的正文或未知字段。"""
        residual, fields = _extract_field_args(edit_text)
        if residual:
            raise ValueError("编辑只接受 field:value 字段；含空格的值请加引号")
        if not fields:
            raise ValueError("未识别到有效字段")

        unknown_fields = sorted(fields.keys() - _EDIT_LEDGER_FIELDS)
        if unknown_fields:
            raise ValueError(f"不支持的编辑字段: {', '.join(unknown_fields)}")
        empty_fields = sorted(
            key for key in _NONEMPTY_EDIT_FIELDS if key in fields and not fields[key]
        )
        if empty_fields:
            raise ValueError(f"编辑字段不能为空: {', '.join(empty_fields)}")

        updates: dict[str, Any] = {}
        for source, value in fields.items():
            if source == "type":
                continue
            target = _EDIT_LEDGER_FIELD_MAP[source]
            if target in updates:
                raise ValueError(f"同一字段不能使用多个别名: {source}")
            updates[target] = value

        if "type" in fields:
            raw_type = fields["type"]
            transaction_type = parse_ledger_transaction_type(raw_type)
            if transaction_type is None:
                raise ValueError(f"无效交易类型: {raw_type or '(空)'}")
            updates["transaction_type"] = transaction_type
        return updates

    @staticmethod
    def _normalize_edit_updates(
        item: LedgerItem, requested_updates: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """合并并校验编辑，同时清理交易类型切换后失效的字段。"""
        updates = dict(requested_updates)
        final_type = str(updates.get("transaction_type") or item.transaction_type or "expense")
        counter = str(updates.get("counter_account_name") or "").strip()
        if final_type != "transfer" and counter:
            raise ValueError("只有转账账目可以设置转入账户")
        if final_type != "transfer" and item.counter_account_name:
            updates["counter_account_name"] = ""
        if item.transaction_type == "transfer" and final_type != "transfer":
            updates.setdefault("ledger_category", "其他")
        if item.transaction_type != "transfer" and final_type == "transfer":
            updates.setdefault("ledger_category", "转账")

        merged = item.to_dict()
        if "amount" in updates:
            merged.pop("amount_cents", None)
        merged.update(updates)
        normalized = normalize_ledger_fields(merged, partial=False)
        apply_updates = {key: normalized[key] for key in updates if key in normalized}
        if "amount" in updates:
            apply_updates["amount"] = normalized["amount"]
            apply_updates["amount_cents"] = normalized["amount_cents"]
        return apply_updates, normalized

    @staticmethod
    def _format_edit_labels(updates: dict[str, Any], normalized: dict[str, Any]) -> list[str]:
        labels: list[str] = []
        if "transaction_type" in updates:
            labels.append(f"类型 → {_transaction_type_label(normalized['transaction_type'])}")
        if "amount" in updates:
            labels.append(f"金额 → ¥{normalized['amount']:.2f}")
        for key, label in _EDIT_LEDGER_LABELS.items():
            if key in updates:
                labels.append(f"{label} → {normalized.get(key, '')}")
        return labels

    # 编辑

    async def edit_ledger(self, user_id: str, args: str, context: PendoContext) -> CommandMessage:
        """编辑账目

        格式: /pendo ledger edit <id> [amount:金额] [title:描述] [cat:分类] [type:expense/income/transfer] [account:账户] [to:账户] [date:日期] [remark:备注]
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
                    "• type:expense/income/transfer - 修改交易类型\n"
                    "• account:账户 / to:账户 - 修改账户/转入账户\n"
                    "• merchant:商户 - 修改商户\n"
                    "• date:YYYY-MM-DD - 修改日期\n"
                    "• remark:备注 / currency:CNY - 修改备注或币种\n\n"
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

        try:
            requested_updates = self._parse_edit_updates(edit_str)
            apply_updates, normalized = self._normalize_edit_updates(item, requested_updates)
        except ValueError as exc:
            return {"status": "error", "message": f"❌ {exc}"}

        await self._db_update_with_log(
            item_id,
            apply_updates,
            user_id,
            action="edit_ledger",
            expected_version=item.version,
        )

        field_labels = self._format_edit_labels(apply_updates, normalized)
        changes = "\n".join(f"  • {label}" for label in field_labels)
        return {
            "status": "success",
            "message": f"✅ 已更新账目 `{item_id}`\n\n{changes}\n\n💡 /pendo undo 可撤销编辑",
        }

    # 删除

    async def delete_ledger(
        self, user_id: str, item_id: str, context: PendoContext
    ) -> CommandMessage:
        """删除账目"""
        if not item_id:
            raise MissingRequiredFieldException("id")

        item_id = item_id.strip()
        if error := self._single_token_error(
            item_id, "❌ 删除账目只接受一个ID\n例如: /pendo ledger delete abc12345"
        ):
            return error
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
                f"🗑️ 已删除: {_transaction_type_label(item.transaction_type or 'expense')} "
                f"¥{_format_cents(item.amount_cents)} {item.title or ''}\n\n"
                f"{PendoConfig.UNDO_HINT}"
            ),
        }

    @staticmethod
    def _format_category_breakdown(title: str, amounts: dict[str, int], total_cents: int) -> str:
        if not amounts:
            return ""
        lines = [title]
        for index, (category, amount_cents) in enumerate(
            sorted(amounts.items(), key=lambda pair: (-pair[1], pair[0])),
            1,
        ):
            percentage = amount_cents / total_cents * 100 if total_cents else 0.0
            lines.append(
                f"  {index}. {_get_category_icon(category)} {category}  "
                f"¥{_format_cents(amount_cents)} ({percentage:.1f}%)"
            )
        return "\n".join(lines)

    # 收支汇总

    async def summary(self, user_id: str, range_str: str, context: PendoContext) -> CommandMessage:
        """收支汇总统计"""
        try:
            user_now = await get_user_local_wall_time(user_id, self.db)
            start_date, end_date, range_label = self._parse_date_range(range_str, user_now)
        except ValueError as exc:
            return {"status": "error", "message": f"❌ {exc}"}

        items = await self._load_ledger_items(user_id, start_date, end_date)

        if not items:
            return {"status": "success", "message": f"📊 **{range_label}收支汇总**\n\n暂无记录"}

        totals = self._summarize_items(items)
        balance_cents = totals.income_cents - totals.expense_cents
        balance_sign = "+" if balance_cents >= 0 else ""
        message = (
            f"📊 {range_label}收支汇总\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"💸 总支出: ¥{_format_cents(totals.expense_cents)}\n"
            f"💰 总收入: ¥{_format_cents(totals.income_cents)}\n"
            f"🔁 转账流量: ¥{_format_cents(totals.transfer_cents)}\n"
            f"📊 结余: {balance_sign}¥{_format_cents(balance_cents)}\n"
        )

        if totals.expense_by_category:
            top_category, top_cents = sorted(
                totals.expense_by_category.items(), key=lambda pair: (-pair[1], pair[0])
            )[0]
            message += f"📂 最大支出分类: {top_category} ¥{_format_cents(top_cents)}\n"
        if totals.income_by_category:
            top_category, top_cents = sorted(
                totals.income_by_category.items(), key=lambda pair: (-pair[1], pair[0])
            )[0]
            message += f"📥 主要收入来源: {top_category} ¥{_format_cents(top_cents)}\n"

        message += "━━━━━━━━━━━━━━━━━━\n"
        expense_breakdown = self._format_category_breakdown(
            "📂 支出分类", totals.expense_by_category, totals.expense_cents
        )
        income_breakdown = self._format_category_breakdown(
            "📂 收入分类", totals.income_by_category, totals.income_cents
        )
        message += "\n\n".join(
            breakdown for breakdown in (expense_breakdown, income_breakdown) if breakdown
        )

        return {"status": "success", "message": message}

    # 工具方法

    @staticmethod
    def _parse_date_range(range_str: str, now: datetime) -> tuple[str, str, str]:
        """解析日期范围，返回 (start_date, end_date, label)。

        委托 _parse_time_range_core 做实际计算，仅负责生成账目列表的人类可读标签。
        """
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
            raise ValueError(f"无法解析时间范围: {range_str}")

        start_dt, end_dt = _parse_time_range_core(rs or "本月", now, strict=True)
        return start_dt.strftime("%Y-%m-%d"), end_dt.strftime("%Y-%m-%d"), label
