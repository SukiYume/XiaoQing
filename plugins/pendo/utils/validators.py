"""
输入验证和清洗工具

提供统一的输入验证功能，确保数据安全性和一致性
"""

import json
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Final, TypeAlias
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..config import PendoConfig

DEFAULT_EVENT_REMINDER_RULES: Final[tuple[dict[str, int], ...]] = ({"offset_seconds": 0},)
DEFAULT_EVENT_TIMEZONE: Final = ZoneInfo(PendoConfig.DEFAULT_TIMEZONE)
EVENT_ROLES: Final = frozenset({"single", "multi_node_child", "recurring_occurrence"})
EVENT_COLLECTION_KINDS: Final = frozenset({"multi_node", "recurring"})
TASK_STATUSES: Final = frozenset({"open", "done", "cancelled"})
LEGACY_TASK_FIELDS: Final = frozenset(
    {"due_time", "estimate", "subtasks", "dependencies", "progress"}
)

LEDGER_TRANSACTION_TYPES: Final        = frozenset({"expense", "income", "transfer"})
LEDGER_TRANSACTION_TYPE_ALIASES: Final = {
    "out": "expense",
    "expense": "expense",
    "支出": "expense",
    "in": "income",
    "income": "income",
    "收入": "income",
    "transfer": "transfer",
    "xfer": "transfer",
    "转账": "transfer",
}
LEDGER_DEFAULT_ACCOUNT   = "现金"
LEDGER_DEFAULT_CURRENCY  = "CNY"
MAX_NOTE_REFERENCES      = 100
MAX_NOTE_REFERENCE_BYTES = 64 * 1024

COMMON_ITEM_FIELDS = {
    "id",
    "owner_id",
    "type",
    "title",
    "content",
    "tags",
    "category",
    "created_at",
    "updated_at",
    "context",
    "visibility",
    "attachments",
    "ai_meta",
    "deleted",
    "deleted_at",
    "version",
}

TYPE_SPECIFIC_ITEM_FIELDS = {
    "event": {
        "start_time",
        "end_time",
        "timezone",
        "location",
        "participants",
        "remind_times",
        "notes",
        "event_role",
        "event_collection_id",
        "event_collection_kind",
        "event_index",
        "event_node_key",
        "source_item_id",
        "reminder_rules",
    },
    "task": {
        "plan_date",
        "deadline_at",
        "priority",
        "status",
        "remind_times",
        "reminder_rules",
        "repeat_rule",
        "completed_at",
        "cancelled_at",
    },
    "ledger": {
        "amount",
        "amount_cents",
        "currency",
        "transaction_type",
        "ledger_category",
        "ledger_date",
        "account_name",
        "counter_account_name",
        "merchant",
        "remark",
    },
    "note": {"references", "last_viewed", "related_items"},
    "diary": {
        "mood",
        "mood_score",
        "weather",
        "location",
        "template_id",
        "diary_date",
        "entry_time",
        "template_answers",
        "is_favorite",
    },
}

SUPPORTED_ITEM_TYPES = set(TYPE_SPECIFIC_ITEM_FIELDS)

DIARY_MOOD_VALUES = {
    "happy",
    "calm",
    "excited",
    "sad",
    "angry",
    "tired",
    "anxious",
    "grateful",
    "neutral",
}

DIARY_MOOD_ALIASES = {
    "😊": "happy",
    "😄": "happy",
    "😌": "calm",
    "🤩": "excited",
    "😢": "sad",
    "😭": "sad",
    "😠": "angry",
    "😡": "angry",
    "😴": "tired",
    "😰": "anxious",
    "🙏": "grateful",
    "😐": "neutral",
    "开心": "happy",
    "平静": "calm",
    "兴奋": "excited",
    "难过": "sad",
    "生气": "angry",
    "疲惫": "tired",
    "焦虑": "anxious",
    "感恩": "grateful",
    "普通": "neutral",
}


def sanitize_text(text: str, max_length: int = 10000) -> str:
    """清洗文本输入

    Args:
        text: 输入文本
        max_length: 最大长度限制

    Returns:
        清洗后的文本
    """
    if not text:
        return ""

    # 转换为字符串
    text = str(text)

    # 限制长度
    if len(text) > max_length:
        text = text[:max_length]

    # 移除控制字符（保留换行和制表符）
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)

    # 规范化Unicode字符
    text = text.strip()

    return text


def validate_category(category: str, max_length: int = 50) -> str:
    """验证分类名

    Args:
        category: 分类名
        max_length: 最大长度

    Returns:
        验证后的分类名

    Raises:
        ValueError: 分类名无效
    """
    if not category:
        raise ValueError("分类名不能为空")

    # 清洗并限制长度
    category = sanitize_text(category, max_length)

    # 验证字符集（只允许中文、英文、数字、下划线、短横线、空格）
    if not re.match(r"^[\u4e00-\u9fa5a-zA-Z0-9_\-\s]+$", category):
        raise ValueError("分类名只能包含中文、英文、数字、下划线、短横线和空格")

    return category.strip()


