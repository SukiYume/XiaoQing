"""
输入验证和清洗工具

提供统一的输入验证功能，确保数据安全性和一致性
"""
import re
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from ..config import PendoConfig

DEFAULT_EVENT_REMINDER_RULES = [{"offset_seconds": 0}]
DEFAULT_EVENT_TIMEZONE = ZoneInfo(PendoConfig.DEFAULT_TIMEZONE)

LEDGER_TRANSACTION_TYPES = {"expense", "income", "transfer"}
LEDGER_DEFAULT_ACCOUNT = "现金"
LEDGER_DEFAULT_CURRENCY = "CNY"

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
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', text)

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
    if not re.match(r'^[\u4e00-\u9fa5a-zA-Z0-9_\-\s]+$', category):
        raise ValueError("分类名只能包含中文、英文、数字、下划线、短横线和空格")

    return category.strip()


def is_date_category(category: Any) -> bool:
    return bool(re.match(r"^\d{4}-\d{2}-\d{2}$", str(category or "").strip()))

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
    if not re.match(r'^[\u4e00-\u9fa5a-zA-Z0-9_\-]+$', tag):
        raise ValueError("标签名只能包含中文、英文、数字、下划线和短横线")

    return tag


def default_task_plan_date(now: datetime | None = None) -> str:
    """Return the default planned date used by CLI and web create flows."""
    current = now or datetime.now()
    target = current + timedelta(days=1) if current.hour >= 20 else current
    return target.strftime("%Y-%m-%d")


def _coerce_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if value in (None, ""):
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


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


def validate_diary_content(content: str, max_length: int = 50000) -> str:
    """验证日记内容

    Args:
        content: 日记内容
        max_length: 最大长度

    Returns:
        验证后的内容
    """
    if not content:
        return ""

    # 清洗并限制长度
    content = sanitize_text(content, max_length)

    return content

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
        priority = int(priority)
    except (ValueError, TypeError) as exc:
        raise ValueError("优先级必须是数字") from exc

    if not 1 <= priority <= 5:
        raise ValueError("优先级必须在1-5之间")

    return priority

def validate_item_data(data: dict[str, Any]) -> dict[str, Any]:
    """验证条目数据

    Args:
        data: 条目数据字典

    Returns:
        验证后的数据字典

    Raises:
        ValueError: 数据无效
    """
    item_type = str(data.get("type") or "").strip()
    if item_type in SUPPORTED_ITEM_TYPES:
        unknown = sorted(key for key in data if key not in get_allowed_item_fields(item_type))
        if unknown:
            raise ValueError(f"Unsupported {item_type} field: {', '.join(unknown)}")
        if item_type == "ledger":
            return normalize_ledger_fields(dict(data), partial=False)

    validated = {}

    # 验证标题
    if 'title' in data:
        validated['title'] = validate_title(data['title'])

    # 验证内容
    if 'content' in data:
        validated['content'] = sanitize_text(data['content'], 50000)

    # 验证分类
    if 'category' in data and data['category']:
        validated['category'] = validate_category(data['category'])

    # 验证标签
    if 'tags' in data and isinstance(data['tags'], list):
        validated['tags'] = [validate_tag(tag) for tag in data['tags'] if tag]

    # 验证优先级
    if 'priority' in data and data['priority'] is not None:
        validated['priority'] = validate_priority(data['priority'])

    # 验证地点
    if 'location' in data and data['location']:
        validated['location'] = validate_location(data['location'])

    # 复制其他字段
    for key, value in data.items():
        if key not in validated and value is not None:
            validated[key] = value

    return validated


