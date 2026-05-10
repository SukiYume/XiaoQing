"""
记账(Ledger)处理器
处理记账相关的所有操作，不需要AI解析
"""

import logging
import re
from datetime import datetime
from typing import TYPE_CHECKING, Any, cast

from core.plugin_base import run_sync

from ..config import (
    LEDGER_EXPENSE_CATEGORIES,
    LEDGER_INCOME_CATEGORIES,
    PendoConfig,
)
from ..core.exceptions import MissingRequiredFieldException
from ..core.types import CommandMessage, PendoContext
from ..models.item import ItemType, LedgerItem
from ..utils.db_ops import DbOpsMixin
from ..utils.error_handlers import handle_command_errors
from ..utils.formatters import ItemFormatter, paginate
from ..utils.session_utils import safe_create_session, safe_end_session
from ..utils.time_utils import _parse_time_range_core, now_in_timezone
from ..utils.validators import normalize_ledger_fields

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from ..services.db import Database


def _get_category_icon(name: str) -> str:
    """根据分类名获取图标"""
    for cat in LEDGER_EXPENSE_CATEGORIES + LEDGER_INCOME_CATEGORIES:
        if cat["name"] == name:
            return cat["icon"]
    return "📌"


def _transaction_type_label(transaction_type: str) -> str:
    labels = {"income": "收入", "expense": "支出", "transfer": "转账"}
    return labels.get(transaction_type, "支出")


def _transaction_type_icon(transaction_type: str) -> str:
    icons = {"income": "💰", "expense": "💸", "transfer": "🔁"}
    return icons.get(transaction_type, "💸")


_COMMON_LEDGER_ACCOUNTS = ["现金", "微信", "支付宝", "银行卡", "信用卡"]


_FIELD_RE = re.compile(r"(?P<key>[A-Za-z_]+):(?P<value>\"[^\"]*\"|“[^”]*”|\S+)")


def _extract_field_args(text: str) -> tuple[str, dict[str, str]]:
    fields: dict[str, str] = {}
    spans: list[tuple[int, int]] = []
    for match in _FIELD_RE.finditer(text):
        key = match.group("key").lower()
        value = match.group("value").strip()
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