def validate_tag(tag: str, max_length: int = 20) -> str:
    """验证标签名

    Args:
        tag: 标签名
        max_length: 最大长度

    Returns:
        验证后的标签名

    Raises:
        ValueError: 标签名无效
    """
    if not tag:
        raise ValueError("标签名不能为空")

    # 清洗并限制长度
    tag = sanitize_text(tag, max_length)

    # 验证字符集（不允许空格）
    if not re.match(r"^[\u4e00-\u9fa5a-zA-Z0-9_\-]+$", tag):
        raise ValueError("标签名只能包含中文、英文、数字、下划线和短横线")

    return tag


def default_task_plan_date(now: datetime | None = None) -> str:
    """Return the default planned date used by CLI and web create flows."""
    current = now or datetime.now()
    target = current + timedelta(days=1) if current.hour >= 20 else current
    return target.strftime("%Y-%m-%d")


def validate_title(title: str, max_length: int = 200) -> str:
    """验证标题

    Args:
        title: 标题
        max_length: 最大长度

    Returns:
        验证后的标题

    Raises:
        ValueError: 标题无效
    """
    if not title:
        raise ValueError("标题不能为空")

    # 清洗并限制长度
    title = sanitize_text(title, max_length)

    return title.strip()


def sanitize_search_keyword(keyword: str) -> str:
    """清洗搜索关键词

    Args:
        keyword: 搜索关键词

    Returns:
        清洗后的关键词
    """
    if not keyword:
        return ""

    # 转成字符串并限制长度，避免把 IME 组合态或 FTS 操作符原样送进 MATCH
    keyword = sanitize_text(str(keyword), 100)

    # 移除/替换 FTS5 容易触发语法解析的字符，保留中文、字母、数字和空格检索
    keyword = re.sub(r"[\"'`*:(){}\[\]+\-]", " ", keyword)
    keyword = re.sub(r"\s+", " ", keyword).strip()

    # 限制长度
    if len(keyword) > 100:
        keyword = keyword[:100]

    return keyword.strip()


def normalize_diary_mood(value: Any) -> str:
    """Normalize diary mood into the canonical journal vocabulary."""
    if value in (None, ""):
        return ""
    mood = sanitize_text(str(value), 16).strip().lower()
    if not mood:
        return ""
    mood = DIARY_MOOD_ALIASES.get(mood, mood)
    if mood not in DIARY_MOOD_VALUES:
        allowed = ", ".join(sorted(DIARY_MOOD_VALUES))
        raise ValueError(f"Invalid diary mood: {mood}. Allowed values: {allowed}")
    return mood


def normalize_template_answers(value: Any) -> list[dict[str, str]]:
    """Normalize structured diary template answers."""
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise ValueError("Diary template_answers must be a list")

    answers: list[dict[str, str]] = []
    for row in value:
        if not isinstance(row, dict):
            raise ValueError("Diary template_answers must contain objects")
        prompt = sanitize_text(str(row.get("prompt") or ""), 300)
        answer = sanitize_text(str(row.get("answer") or ""), 50000)
        if not prompt and not answer:
            continue
        answers.append({"prompt": prompt, "answer": answer})
    return answers


