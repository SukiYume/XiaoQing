"""
输入验证和清洗工具

提供统一的输入验证功能，确保数据安全性和一致性
"""
from datetime import datetime, timedelta
import re
from typing import Any, Optional

DEFAULT_EVENT_REMINDER_RULES = [{"offset_seconds": 0}]


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


def default_task_category(now: Optional[datetime] = None) -> str:
    """Return the default task date category used by CLI and web create flows."""
    current = now or datetime.now()
    target = current + timedelta(days=1) if current.hour >= 20 else current
    return target.strftime("%Y-%m-%d")


def _coerce_datetime(value: Any) -> Optional[datetime]:
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


def is_empty_task_category(category: Any) -> bool:
    text = str(category or "").strip()
    return not text or text == "未分类"


def derive_task_category(
    category: Any,
    due_time: Any = None,
    reference_time: Any = None,
) -> str:
    """Resolve task category into either a date bucket or user-provided text."""
    text = str(category or "").strip()
    due_text = str(due_time or "").strip()
    if due_text and (is_empty_task_category(text) or is_date_category(text)):
        return due_text[:10]
    if text and text != "未分类":
        return text
    reference_dt = _coerce_datetime(reference_time)
    return default_task_category(reference_dt)

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
    except (ValueError, TypeError):
        raise ValueError("优先级必须是数字")
    
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


def normalize_ledger_fields(data: dict[str, Any], partial: bool = False) -> dict[str, Any]:
    """规范化并验证 ledger 字段。"""
    normalized = dict(data)

    amount = normalized.get("amount")
    if not partial or "amount" in normalized:
        if amount is None or float(amount) <= 0:
            raise ValueError("Ledger amount must be greater than 0")
        normalized["amount"] = float(amount)

    direction = normalized.get("direction")
    if direction is None and not partial:
        direction = "expense"
    if direction is not None:
        if direction not in {"expense", "income"}:
            raise ValueError("Invalid ledger direction")
        normalized["direction"] = direction

    category = normalized.get("ledger_category")
    if category is None and not partial:
        category = "其他"
    if category is not None:
        normalized["ledger_category"] = str(category).strip() or "其他"

    ledger_date = normalized.get("ledger_date")
    if not ledger_date and not partial:
        ledger_date = datetime.now().strftime("%Y-%m-%d")
    if ledger_date:
        try:
            normalized["ledger_date"] = datetime.strptime(str(ledger_date), "%Y-%m-%d").strftime("%Y-%m-%d")
        except ValueError as exc:
            raise ValueError("Invalid ledger_date, expected YYYY-MM-DD") from exc

    return normalized


def _normalize_iso_datetime(value: Any, field_name: str) -> str:
    """将输入规范化为秒级 ISO datetime。"""
    text = sanitize_text(str(value), 40)
    if not text:
        raise ValueError(f"{field_name} is required")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"Invalid {field_name}, expected ISO datetime") from exc
    return parsed.isoformat(timespec="seconds")


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

    start_dt = datetime.fromisoformat(_normalize_iso_datetime(start_time, "start_time"))
    offsets: set[int] = set()
    for value in remind_times:
        if value in (None, ""):
            continue
        remind_dt = datetime.fromisoformat(_normalize_iso_datetime(value, "remind_times"))
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

    for legacy_field in ("rrule", "parent_id", "remind_policy_id", "milestones"):
        normalized.pop(legacy_field, None)

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

    title = normalized.get("title")
    if not partial or title is not None:
        normalized["title"] = validate_title(title or "")

    due_time = normalized.get("due_time")
    if due_time in (None, ""):
        normalized["due_time"] = None
    elif due_time is not None:
        normalized["due_time"] = _normalize_iso_datetime(due_time, "due_time")

    category = normalized.get("category")
    if not partial or category is not None:
        normalized["category"] = validate_category(
            derive_task_category(category, normalized.get("due_time"), normalized.get("created_at"))
        )

    if "content" in normalized:
        normalized["content"] = sanitize_text(normalized.get("content") or "", 50000)
    elif not partial:
        normalized["content"] = ""

    priority = normalized.get("priority")
    if priority is None and not partial:
        priority = 3
    if priority is not None:
        normalized["priority"] = validate_priority(priority)

    status = normalized.get("status")
    if status is None and not partial:
        status = "todo"
    if status is not None:
        status = sanitize_text(str(status), 30)
        if status not in {"todo", "in_progress", "done", "cancelled"}:
            raise ValueError("Invalid task status")
        normalized["status"] = status

    progress = normalized.get("progress")
    if progress is not None:
        try:
            progress_value = int(progress)
        except (TypeError, ValueError) as exc:
            raise ValueError("Task progress must be an integer") from exc
        if not 0 <= progress_value <= 100:
            raise ValueError("Task progress must be between 0 and 100")
        normalized["progress"] = progress_value

    if "estimate" in normalized and normalized.get("estimate") not in (None, ""):
        try:
            estimate = int(normalized["estimate"])
        except (TypeError, ValueError) as exc:
            raise ValueError("Task estimate must be an integer") from exc
        if estimate < 0:
            raise ValueError("Task estimate must be non-negative")
        normalized["estimate"] = estimate

    completed_at = normalized.get("completed_at")
    status_value = normalized.get("status")
    if status_value in {"done", "cancelled"}:
        if completed_at in (None, ""):
            normalized["completed_at"] = datetime.now().isoformat(timespec="seconds")
        else:
            normalized["completed_at"] = _normalize_iso_datetime(completed_at, "completed_at")
    elif status_value is not None:
        normalized["completed_at"] = None

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
    if mood in (None, ""):
        normalized["mood"] = ""
    elif mood is not None:
        normalized["mood"] = sanitize_text(str(mood), 16)

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

    return normalized