def _parse_ledger_type(value: str) -> str | None:
    mapping = {
        "in": "income",
        "income": "income",
        "收入": "income",
        "out": "expense",
        "expense": "expense",
        "支出": "expense",
        "transfer": "transfer",
        "xfer": "transfer",
        "转账": "transfer",
    }
    return mapping.get(str(value or "").strip().lower())


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
            "add": lambda: (
                self.quick_add(user_id, rest, context, group_id)
                if rest.strip()
                else self.start_add_session(user_id, context, group_id)
            ),
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
            "• /pendo ledger add <金额> <描述> [cat:分类] [in|out|transfer] [account:账户] [to:账户] [merchant:商户] - 快捷记账\n"
            "• /pendo ledger quick <金额> <描述> [cat:分类] [in|transfer] [account:账户] [to:账户] [merchant:商户] - 快速记账\n"
            "• /pendo ledger list [范围] [type:expense/income/transfer] [account:账户] [cat:分类] [amount:N或N..M] [ex] - 查看账目\n"
            "  范围: today/week/month/year/last7d/2026-03/start..end\n"
            "  ex: 额外显示各分类最大单笔\n"
            "• /pendo ledger view <id> - 查看详情\n"
            "• /pendo ledger edit <id> <字段:值> ... - 编辑\n"
            "  字段: amount: title: cat: type: account: to: merchant: date: remark:\n"
            "• /pendo ledger delete <id> - 删除\n"
            "• /pendo ledger summary [范围] - 收支汇总"
        )

    # ============================================================
    # 交互式记账
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
                "step": "amount",
                "data": {},
            },
            timeout=PendoConfig.SESSION_TIMEOUT_SECONDS,
        )

        return {
            "status": "success",
            "message": (
                "📝 开始记账，请先输入金额：\n\n"
                "例如：28.5 / ¥88 / 1,200\n"
                "后面只需要输入描述，类型、账户和分类可直接选数字。\n\n"
                "💡 如果想一条命令完成，也可以用：/pendo ledger add 28.5 午饭 cat:餐饮 account:微信\n"
                "💡 输入\"退出\"可取消"
            ),
        }

    async def handle_session_step(
        self, user_id: str, text: str, session: dict[str, Any], context: PendoContext
    ) -> CommandMessage:
        """处理记账会话的每一步"""
        step = session.get("step", "amount")
        data = session.get("data", {})
        group_id = session.get("group_id")

        text = text.strip()

        if step == "entry":
            return await self._step_entry(user_id, text, data, session, context, group_id)
        elif step == "transaction_type":
            return await self._step_transaction_type(text, data, session, context)
        elif step == "amount":
            return await self._step_amount(text, data, session, context)
        elif step == "account":
            return await self._step_account(text, data, session, context)
        elif step == "counter_account":
            return await self._step_counter_account(text, data, session, context)
        elif step == "category":
            return await self._step_category(text, data, session, context)
        elif step == "merchant":
            return await self._step_merchant(text, data, session, context)
        elif step == "description":
            return await self._step_description(user_id, text, data, session, context, group_id)
        else:
            return {"status": "error", "message": "❌ 会话状态异常"}

    async def _step_entry(
        self,
        user_id: str,
        text: str,
        data: dict,
        session: dict,
        context: PendoContext,
        group_id: int | None,
    ) -> CommandMessage:
        """Compact add flow: parse a full ledger entry from one message."""
        parsed_data, error = self._parse_quick_ledger_data(text)
        if error:
            return {
                "status": "info",
                "message": (
                    f"{error}\n\n"
                    "请重新发送，例如：28.5 午饭 cat:餐饮 account:微信"
                ),
            }

        data.update(parsed_data)
        data["owner_id"] = user_id

        if data.get("transaction_type") == "transfer" and not data.get("counter_account_name"):
            data["_compact_entry"] = True
            session.set("data", data)
            session.set("step", "counter_account")
            return {
                "status": "success",
                "message": "➡️ 转账需要转入账户，请输入转入账户（必须和转出账户不同）：",
            }

        await safe_end_session(context)
        return await self._save_ledger_item(user_id, data, group_id)

    async def _step_transaction_type(
        self, text: str, data: dict, session: dict, context: PendoContext
    ) -> CommandMessage:
        """步骤3: 选择交易类型"""
        parsed_type = _parse_ledger_type(text)
        if text == "1":
            parsed_type = "expense"
        elif text == "2":
            parsed_type = "income"
        elif text == "3":
            parsed_type = "transfer"

        if parsed_type in {"expense", "income", "transfer"}:
            data["transaction_type"] = parsed_type
        else:
            return {"status": "info", "message": "请输入 1(支出)、2(收入) 或 3(转账)"}

        session.set("data", data)
        session.set("step", "account")
        return {"status": "success", "message": self._build_account_prompt(default_allowed=True)}

    async def _step_account(
        self, text: str, data: dict, session: dict, context: PendoContext
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
        return {"status": "success", "message": self._build_category_prompt(data["transaction_type"])}

    async def _step_counter_account(
        self, text: str, data: dict, session: dict, context: PendoContext
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
        if data.get("_compact_entry"):
            await safe_end_session(context)
            return await self._save_ledger_item(
                data.get("owner_id", ""), data, session.get("group_id")
            )
        session.set("step", "merchant")
        return {
            "status": "success",
            "message": "🏷️ 请输入商户/对方（可输入 0 或 跳过）：",
        }

    async def _step_amount(
        self, text: str, data: dict, session: dict, context: PendoContext
    ) -> CommandMessage:
        """步骤1: 输入金额"""
        try:
            amount = float(text.replace("￥", "").replace("¥", "").replace(",", "").strip())
            if amount <= 0:
                return {"status": "info", "message": "❌ 金额必须大于0，请重新输入："}
        except ValueError:
            return {"status": "info", "message": "❌ 请输入有效的数字金额："}

        data["amount"] = amount
        session.set("data", data)
        session.set("step", "description")
        return {"status": "success", "message": "📝 请输入描述（简要说明，如\"午饭\"）："}

    async def _step_category(
        self, text: str, data: dict, session: dict, context: PendoContext
    ) -> CommandMessage:
        """步骤5: 选择分类"""
        tx_type = data.get("transaction_type", "expense")
        categories = (
            LEDGER_EXPENSE_CATEGORIES if tx_type == "expense" else LEDGER_INCOME_CATEGORIES
        )

        category_name = None

        if text.strip() in {"0", "默认", "跳过"}:
            category_name = "其他"

        # 尝试按编号匹配
        if not category_name:
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
        session.set("step", "merchant")
        return {
            "status": "success",
            "message": "🏷️ 请输入商户/对方（可输入 0 或 跳过）：",
        }

    async def _step_merchant(
        self, text: str, data: dict, session: dict, context: PendoContext
    ) -> CommandMessage:
        """最后一步: 输入商户/对方并保存。"""
        merchant = text.strip()
        if merchant and merchant not in {"0", "跳过", "默认", "无", "-"}:
            data["merchant"] = merchant
        await safe_end_session(context)
        return await self._save_ledger_item(
            data.get("owner_id", ""), data, session.get("group_id")
        )

    async def _step_description(
        self, user_id: str, text: str, data: dict, session: dict,
        context: PendoContext, group_id: int | None
    ) -> CommandMessage:
        """步骤2: 输入描述，然后进入类型选择"""
        if not text:
            return {"status": "info", "message": "❌ 描述不能为空，请输入："}

        data["title"] = text
        data["owner_id"] = user_id
        session.set("data", data)
        session.set("step", "transaction_type")
        return {
            "status": "success",
            "message": (
                "📌 请选择收支类型：\n\n"
                "1️⃣ 支出\n"
                "2️⃣ 收入\n"
                "3️⃣ 转账\n\n"
                "(输入 1/2/3，或直接输入\"支出\"/\"收入\"/\"转账\")"
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
        self, user_id: str, data: dict, group_id: int | None = None
    ) -> CommandMessage:
        """保存记账条目"""
        now = now_in_timezone(user_id, self.db).replace(tzinfo=None)
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
            "context": {"group_id": group_id} if group_id else {},
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

    # ============================================================
    # 快速记账
    # ============================================================

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

        text, fields = _extract_field_args(text)

        transaction_type = _parse_ledger_type(fields.get("type") or "") or "expense"
        tokens = text.split()
        remaining_tokens: list[str] = []
        for token in tokens:
            parsed_type = _parse_ledger_type(token)
            if parsed_type in {"income", "transfer"}:
                transaction_type = parsed_type
            elif parsed_type == "expense" or token.lower() in {"out", "expense"}:
                transaction_type = "expense"
            else:
                remaining_tokens.append(token)
        text = " ".join(remaining_tokens)

        # 解析金额和描述
        parts = text.split(maxsplit=1)
        if not parts:
            return {}, "❌ 请提供金额"

        try:
            amount = float(parts[0].replace("￥", "").replace("¥", "").replace(",", ""))
            if amount <= 0:
                return {}, "❌ 金额必须大于0"
        except ValueError:
            return {}, f"❌ 无法识别金额: {parts[0]}"

        title = parts[1] if len(parts) > 1 else ""

        data = {
            "amount": amount,
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
        filter_str = (filter_str or "").strip()

        # 解析所有参数
        show_all = False
        page_num = 1
        show_extra = False
        type_filter = None
        cat_filter = None
        account_filter = None
        counter_filter = None
        merchant_filter = None
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
                    page_num = int(part.split(":", 1)[1])
                    if page_num < 1:
                        raise ValueError
                except (IndexError, ValueError):
                    return {"status": "error", "message": f"❌ 无效页码: {part}"}
            elif part.startswith("type:"):
                val = part[5:]
                type_filter = _parse_ledger_type(val)
                if not type_filter:
                    return {"status": "error", "message": f"❌ 无效交易类型: {val}"}
            elif part.startswith("account:"):
                account_filter = part[8:]
            elif part.startswith("to:"):
                counter_filter = part[3:]
            elif part.startswith("merchant:"):
                merchant_filter = part[9:]
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
                        return {"status": "error", "message": f"❌ 无效金额范围: {rng}"}
                else:
                    try:
                        amount_min = float(rng)
                    except ValueError:
                        return {"status": "error", "message": f"❌ 无效金额: {rng}"}
            else:
                clean_parts.append(part)
        range_str = " ".join(clean_parts)

        try:
            start_date, end_date, range_label = self._parse_date_range(range_str)
        except ValueError as exc:
            return {"status": "error", "message": f"❌ {str(exc)}"}

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
        if type_filter:
            items = [
                i for i in items
                if getattr(i, "transaction_type", None) == type_filter
            ]
        if cat_filter:
            items = [i for i in items if i.ledger_category == cat_filter]
        if account_filter:
            items = [
                i for i in items
                if account_filter in {getattr(i, "account_name", ""), getattr(i, "counter_account_name", "")}
            ]
        if counter_filter:
            items = [i for i in items if getattr(i, "counter_account_name", "") == counter_filter]
        if merchant_filter:
            items = [i for i in items if getattr(i, "merchant", "") == merchant_filter]
        if amount_min is not None:
            items = [i for i in items if i.amount >= amount_min]
        if amount_max is not None:
            items = [i for i in items if i.amount <= amount_max]

        # 构建过滤描述
        filter_labels = []
        if type_filter:
            filter_labels.append(_transaction_type_label(type_filter))
        if cat_filter:
            filter_labels.append(f"分类:{cat_filter}")
        if account_filter:
            filter_labels.append(f"账户:{account_filter}")
        if counter_filter:
            filter_labels.append(f"转入:{counter_filter}")
        if merchant_filter:
            filter_labels.append(f"商户:{merchant_filter}")
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
        total_income = sum(i.amount for i in items if getattr(i, "transaction_type", "") == "income")
        total_expense = sum(i.amount for i in items if getattr(i, "transaction_type", "") == "expense")
        total_transfer = sum(i.amount for i in items if getattr(i, "transaction_type", "") == "transfer")

        message = (
            f"💰 {range_label}账目{filter_suffix}\n"
            f"共 {len(items)} 笔{page_info}\n"
            f"💸 支出 ¥{total_expense:.2f} | 💰 收入 ¥{total_income:.2f} | 🔁 转账 ¥{total_transfer:.2f}\n"
            "━━━━━━━━━━━━━━━━━━\n"
        )

        for item in display_items:
            tx_type = getattr(item, "transaction_type", "expense")
            icon = _transaction_type_icon(tx_type)
            cat_icon = _get_category_icon(item.ledger_category)
            sign = "+" if tx_type == "income" else ("↔ " if tx_type == "transfer" else "-")
            date_str = item.ledger_date or ""
            message += f"• {icon} {sign}¥{item.amount:.2f}  {cat_icon}{item.ledger_category}"
            if item.title:
                message += f" {item.title}"
            account = getattr(item, "account_name", "") or "现金"
            counter = getattr(item, "counter_account_name", "") or ""
            account_text = f"{account} → {counter}" if tx_type == "transfer" and counter else account
            merchant = getattr(item, "merchant", "") or ""
            merchant_text = f" | 🏷️ {merchant}" if merchant else ""
            message += f"\n  📅 {date_str} | 🏦 {account_text}{merchant_text} | ID `{item.id}`\n\n"

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
                    tx_type = getattr(item, "transaction_type", "expense")
                    icon = _transaction_type_icon(tx_type)
                    cat_icon = _get_category_icon(cat)
                    sign = "+" if tx_type == "income" else ("↔ " if tx_type == "transfer" else "-")
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

        tx_type = getattr(item, "transaction_type", "expense")
        icon = _transaction_type_icon(tx_type)
        cat_icon = _get_category_icon(item.ledger_category)
        account = getattr(item, "account_name", "") or "现金"
        counter = getattr(item, "counter_account_name", "") or ""
        account_text = f"{account} → {counter}" if tx_type == "transfer" and counter else account

        message = (
            f"💰 **账目详情**\n\n"
            f"{icon} {_transaction_type_label(tx_type)} ¥{item.amount:.2f} {getattr(item, 'currency', 'CNY') or 'CNY'}\n"
            f"{cat_icon} 分类：{item.ledger_category}\n"
            f"🏦 账户：{account_text}\n"
            f"📝 描述：{item.title or '无'}\n"
            f"📅 日期：{item.ledger_date or '未知'}\n"
            f"🔖 ID：`{item.id}`\n"
            f"⏰ 创建：{ItemFormatter.format_datetime(item.created_at)}"
        )

        if getattr(item, "merchant", ""):
            message += f"\n🏷️ 商户：{item.merchant}"
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

        _unused_text, fields = _extract_field_args(edit_str)
        if not fields:
            return {
                "status": "error",
                "message": (
                    "❌ 未识别到有效字段\n\n"
        "可用字段: amount: title: cat: type: account: to: merchant: date: remark:\n"
                    "带空格值请加引号，例如 title:\"团队午餐\""
                ),
            }

        updates: dict[str, Any] = {}
        field_labels: list[str] = []
        field_map = {
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
        for key, value in fields.items():
            if key == "type":
                parsed_type = _parse_ledger_type(value)
                if not parsed_type:
                    return {"status": "error", "message": f"❌ 无效交易类型: {value}"}
                updates["transaction_type"] = parsed_type
                field_labels.append(f"类型 → {_transaction_type_label(parsed_type)}")
                continue
            target = field_map.get(key)
            if not target:
                continue
            updates[target] = value

        if not updates:
            return {"status": "error", "message": "❌ 未识别到有效字段"}

        merged = item.to_dict()
        if "amount" in updates and "amount_cents" not in updates:
            merged.pop("amount_cents", None)
        merged.update(updates)
        try:
            normalized = normalize_ledger_fields(merged, partial=False)
        except ValueError as exc:
            return {"status": "error", "message": f"❌ {exc}"}

        apply_updates = {key: normalized[key] for key in updates if key in normalized}
        if "amount" in updates or "amount_cents" in updates:
            apply_updates["amount"] = normalized["amount"]
            apply_updates["amount_cents"] = normalized["amount_cents"]
            field_labels.append(f"金额 → ¥{normalized['amount']:.2f}")
        for label, key in [
            ("摘要", "title"),
            ("分类", "ledger_category"),
            ("日期", "ledger_date"),
            ("账户", "account_name"),
            ("转入", "counter_account_name"),
            ("商户", "merchant"),
            ("备注", "remark"),
            ("币种", "currency"),
        ]:
            if key in updates:
                field_labels.append(f"{label} → {normalized.get(key, '')}")

        await self._db_update_with_log(item_id, apply_updates, user_id, action="edit_ledger")

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
                f"🗑️ 已删除: {_transaction_type_label(getattr(item, 'transaction_type', 'expense'))} ¥{item.amount:.2f} {item.title or ''}\n\n"
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
        total_income = sum(i.amount for i in items if getattr(i, "transaction_type", "") == "income")
        total_expense = sum(i.amount for i in items if getattr(i, "transaction_type", "") == "expense")
        total_transfer = sum(i.amount for i in items if getattr(i, "transaction_type", "") == "transfer")
        balance = total_income - total_expense

        # 按分类统计支出
        expense_by_cat: dict[str, float] = {}
        for i in items:
            if getattr(i, "transaction_type", "") == "expense":
                cat = i.ledger_category or "其他"
                expense_by_cat[cat] = expense_by_cat.get(cat, 0.0) + i.amount

        # 按分类统计收入
        income_by_cat: dict[str, float] = {}
        for i in items:
            if getattr(i, "transaction_type", "") == "income":
                cat = i.ledger_category or "其他"
                income_by_cat[cat] = income_by_cat.get(cat, 0.0) + i.amount

        # 格式化
        balance_sign = "+" if balance >= 0 else ""
        message = (
            f"📊 {range_label}收支汇总\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"💸 总支出: ¥{total_expense:.2f}\n"
            f"💰 总收入: ¥{total_income:.2f}\n"
            f"🔁 转账流量: ¥{total_transfer:.2f}\n"
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

        委托 _parse_time_range_core 做实际计算，仅负责生成账目列表的人类可读标签。
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
            raise ValueError(f"无法解析时间范围: {range_str}")

        start_dt, end_dt = _parse_time_range_core(rs or "本月", now, strict=True)
        return start_dt.strftime("%Y-%m-%d"), end_dt.strftime("%Y-%m-%d"), label