def normalize_bool_flag(value: Any) -> bool:
    """Normalize form/API boolean-ish values without treating 'false' as true."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on", "是", "收藏"}


def validate_location(location: str, max_length: int = 200) -> str:
    """验证地点

    Args:
        location: 地点
        max_length: 最大长度

    Returns:
        验证后的地点
    """
    if not location:
        return ""

    # 清洗并限制长度
    location = sanitize_text(location, max_length)

    return location.strip()


def validate_priority(priority: Any) -> int:
    """验证优先级

    Args:
        priority: 优先级值

    Returns:
        验证后的优先级（1-5）

    Raises:
        ValueError: 优先级无效
    """
    try:
        normalized_priority = int(priority)
    except (ValueError, TypeError) as exc:
        raise ValueError("优先级必须是数字") from exc

    if not 1 <= normalized_priority <= 5:
        raise ValueError("优先级必须在1-5之间")

    return normalized_priority


def _normalize_tag_list(value: Any, *, field_name: str) -> list[str]:
    """验证标签列表并按不区分大小写的键稳定去重。"""
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise ValueError(f"{field_name} must be a list")

    tags: list[str] = []
    seen: set[str]  = set()
    for raw_tag in value:
        if raw_tag in (None, ""):
            continue
        tag = validate_tag(str(raw_tag))
        key = tag.casefold()
        if key not in seen:
            seen.add(key)
            tags.append(tag)
    return tags


def _normalize_common_fields(
    normalized: dict[str, Any],
    *,
    partial: bool,
    title_required: bool,
    content_required: bool = False,
) -> None:
    """原地规范所有条目共享的标题、正文、分类与标签。"""
    if "title" in normalized or not partial:
        title               = str(normalized.get("title") or "")
        normalized["title"] = validate_title(title) if title_required else sanitize_text(title, 200)

    if "content" in normalized or not partial:
        content = sanitize_text(str(normalized.get("content") or ""), 50000)
        if content_required and not content:
            raise ValueError("Diary content cannot be empty")
        normalized["content"] = content

    if "category" in normalized or not partial:
        category               = str(normalized.get("category") or PendoConfig.DEFAULT_CATEGORY)
        normalized["category"] = validate_category(category)

    if "tags" in normalized or not partial:
        normalized["tags"] = _normalize_tag_list(
            normalized.get("tags"),
            field_name="tags",
        )


def validate_item_data(data: dict[str, Any]) -> dict[str, Any]:
    """执行不补业务默认值的存储层校验。

    Handler 和 Web API 在构造条目时使用各类型的严格规范化器；数据库入口只负责
    拒绝未知字段并清洗通用字段，不能再次推导提醒或终态时间，否则会改变已经明确
    给出的业务语义。账目金额需要生成整数分字段，因此仍在此完成完整规范化。
    """
    item_type = str(data.get("type") or "").strip()
    if item_type in SUPPORTED_ITEM_TYPES:
        unknown = sorted(key for key in data if key not in get_allowed_item_fields(item_type))
        if unknown:
            raise ValueError(f"Unsupported {item_type} field: {', '.join(unknown)}")
        if item_type == "ledger":
            return normalize_ledger_fields(dict(data), partial=False)

    validated = {key: value for key, value in data.items() if value is not None}
    if "title" in data:
        validated["title"] = validate_title(data["title"])
    if "content" in data:
        validated["content"] = sanitize_text(data["content"], 50000)
    if data.get("category"):
        validated["category"] = validate_category(data["category"])
    if isinstance(data.get("tags"), list):
        validated["tags"] = [validate_tag(tag) for tag in data["tags"] if tag]
    if data.get("priority") is not None:
        validated["priority"] = validate_priority(data["priority"])
    if data.get("location"):
        validated["location"] = validate_location(data["location"])
    return validated


def _ledger_decimal_to_cents(amount: Any) -> int:
    """Convert a decimal money value to integer cents with one rounding rule."""
    if amount is None or amount == "":
        raise ValueError("Ledger amount is required")
    if isinstance(amount, str):
        amount = amount.replace("￥", "").replace("¥", "").replace(",", "").strip()
    try:
        value = Decimal(str(amount))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError("Ledger amount must be a valid number") from exc
    if not value.is_finite():
        raise ValueError("Ledger amount must be a finite number")
    try:
        cents = int((value * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except (InvalidOperation, OverflowError, ValueError) as exc:
        raise ValueError("Ledger amount must be a valid number") from exc
    return cents


def ledger_amount_to_cents(amount: Any) -> int:
    """Convert a positive user-facing money amount to integer cents."""
    cents = _ledger_decimal_to_cents(amount)
    if cents <= 0:
        raise ValueError("Ledger amount must be greater than 0")
    return cents


def ledger_amount_filter_to_cents(amount: Any) -> int:
    """Convert a non-negative money filter to integer cents."""
    cents = _ledger_decimal_to_cents(amount)
    if cents < 0:
        raise ValueError("Ledger amount filter must not be negative")
    return cents


def ledger_cents_to_amount(cents: Any) -> float:
    """Convert integer cents to a two-decimal float for display/API mirrors."""
    try:
        value = int(cents)
    except (TypeError, ValueError) as exc:
        raise ValueError("Ledger amount_cents must be an integer") from exc
    if value <= 0:
        raise ValueError("Ledger amount_cents must be greater than 0")
    return float((Decimal(value) / Decimal("100")).quantize(Decimal("0.01")))


def _clean_ledger_text(value: Any, default: str = "", max_length: int = 80) -> str:
    return sanitize_text(str(value or "").strip(), max_length) or default


def parse_ledger_transaction_type(value: Any) -> str | None:
    """把中英文交易类型别名解析成持久化枚举值。"""
    return LEDGER_TRANSACTION_TYPE_ALIASES.get(str(value or "").strip().lower())


def _normalize_ledger_amount(normalized: dict[str, Any], *, partial: bool) -> None:
    """把输入金额统一为整数分，并维护只用于展示的元镜像。"""
    has_cents  = "amount_cents" in normalized
    has_amount = "amount" in normalized
    if partial and not has_cents and not has_amount:
        return
    if has_cents and normalized.get("amount_cents") not in (None, ""):
        try:
            cents = int(normalized["amount_cents"])
        except (TypeError, ValueError) as exc:
            raise ValueError("Ledger amount_cents must be an integer") from exc
        if cents <= 0:
            raise ValueError("Ledger amount_cents must be greater than 0")
    elif has_amount:
        cents = ledger_amount_to_cents(normalized.get("amount"))
    else:
        raise ValueError("Ledger amount must be greater than 0")
    normalized["amount_cents"] = cents
    normalized["amount"]       = ledger_cents_to_amount(cents)


def _normalize_ledger_kind(normalized: dict[str, Any], *, partial: bool) -> str | None:
    """规范收支类型与账目分类，并返回本次可见的交易类型。"""
    transaction_type = normalized.get("transaction_type")
    if transaction_type is None and not partial:
        transaction_type = "expense"
    if transaction_type is not None:
        raw_type         = str(transaction_type or "").strip().lower()
        transaction_type = parse_ledger_transaction_type(raw_type) or raw_type
        if transaction_type not in LEDGER_TRANSACTION_TYPES:
            raise ValueError("Invalid ledger transaction type")
        normalized["transaction_type"] = transaction_type

    category = normalized.get("ledger_category")
    if category is None and not partial:
        category = "转账" if transaction_type == "transfer" else "其他"
    if category is not None:
        fallback                      = "转账" if transaction_type == "transfer" else "其他"
        normalized["ledger_category"] = _clean_ledger_text(category, fallback, 60)
    return transaction_type


def _normalize_ledger_date_and_currency(normalized: dict[str, Any], *, partial: bool) -> None:
    ledger_date = normalized.get("ledger_date")
    if not ledger_date and not partial:
        ledger_date = datetime.now().strftime("%Y-%m-%d")
    if ledger_date:
        try:
            normalized["ledger_date"] = datetime.strptime(str(ledger_date), "%Y-%m-%d").strftime(
                "%Y-%m-%d"
            )
        except ValueError as exc:
            raise ValueError("Invalid ledger_date, expected YYYY-MM-DD") from exc

    currency = normalized.get("currency")
    if currency is None and not partial:
        currency = LEDGER_DEFAULT_CURRENCY
    if currency is not None:
        code = str(currency or "").strip().upper() or LEDGER_DEFAULT_CURRENCY
        if not re.fullmatch(r"[A-Z]{3}", code):
            raise ValueError("Invalid ledger currency")
        normalized["currency"] = code


def _normalize_ledger_accounts(
    normalized: dict[str, Any],
    *,
    partial: bool,
    transaction_type: str | None,
) -> None:
    account = normalized.get("account_name")
    if account is None and not partial:
        account = LEDGER_DEFAULT_ACCOUNT
    if account is not None:
        normalized["account_name"] = _clean_ledger_text(account, LEDGER_DEFAULT_ACCOUNT, 80)

    counter = normalized.get("counter_account_name")
    if counter is not None:
        normalized["counter_account_name"] = _clean_ledger_text(counter, "", 80)
    elif not partial:
        normalized["counter_account_name"] = ""

    if transaction_type != "transfer":
        return
    account_name = _clean_ledger_text(normalized.get("account_name"), LEDGER_DEFAULT_ACCOUNT, 80)
    counter_name = _clean_ledger_text(normalized.get("counter_account_name"), "", 80)
    if not counter_name:
        raise ValueError("Ledger transfer requires counter_account_name")
    if account_name == counter_name:
        raise ValueError("Ledger transfer accounts must be different")
    normalized["account_name"]         = account_name
    normalized["counter_account_name"] = counter_name
    normalized["ledger_category"]      = _clean_ledger_text(
        normalized.get("ledger_category"), "转账", 60
    )


def _normalize_ledger_details(normalized: dict[str, Any], *, partial: bool) -> None:
    if "merchant" in normalized:
        normalized["merchant"] = _clean_ledger_text(normalized.get("merchant"), "", 120)
    elif not partial:
        normalized["merchant"] = ""

    if "remark" in normalized:
        normalized["remark"] = sanitize_text(str(normalized.get("remark") or "").strip(), 2000)
    elif not partial:
        normalized["remark"] = ""


def normalize_ledger_fields(data: dict[str, Any], partial: bool = False) -> dict[str, Any]:
    """规范账目字段；整数分是唯一计算字段，元值只是展示镜像。"""
    normalized = dict(data)
    _normalize_common_fields(normalized, partial=partial, title_required=False)
    _normalize_ledger_amount(normalized, partial=partial)
    transaction_type = _normalize_ledger_kind(normalized, partial=partial)
    _normalize_ledger_date_and_currency(normalized, partial=partial)
    _normalize_ledger_accounts(
        normalized,
        partial          = partial,
        transaction_type = transaction_type,
    )
    _normalize_ledger_details(normalized, partial=partial)
    return normalized


def _coerce_24_hour_iso_datetime(text: str) -> str:
    match = re.fullmatch(
        r"(\d{4}-\d{2}-\d{2})([T\s])24:00(?::00(?:\.0{1,6})?)?(Z|[+-]\d{2}:\d{2})?",
        text,
    )
    if not match:
        return text
    day = datetime.strptime(match.group(1), "%Y-%m-%d") + timedelta(days=1)
    tz_suffix = match.group(3) or ""
    if tz_suffix == "Z":
        tz_suffix = "+00:00"
    return f"{day.strftime('%Y-%m-%d')}T00:00:00{tz_suffix}"


def _normalize_iso_datetime(value: Any, field_name: str) -> str:
    """将输入规范化为秒级 ISO datetime。"""
    text = sanitize_text(str(value), 40)
    if not text:
        raise ValueError(f"{field_name} is required")
    text = _coerce_24_hour_iso_datetime(text)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"Invalid {field_name}, expected ISO datetime") from exc
    return parsed.isoformat(timespec="seconds")


def _datetime_for_rule_delta(value: Any, field_name: str) -> datetime:
    parsed = datetime.fromisoformat(_normalize_iso_datetime(value, field_name))
    if parsed.tzinfo is not None:
        return parsed.astimezone(DEFAULT_EVENT_TIMEZONE).replace(tzinfo=None)
    return parsed


def normalize_reminder_rules(value: Any) -> list[dict[str, int]]:
    """Normalize reminder rules into unique non-negative second offsets."""
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise ValueError("reminder_rules must be a list")

    offsets: set[int] = set()
    for row in value:
        if not isinstance(row, dict):
            raise ValueError("reminder_rules must contain objects")
        raw_offset = row.get("offset_seconds")
        if raw_offset is None:
            raise ValueError("reminder_rules.offset_seconds must be an integer")
        try:
            offset = int(raw_offset)
        except (TypeError, ValueError) as exc:
            raise ValueError("reminder_rules.offset_seconds must be an integer") from exc
        if offset < 0:
            raise ValueError("reminder_rules.offset_seconds must be non-negative")
        offsets.add(offset)

    return [{"offset_seconds": offset} for offset in sorted(offsets, reverse=True)]


def with_start_time_reminder_rule(rules: Any) -> list[dict[str, int]]:
    """Normalize rules and ensure the event start itself is included."""
    normalized = normalize_reminder_rules(rules)
    if not any(rule["offset_seconds"] == 0 for rule in normalized):
        normalized.append({"offset_seconds": 0})
    return normalize_reminder_rules(normalized)


def derive_reminder_rules(start_time: Any, remind_times: Any) -> list[dict[str, int]]:
    """Derive relative reminder rules from absolute reminder timestamps."""
    if not start_time or not remind_times:
        return []
    if not isinstance(remind_times, list):
        raise ValueError("remind_times must be a list")

    start_dt          = _datetime_for_rule_delta(start_time, "start_time")
    offsets: set[int] = set()
    for value in remind_times:
        if value in (None, ""):
            continue
        remind_dt = _datetime_for_rule_delta(value, "remind_times")
        offset    = int(round((start_dt - remind_dt).total_seconds()))
        if offset >= 0:
            offsets.add(offset)

    return [{"offset_seconds": offset} for offset in sorted(offsets, reverse=True)]


def build_remind_times_from_rules(start_time: Any, reminder_rules: Any) -> list[str]:
    """Build absolute reminder timestamps from a start time and relative rules."""
    if not start_time:
        return []
    rules = normalize_reminder_rules(reminder_rules)
    if not rules:
        return []

    start_dt     = datetime.fromisoformat(_normalize_iso_datetime(start_time, "start_time"))
    remind_times = [
        (start_dt - timedelta(seconds=rule["offset_seconds"])).isoformat(timespec="seconds")
        for rule in rules
    ]
    return sorted(dict.fromkeys(remind_times))


def _normalize_reminder_fields(
    normalized: dict[str, Any],
    *,
    anchor_field: str,
    partial: bool,
    include_anchor_rule: bool,
    default_rules: tuple[dict[str, int], ...] = (),
) -> None:
    """同步绝对提醒时间与相对规则，并保留显式清空语义。"""
    rules_provided = "reminder_rules" in normalized
    times_provided = "remind_times" in normalized

    if rules_provided or not partial:
        normalized["reminder_rules"] = normalize_reminder_rules(normalized.get("reminder_rules"))
    if times_provided or not partial:
        raw_times = normalized.get("remind_times") or []
        if not isinstance(raw_times, list):
            raise ValueError("remind_times must be a list")
        normalized["remind_times"] = sorted(
            {
                _normalize_iso_datetime(value, "remind_times")
                for value in raw_times
                if value not in (None, "")
            }
        )

    rules              = normalized.get("reminder_rules") or []
    times              = normalized.get("remind_times") or []
    explicitly_cleared = (rules_provided and not rules and not times) or (
        times_provided and not times and not rules
    )
    anchor = normalized.get(anchor_field)

    if rules and anchor:
        normalized["remind_times"] = build_remind_times_from_rules(anchor, rules)
    elif times and anchor:
        derived                      = derive_reminder_rules(anchor, times)
        normalized["reminder_rules"] = (
            with_start_time_reminder_rule(derived) if include_anchor_rule else derived
        )
        normalized["remind_times"] = build_remind_times_from_rules(
            anchor,
            normalized["reminder_rules"],
        )
    elif explicitly_cleared:
        normalized["reminder_rules"] = []
        normalized["remind_times"]   = []
    elif not partial and default_rules and anchor:
        normalized["reminder_rules"] = [dict(rule) for rule in default_rules]
        normalized["remind_times"]   = build_remind_times_from_rules(
            anchor,
            normalized["reminder_rules"],
        )


def _normalize_iso_date(value: Any, field_name: str) -> str:
    """将输入规范化为 YYYY-MM-DD。"""
    text = sanitize_text(str(value), 20)
    if not text:
        raise ValueError(f"{field_name} is required")
    try:
        return datetime.strptime(text, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError as exc:
        raise ValueError(f"Invalid {field_name}, expected YYYY-MM-DD") from exc


def _normalize_optional_iso_date(value: Any, field_name: str) -> str | None:
    if value in (None, ""):
        return None
    return _normalize_iso_date(value, field_name)


def _normalize_event_context_fields(normalized: dict[str, Any], *, partial: bool) -> None:
    """规范事件的地点、备注和时区。"""
    if "location" in normalized:
        normalized["location"] = validate_location(normalized.get("location") or "")
    elif not partial:
        normalized["location"] = ""

    if "notes" in normalized:
        normalized["notes"] = sanitize_text(normalized.get("notes") or "", 50000)
    elif not partial:
        normalized["notes"] = ""

    timezone = normalized.get("timezone")
    if timezone is None and not partial:
        timezone = PendoConfig.DEFAULT_TIMEZONE
    if timezone is not None:
        timezone_name = sanitize_text(str(timezone), 80) or PendoConfig.DEFAULT_TIMEZONE
        try:
            ZoneInfo(timezone_name)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("Invalid event timezone") from exc
        normalized["timezone"] = timezone_name


def _normalize_event_role_fields(normalized: dict[str, Any], *, partial: bool) -> None:
    """校验事件在集合图中的角色和集合类型。"""
    event_role = normalized.get("event_role")
    if event_role is None and not partial:
        event_role = "single"
    if event_role is not None:
        event_role = sanitize_text(str(event_role), 40) or "single"
        if event_role not in EVENT_ROLES:
            raise ValueError("Invalid event_role")
        normalized["event_role"] = event_role

    collection_kind = normalized.get("event_collection_kind")
    if collection_kind is not None:
        collection_kind = sanitize_text(str(collection_kind), 40)
        if collection_kind and collection_kind not in EVENT_COLLECTION_KINDS:
            raise ValueError("Invalid event_collection_kind")
        normalized["event_collection_kind"] = collection_kind or None


def _normalize_event_identity_fields(normalized: dict[str, Any]) -> None:
    """规范事件集合标识、节点标识和序号。"""
    for text_field in ("event_collection_id", "event_node_key", "source_item_id"):
        if text_field in normalized and normalized[text_field] is not None:
            normalized[text_field] = sanitize_text(str(normalized[text_field]), 120)

    if "event_index" in normalized and normalized.get("event_index") is not None:
        try:
            normalized["event_index"] = int(normalized["event_index"])
        except (TypeError, ValueError) as exc:
            raise ValueError("event_index must be an integer") from exc


def _normalize_event_time_fields(normalized: dict[str, Any]) -> None:
    """规范事件起止时间，并按真实时间顺序比较带偏移时间。"""
    start_time = normalized.get("start_time")
    if not start_time:
        raise ValueError("Event start_time is required")
    normalized["start_time"] = _normalize_iso_datetime(start_time, "start_time")

    end_time = normalized.get("end_time")
    if end_time in (None, ""):
        normalized["end_time"] = None
    else:
        normalized["end_time"] = _normalize_iso_datetime(end_time, "end_time")
        start_dt               = datetime.fromisoformat(normalized["start_time"])
        end_dt                 = datetime.fromisoformat(normalized["end_time"])
        if (start_dt.tzinfo is None) != (end_dt.tzinfo is None):
            raise ValueError("Event start_time and end_time must use matching timezone forms")
        if end_dt <= start_dt:
            raise ValueError("Event end_time must be after start_time")


def normalize_event_fields(data: dict[str, Any], partial: bool = False) -> dict[str, Any]:
    """规范化并验证 event 字段。"""
    normalized = dict(data)
    _normalize_common_fields(normalized, partial=partial, title_required=True)
    _normalize_event_context_fields(normalized, partial=partial)
    _normalize_event_role_fields(normalized, partial=partial)
    _normalize_event_identity_fields(normalized)
    _normalize_event_time_fields(normalized)
    _normalize_reminder_fields(
        normalized,
        anchor_field        = "start_time",
        partial             = partial,
        include_anchor_rule = True,
        default_rules       = DEFAULT_EVENT_REMINDER_RULES,
    )

    return normalized


def _reject_legacy_task_fields(normalized: dict[str, Any]) -> None:
    """拒绝已经退出持久化模型的旧待办字段。"""
    legacy_fields = LEGACY_TASK_FIELDS & normalized.keys()
    if legacy_fields:
        field_list = ", ".join(sorted(legacy_fields))
        raise ValueError(f"Unsupported legacy task field: {field_list}")


def _normalize_task_schedule_fields(normalized: dict[str, Any], *, partial: bool) -> None:
    """规范待办计划日期、截止时间和重复规则。"""
    plan_date = normalized.get("plan_date")
    if not partial or "plan_date" in normalized:
        normalized["plan_date"] = _normalize_optional_iso_date(plan_date, "plan_date")

    deadline_at = normalized.get("deadline_at")
    if not partial or "deadline_at" in normalized:
        normalized["deadline_at"] = (
            None
            if deadline_at in (None, "")
            else _normalize_iso_datetime(deadline_at, "deadline_at")
        )

    repeat_rule = normalized.get("repeat_rule")
    if repeat_rule in (None, ""):
        if not partial or "repeat_rule" in normalized:
            normalized["repeat_rule"] = None
    else:
        normalized["repeat_rule"] = sanitize_text(str(repeat_rule), 200)


def _normalize_task_priority_and_status(normalized: dict[str, Any], *, partial: bool) -> None:
    """规范待办优先级和生命周期状态。"""
    priority = normalized.get("priority")
    if priority is None and not partial:
        priority = 3
    if priority is not None:
        normalized["priority"] = validate_priority(priority)

    status = normalized.get("status")
    if status is None and not partial:
        status = "open"
    if status is not None:
        status = sanitize_text(str(status), 30)
        if status not in TASK_STATUSES:
            raise ValueError("Invalid task status")
        normalized["status"] = status


def _sync_task_terminal_timestamps(normalized: dict[str, Any]) -> None:
    """让完成/取消时间与待办终态保持一致。"""
    completed_at = normalized.get("completed_at")
    cancelled_at = normalized.get("cancelled_at")
    status_value = normalized.get("status")
    if status_value == "done":
        if completed_at in (None, ""):
            normalized["completed_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        else:
            normalized["completed_at"] = _normalize_iso_datetime(completed_at, "completed_at")
        normalized["cancelled_at"] = None
    elif status_value == "cancelled":
        normalized["completed_at"] = None
        if cancelled_at in (None, ""):
            normalized["cancelled_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        else:
            normalized["cancelled_at"] = _normalize_iso_datetime(cancelled_at, "cancelled_at")
    elif status_value is not None:
        normalized["completed_at"] = None
        normalized["cancelled_at"] = None


def normalize_task_fields(data: dict[str, Any], partial: bool = False) -> dict[str, Any]:
    """规范化并验证 task 字段。"""
    normalized = dict(data)
    _reject_legacy_task_fields(normalized)
    _normalize_common_fields(normalized, partial=partial, title_required=True)
    _normalize_task_schedule_fields(normalized, partial=partial)
    _normalize_task_priority_and_status(normalized, partial=partial)
    _normalize_reminder_fields(
        normalized,
        anchor_field        = "deadline_at",
        partial             = partial,
        include_anchor_rule = False,
    )
    _sync_task_terminal_timestamps(normalized)
    return normalized


def _check_note_reference_budget(normalized: dict[str, Any]) -> None:
    """在展开引用前限制原始 JSON 体积，防止小条目携带巨型关系图。"""
    payload = {key: normalized[key] for key in ("references", "related_items") if key in normalized}
    if not payload:
        return
    try:
        size = len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    except (TypeError, ValueError) as exc:
        raise ValueError("Note references must be JSON serializable") from exc
    if size > MAX_NOTE_REFERENCE_BYTES:
        raise ValueError(f"Note references exceed {MAX_NOTE_REFERENCE_BYTES} UTF-8 bytes")


def _normalize_note_references(value: Any) -> list[dict[str, str]]:
    """规范引用对象，并按引用 ID 稳定去重。"""
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise ValueError("Note references must be a list")
    if len(value) > MAX_NOTE_REFERENCES:
        raise ValueError(f"Note references cannot exceed {MAX_NOTE_REFERENCES} entries")

    references: list[dict[str, str]] = []
    seen: set[str]                   = set()
    for raw_reference in value:
        if not isinstance(raw_reference, dict):
            continue
        reference_id = sanitize_text(str(raw_reference.get("id") or ""), 120)
        if not reference_id or reference_id in seen:
            continue
        seen.add(reference_id)
        reference = {
            "kind": sanitize_text(str(raw_reference.get("kind") or "item"), 40) or "item",
            "id": reference_id,
        }
        for key, limit in (("type", 40), ("title", 200)):
            text = sanitize_text(str(raw_reference.get(key) or ""), limit)
            if text:
                reference[key] = text
        references.append(reference)
    return references


def _normalize_related_item_ids(value: Any) -> list[str]:
    """规范关联条目 ID，并保持首次出现顺序。"""
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise ValueError("Note related_items must be a list")
    if len(value) > MAX_NOTE_REFERENCES:
        raise ValueError(f"Note related_items cannot exceed {MAX_NOTE_REFERENCES} entries")

    related: list[str] = []
    seen: set[str]     = set()
    for raw_id in value:
        related_id = sanitize_text(str(raw_id or ""), 120)
        if related_id and related_id not in seen:
            seen.add(related_id)
            related.append(related_id)
    return related


def _merge_note_related_ids(
    related_items: list[str], references: list[dict[str, str]]
) -> list[str]:
    """把引用 ID 补入关联列表，并执行统一总量上限。"""
    merged = list(dict.fromkeys([*related_items, *(row["id"] for row in references)]))
    if len(merged) > MAX_NOTE_REFERENCES:
        raise ValueError(
            f"Note references and related_items cannot exceed {MAX_NOTE_REFERENCES} unique entries"
        )
    return merged


def normalize_note_fields(data: dict[str, Any], partial: bool = False) -> dict[str, Any]:
    """规范化并验证 note 字段。"""
    normalized = dict(data)
    _check_note_reference_budget(normalized)
    _normalize_common_fields(normalized, partial=partial, title_required=True)

    if "references" in normalized or not partial:
        normalized["references"] = _normalize_note_references(normalized.get("references"))
    if "related_items" in normalized or not partial:
        normalized["related_items"] = _normalize_related_item_ids(normalized.get("related_items"))
    if "references" in normalized:
        related_items               = normalized.get("related_items")
        normalized["related_items"] = _merge_note_related_ids(
            related_items if isinstance(related_items, list) else [],
            normalized["references"],
        )

    if "last_viewed" in normalized:
        last_viewed               = normalized.get("last_viewed")
        normalized["last_viewed"] = (
            _normalize_iso_datetime(last_viewed, "last_viewed")
            if last_viewed not in (None, "")
            else None
        )

    return normalized


def _normalize_diary_context_fields(normalized: dict[str, Any], *, partial: bool) -> None:
    """规范日记日期和地点。"""
    diary_date = normalized.get("diary_date")
    if not partial or "diary_date" in normalized:
        normalized["diary_date"] = _normalize_iso_date(diary_date, "diary_date")

    if "location" in normalized:
        normalized["location"] = validate_location(normalized.get("location") or "")
    elif not partial:
        normalized["location"] = ""


def _normalize_diary_mood_fields(normalized: dict[str, Any], *, partial: bool) -> None:
    """规范日记心情、天气和心情分数。"""
    mood = normalized.get("mood")
    if not partial or "mood" in normalized:
        normalized["mood"] = normalize_diary_mood(mood)

    weather = normalized.get("weather")
    if weather in (None, ""):
        normalized["weather"] = ""
    elif weather is not None:
        normalized["weather"] = sanitize_text(str(weather), 32)

    mood_score = normalized.get("mood_score")
    if mood_score in (None, ""):
        normalized["mood_score"] = None
    elif mood_score is not None:
        try:
            score = int(mood_score)
        except (TypeError, ValueError) as exc:
            raise ValueError("Diary mood_score must be an integer") from exc
        if not 1 <= score <= 10:
            raise ValueError("Diary mood_score must be between 1 and 10")
        normalized["mood_score"] = score


def _normalize_diary_template_fields(normalized: dict[str, Any], *, partial: bool) -> None:
    """规范日记模板、记录时间和收藏状态。"""
    template_id = normalized.get("template_id")
    if template_id in (None, ""):
        normalized["template_id"] = None
    elif template_id is not None:
        normalized["template_id"] = sanitize_text(str(template_id), 80) or None

    entry_time = normalized.get("entry_time")
    if not partial or "entry_time" in normalized:
        if entry_time in (None, ""):
            normalized["entry_time"] = None
        else:
            normalized["entry_time"] = _normalize_iso_datetime(entry_time, "entry_time")

    if not partial or "template_answers" in normalized:
        normalized["template_answers"] = normalize_template_answers(
            normalized.get("template_answers")
        )

    if "is_favorite" in normalized:
        normalized["is_favorite"] = normalize_bool_flag(normalized.get("is_favorite"))
    elif not partial:
        normalized["is_favorite"] = False


def normalize_diary_fields(data: dict[str, Any], partial: bool = False) -> dict[str, Any]:
    """规范化并验证 diary 字段。"""
    normalized = dict(data)
    _normalize_common_fields(
        normalized,
        partial          = partial,
        title_required   = False,
        content_required = True,
    )
    _normalize_diary_context_fields(normalized, partial=partial)
    _normalize_diary_mood_fields(normalized, partial=partial)
    _normalize_diary_template_fields(normalized, partial=partial)
    return normalized


ItemNormalizer: TypeAlias = Callable[[dict[str, Any], bool], dict[str, Any]]
ITEM_NORMALIZERS: Final[dict[str, ItemNormalizer]] = {
    "event": normalize_event_fields,
    "task": normalize_task_fields,
    "note": normalize_note_fields,
    "diary": normalize_diary_fields,
    "ledger": normalize_ledger_fields,
}


def get_item_normalizer(item_type: str) -> ItemNormalizer | None:
    """Return the strict field normalizer for a supported item type."""
    return ITEM_NORMALIZERS.get(str(item_type or "").strip())


def get_allowed_item_fields(item_type: str) -> set[str]:
    """Return the complete top-level field set accepted for a persisted item."""
    item_type = str(item_type or "").strip()
    return set(COMMON_ITEM_FIELDS) | set(TYPE_SPECIFIC_ITEM_FIELDS.get(item_type, set()))


def normalize_item_fields(data: dict[str, Any], partial: bool = False) -> dict[str, Any]:
    """Normalize a full item payload and reject fields outside the new schema."""
    item_type  = str(data.get("type") or "").strip()
    normalizer = get_item_normalizer(item_type)
    if not normalizer:
        raise ValueError(f"Unsupported record type: {item_type}")

    allowed = get_allowed_item_fields(item_type)
    unknown = sorted(key for key in data if key not in allowed)
    if unknown:
        raise ValueError(f"Unsupported {item_type} field: {', '.join(unknown)}")

    payload         = {key: value for key, value in data.items() if key in allowed}
    payload["type"] = item_type
    return normalizer(payload, partial)