def ledger_amount_to_cents(amount: Any) -> int:
    """Convert a user-facing money amount to integer cents."""
    if amount is None or amount == "":
        raise ValueError("Ledger amount is required")
    if isinstance(amount, str):
        amount = amount.replace("￥", "").replace("¥", "").replace(",", "").strip()
    try:
        value = Decimal(str(amount))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError("Ledger amount must be a valid number") from exc
    cents = int((value * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    if cents <= 0:
        raise ValueError("Ledger amount must be greater than 0")
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


def _normalize_ledger_type(value: Any) -> str:
    text = str(value or "").strip().lower()
    aliases = {
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
    return aliases.get(text, text)


def normalize_ledger_fields(data: dict[str, Any], partial: bool = False) -> dict[str, Any]:
    """规范化并验证 ledger 字段。

    `amount_cents` is the canonical money field. `amount` is a decimal
    display mirror generated from cents.
    """
    normalized = dict(data)

    has_amount_cents = "amount_cents" in normalized
    has_amount = "amount" in normalized
    if not partial or has_amount_cents or has_amount:
        if has_amount_cents and normalized.get("amount_cents") not in (None, ""):
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
        normalized["amount"] = ledger_cents_to_amount(cents)

    tx_type = normalized.get("transaction_type")
    if tx_type is None and not partial:
        tx_type = "expense"
    if tx_type is not None:
        tx_type = _normalize_ledger_type(tx_type)
        if tx_type not in LEDGER_TRANSACTION_TYPES:
            raise ValueError("Invalid ledger transaction type")
        normalized["transaction_type"] = tx_type

    effective_type = normalized.get("transaction_type")
    category = normalized.get("ledger_category")
    if category is None and not partial:
        category = "转账" if effective_type == "transfer" else "其他"
    if category is not None:
        default_category = "转账" if effective_type == "transfer" else "其他"
        normalized["ledger_category"] = _clean_ledger_text(category, default_category, 60)

    ledger_date = normalized.get("ledger_date")
    if not ledger_date and not partial:
        ledger_date = datetime.now().strftime("%Y-%m-%d")
    if ledger_date:
        try:
            normalized["ledger_date"] = datetime.strptime(str(ledger_date), "%Y-%m-%d").strftime("%Y-%m-%d")
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

    if effective_type == "transfer":
        account_name = _clean_ledger_text(normalized.get("account_name"), LEDGER_DEFAULT_ACCOUNT, 80)
        counter_name = _clean_ledger_text(normalized.get("counter_account_name"), "", 80)
        if not counter_name:
            raise ValueError("Ledger transfer requires counter_account_name")
        if account_name == counter_name:
            raise ValueError("Ledger transfer accounts must be different")
        normalized["account_name"] = account_name
        normalized["counter_account_name"] = counter_name
        normalized["ledger_category"] = _clean_ledger_text(
            normalized.get("ledger_category"), "转账", 60
        )

    if "merchant" in normalized:
        normalized["merchant"] = _clean_ledger_text(normalized.get("merchant"), "", 120)
    elif not partial:
        normalized["merchant"] = ""

    if "remark" in normalized:
        normalized["remark"] = sanitize_text(str(normalized.get("remark") or "").strip(), 2000)
    elif not partial:
        normalized["remark"] = ""

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

    start_dt = _datetime_for_rule_delta(start_time, "start_time")
    offsets: set[int] = set()
    for value in remind_times:
        if value in (None, ""):
            continue
        remind_dt = _datetime_for_rule_delta(value, "remind_times")
        offset = int(round((start_dt - remind_dt).total_seconds()))
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

    start_dt = datetime.fromisoformat(_normalize_iso_datetime(start_time, "start_time"))
    remind_times = [
        (start_dt - timedelta(seconds=rule["offset_seconds"])).isoformat(timespec="seconds")
        for rule in rules
    ]
    return sorted(dict.fromkeys(remind_times))


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


def normalize_event_fields(data: dict[str, Any], partial: bool = False) -> dict[str, Any]:
    """规范化并验证 event 字段。"""
    normalized = dict(data)
    reminder_rules_provided = "reminder_rules" in normalized
    remind_times_provided = "remind_times" in normalized

    title = normalized.get("title")
    if not partial or title is not None:
        normalized["title"] = validate_title(title or "")

    category = normalized.get("category")
    if category is None and not partial:
        category = "未分类"
    if category is not None:
        normalized["category"] = validate_category(category or "未分类")

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
        timezone = "Asia/Shanghai"
    if timezone is not None:
        normalized["timezone"] = sanitize_text(str(timezone), 80) or "Asia/Shanghai"

    event_role = normalized.get("event_role")
    if event_role is None and not partial:
        event_role = "single"
    if event_role is not None:
        event_role = sanitize_text(str(event_role), 40) or "single"
        if event_role not in {"single", "multi_node_child", "recurring_occurrence"}:
            raise ValueError("Invalid event_role")
        normalized["event_role"] = event_role

    collection_kind = normalized.get("event_collection_kind")
    if collection_kind is not None:
        collection_kind = sanitize_text(str(collection_kind), 40)
        if collection_kind and collection_kind not in {"multi_node", "recurring"}:
            raise ValueError("Invalid event_collection_kind")
        normalized["event_collection_kind"] = collection_kind or None

    for text_field in ("event_collection_id", "event_node_key", "source_item_id"):
        if text_field in normalized and normalized[text_field] is not None:
            normalized[text_field] = sanitize_text(str(normalized[text_field]), 120)

    if "event_index" in normalized and normalized.get("event_index") is not None:
        try:
            normalized["event_index"] = int(normalized["event_index"])
        except (TypeError, ValueError) as exc:
            raise ValueError("event_index must be an integer") from exc

    reminders = normalized.get("remind_times")
    if reminders is None and not partial:
        reminders = []
    if reminders is not None:
        if not isinstance(reminders, list):
            raise ValueError("remind_times must be a list")
        normalized["remind_times"] = sorted({
            _normalize_iso_datetime(value, "remind_times")
            for value in reminders
            if value not in (None, "")
        })

    rules = normalized.get("reminder_rules")
    if rules is None and not partial:
        rules = []
    if rules is not None:
        normalized["reminder_rules"] = normalize_reminder_rules(rules)

    start_time = normalized.get("start_time")
    if not start_time:
        raise ValueError("Event start_time is required")
    normalized["start_time"] = _normalize_iso_datetime(start_time, "start_time")

    end_time = normalized.get("end_time")
    if end_time in (None, ""):
        normalized["end_time"] = None
    else:
        normalized["end_time"] = _normalize_iso_datetime(end_time, "end_time")
        if normalized["end_time"] < normalized["start_time"]:
            raise ValueError("Event end_time must be after start_time")

    has_rules = bool(normalized.get("reminder_rules"))
    has_reminders = bool(normalized.get("remind_times"))
    explicitly_cleared = (
        (reminder_rules_provided and not has_rules and (not remind_times_provided or not has_reminders))
        or (remind_times_provided and not has_reminders and (not reminder_rules_provided or not has_rules))
    )

    if has_rules:
        normalized["remind_times"] = build_remind_times_from_rules(
            normalized["start_time"],
            normalized["reminder_rules"],
        )
    elif has_reminders:
        normalized["reminder_rules"] = with_start_time_reminder_rule(
            derive_reminder_rules(
                normalized["start_time"],
                normalized["remind_times"],
            )
        )
        normalized["remind_times"] = build_remind_times_from_rules(
            normalized["start_time"],
            normalized["reminder_rules"],
        )
    elif explicitly_cleared:
        normalized["reminder_rules"] = []
        normalized["remind_times"] = []
    elif not partial:
        normalized["reminder_rules"] = [dict(rule) for rule in DEFAULT_EVENT_REMINDER_RULES]
        normalized["remind_times"] = build_remind_times_from_rules(
            normalized["start_time"],
            normalized["reminder_rules"],
        )

    return normalized


def normalize_task_fields(data: dict[str, Any], partial: bool = False) -> dict[str, Any]:
    """规范化并验证 task 字段。"""
    normalized = dict(data)
    legacy_fields = {"due_time", "estimate", "subtasks", "dependencies", "progress"} & normalized.keys()
    if legacy_fields:
        field_list = ", ".join(sorted(legacy_fields))
        raise ValueError(f"Unsupported legacy task field: {field_list}")
    reminder_rules_provided = "reminder_rules" in normalized
    remind_times_provided = "remind_times" in normalized

    title = normalized.get("title")
    if not partial or title is not None:
        normalized["title"] = validate_title(title or "")

    plan_date = normalized.get("plan_date")
    if not partial or "plan_date" in normalized:
        normalized["plan_date"] = _normalize_optional_iso_date(plan_date, "plan_date")

    category = normalized.get("category")
    if category is None and not partial:
        category = "未分类"
    if category is not None:
        normalized["category"] = validate_category(category or "未分类")

    if "content" in normalized:
        normalized["content"] = sanitize_text(normalized.get("content") or "", 50000)
    elif not partial:
        normalized["content"] = ""

    deadline_at = normalized.get("deadline_at")
    if not partial or "deadline_at" in normalized:
        normalized["deadline_at"] = (
            None if deadline_at in (None, "") else _normalize_iso_datetime(deadline_at, "deadline_at")
        )

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
        if status not in {"open", "done", "cancelled"}:
            raise ValueError("Invalid task status")
        normalized["status"] = status

    reminders = normalized.get("remind_times")
    if reminders is None and not partial:
        reminders = []
    if reminders is not None:
        if not isinstance(reminders, list):
            raise ValueError("remind_times must be a list")
        normalized["remind_times"] = sorted({
            _normalize_iso_datetime(value, "remind_times")
            for value in reminders
            if value not in (None, "")
        })

    rules = normalized.get("reminder_rules")
    if rules is None and not partial:
        rules = []
    if rules is not None:
        normalized["reminder_rules"] = normalize_reminder_rules(rules)

    repeat_rule = normalized.get("repeat_rule")
    if repeat_rule in (None, ""):
        if not partial or "repeat_rule" in normalized:
            normalized["repeat_rule"] = None
    elif repeat_rule is not None:
        normalized["repeat_rule"] = sanitize_text(str(repeat_rule), 200)

    has_rules = bool(normalized.get("reminder_rules"))
    has_reminders = bool(normalized.get("remind_times"))
    deadline = normalized.get("deadline_at")
    explicitly_cleared = (
        (reminder_rules_provided and not has_rules and (not remind_times_provided or not has_reminders))
        or (remind_times_provided and not has_reminders and (not reminder_rules_provided or not has_rules))
    )
    if has_rules and deadline:
        normalized["remind_times"] = build_remind_times_from_rules(deadline, normalized["reminder_rules"])
    elif has_reminders and deadline:
        normalized["reminder_rules"] = derive_reminder_rules(deadline, normalized["remind_times"])
    elif explicitly_cleared:
        normalized["reminder_rules"] = []
        normalized["remind_times"] = []

    completed_at = normalized.get("completed_at")
    cancelled_at = normalized.get("cancelled_at")
    status_value = normalized.get("status")
    if status_value == "done":
        if completed_at in (None, ""):
            normalized["completed_at"] = datetime.now().isoformat(timespec="seconds")
        else:
            normalized["completed_at"] = _normalize_iso_datetime(completed_at, "completed_at")
        normalized["cancelled_at"] = None
    elif status_value == "cancelled":
        normalized["completed_at"] = None
        if cancelled_at in (None, ""):
            normalized["cancelled_at"] = datetime.now().isoformat(timespec="seconds")
        else:
            normalized["cancelled_at"] = _normalize_iso_datetime(cancelled_at, "cancelled_at")
    elif status_value is not None:
        normalized["completed_at"] = None
        normalized["cancelled_at"] = None

    return normalized


def normalize_note_fields(data: dict[str, Any], partial: bool = False) -> dict[str, Any]:
    """规范化并验证 note 字段。"""
    normalized = dict(data)

    title = normalized.get("title")
    if not partial or title is not None:
        normalized["title"] = validate_title(title or "")

    category = normalized.get("category")
    if category is None and not partial:
        category = "未分类"
    if category is not None:
        normalized["category"] = validate_category(category or "未分类")

    if "content" in normalized:
        normalized["content"] = sanitize_text(normalized.get("content") or "", 50000)
    elif not partial:
        normalized["content"] = ""

    tags = normalized.get("tags")
    if tags is None and not partial:
        tags = []
    if tags is not None:
        if not isinstance(tags, list):
            raise ValueError("Note tags must be a list")
        clean_tags: list[str] = []
        seen: set[str] = set()
        for tag in tags:
            if not tag:
                continue
            validated = validate_tag(tag)
            key = validated.lower()
            if key in seen:
                continue
            seen.add(key)
            clean_tags.append(validated)
        normalized["tags"] = clean_tags

    references = normalized.get("references")
    if references is None and not partial:
        references = []
    if references is not None:
        if not isinstance(references, list):
            raise ValueError("Note references must be a list")
        clean_refs: list[dict[str, str]] = []
        seen_refs: set[str] = set()
        for ref in references:
            if not isinstance(ref, dict):
                continue
            ref_id = sanitize_text(str(ref.get("id") or ""), 120)
            if not ref_id or ref_id in seen_refs:
                continue
            seen_refs.add(ref_id)
            clean_ref = {
                "kind": sanitize_text(str(ref.get("kind") or "item"), 40) or "item",
                "id": ref_id,
            }
            ref_type = sanitize_text(str(ref.get("type") or ""), 40)
            ref_title = sanitize_text(str(ref.get("title") or ""), 200)
            if ref_type:
                clean_ref["type"] = ref_type
            if ref_title:
                clean_ref["title"] = ref_title
            clean_refs.append(clean_ref)
        normalized["references"] = clean_refs

    related_items = normalized.get("related_items")
    if related_items is None and not partial:
        related_items = []
    if related_items is not None:
        if not isinstance(related_items, list):
            raise ValueError("Note related_items must be a list")
        clean_related: list[str] = []
        seen_related: set[str] = set()
        for value in related_items:
            related_id = sanitize_text(str(value or ""), 120)
            if not related_id or related_id in seen_related:
                continue
            seen_related.add(related_id)
            clean_related.append(related_id)
        normalized["related_items"] = clean_related

    if "references" in normalized:
        ref_ids = [
            str(ref.get("id") or "")
            for ref in normalized.get("references", [])
            if isinstance(ref, dict) and ref.get("id")
        ]
        existing_related = normalized.get("related_items") if isinstance(normalized.get("related_items"), list) else []
        merged_related: list[str] = []
        seen_merged: set[str] = set()
        for related_id in [*existing_related, *ref_ids]:
            clean_id = sanitize_text(str(related_id or ""), 120)
            if clean_id and clean_id not in seen_merged:
                seen_merged.add(clean_id)
                merged_related.append(clean_id)
        normalized["related_items"] = merged_related

    if "last_viewed" in normalized:
        last_viewed = normalized.get("last_viewed")
        normalized["last_viewed"] = (
            _normalize_iso_datetime(last_viewed, "last_viewed")
            if last_viewed not in (None, "")
            else None
        )

    return normalized


def normalize_diary_fields(data: dict[str, Any], partial: bool = False) -> dict[str, Any]:
    """规范化并验证 diary 字段。"""
    normalized = dict(data)

    diary_date = normalized.get("diary_date")
    if not partial or "diary_date" in normalized:
        normalized["diary_date"] = _normalize_iso_date(diary_date, "diary_date")

    title = normalized.get("title")
    if title is not None:
        normalized["title"] = sanitize_text(str(title), 200)
    elif not partial:
        normalized["title"] = ""

    content = normalized.get("content")
    if not partial or "content" in normalized:
        normalized["content"] = validate_diary_content(content or "")
        if not normalized["content"]:
            raise ValueError("Diary content cannot be empty")

    if "location" in normalized:
        normalized["location"] = validate_location(normalized.get("location") or "")
    elif not partial:
        normalized["location"] = ""

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
        normalized["template_answers"] = normalize_template_answers(normalized.get("template_answers"))

    if "is_favorite" in normalized:
        normalized["is_favorite"] = normalize_bool_flag(normalized.get("is_favorite"))
    elif not partial:
        normalized["is_favorite"] = False

    return normalized


def get_item_normalizer(item_type: str):
    """Return the strict field normalizer for a supported item type."""
    return {
        "event": normalize_event_fields,
        "task": normalize_task_fields,
        "note": normalize_note_fields,
        "diary": normalize_diary_fields,
        "ledger": normalize_ledger_fields,
    }.get(str(item_type or "").strip())


def get_allowed_item_fields(item_type: str) -> set[str]:
    """Return the complete top-level field set accepted for a persisted item."""
    item_type = str(item_type or "").strip()
    return set(COMMON_ITEM_FIELDS) | set(TYPE_SPECIFIC_ITEM_FIELDS.get(item_type, set()))


def normalize_item_fields(data: dict[str, Any], partial: bool = False) -> dict[str, Any]:
    """Normalize a full item payload and reject fields outside the new schema."""
    item_type = str(data.get("type") or "").strip()
    normalizer = get_item_normalizer(item_type)
    if not normalizer:
        raise ValueError(f"Unsupported record type: {item_type}")

    allowed = get_allowed_item_fields(item_type)
    unknown = sorted(key for key in data if key not in allowed)
    if unknown:
        raise ValueError(f"Unsupported {item_type} field: {', '.join(unknown)}")

    payload = {key: value for key, value in data.items() if key in allowed}
    payload["type"] = item_type
    return normalizer(payload, partial=partial)
