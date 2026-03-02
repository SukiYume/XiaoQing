"""
时间与格式化工具（精简版）
合并 time_parser, timezone_helper, helpers 核心功能
"""
import json
import re
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, Any
from zoneinfo import ZoneInfo

from ..config import PendoConfig
from .formatters import ItemFormatter as _ItemFormatter

logger = logging.getLogger(__name__)

# ==================== 时区辅助 ====================

class TimezoneHelper:
    """时区辅助类"""
    
    DEFAULT_TZ = ZoneInfo(PendoConfig.DEFAULT_TIMEZONE)
    
    @staticmethod
    def get_user_timezone(user_id: str, db=None) -> ZoneInfo:
        """获取用户时区"""
        if db:
            try:
                settings = db.settings.get_user_settings(user_id)
                tz_str = settings.get('timezone', PendoConfig.DEFAULT_TIMEZONE)
                return ZoneInfo(tz_str)
            except Exception as e:
                logger.warning("Failed to get user timezone: %s", e)
        return TimezoneHelper.DEFAULT_TZ
    
    @staticmethod
    def now(tz: Optional[ZoneInfo] = None) -> datetime:
        """获取带时区的当前时间"""
        return datetime.now(tz or TimezoneHelper.DEFAULT_TZ)
    
    @staticmethod
    def parse(dt_str: str, tz: Optional[ZoneInfo] = None) -> datetime:
        """解析日期时间字符串并附加时区"""
        if not dt_str:
            raise ValueError("Empty datetime string")
        
        try:
            # M-7修复：直接解析后检查 tzinfo，不依赖字符串计数
            dt = datetime.fromisoformat(dt_str.replace('Z', '+00:00'))
            if dt.tzinfo is not None:
                return dt.astimezone(tz) if tz else dt
            return dt.replace(tzinfo=tz or TimezoneHelper.DEFAULT_TZ)
        except ValueError as e:
            logger.error("Failed to parse datetime: %s, error: %s", dt_str, e)
            raise
    
    @staticmethod
    def format_for_storage(dt: datetime) -> str:
        """格式化datetime用于存储"""
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TimezoneHelper.DEFAULT_TZ)
        return dt.astimezone(timezone.utc).isoformat()

def now_in_timezone(user_id: str = None, db=None) -> datetime:
    """获取用户时区的当前时间"""
    if user_id and db:
        tz = TimezoneHelper.get_user_timezone(user_id, db)
    else:
        tz = TimezoneHelper.DEFAULT_TZ
    return TimezoneHelper.now(tz)

def parse_and_localize(dt_str: str, user_id: str = None, db=None) -> datetime:
    """解析时间字符串并本地化"""
    if user_id and db:
        tz = TimezoneHelper.get_user_timezone(user_id, db)
    else:
        tz = TimezoneHelper.DEFAULT_TZ
    return TimezoneHelper.parse(dt_str, tz)

# ==================== 日期解析 ====================

def parse_date_optional(date_str: str, now: Optional[datetime] = None) -> Optional[str]:
    """解析日期字符串为 YYYY-MM-DD，失败返回 None"""
    if not date_str or not str(date_str).strip():
        return None
    
    now = now or datetime.now()
    text = str(date_str).strip()
    lowered = text.lower()
    
    # 相对日期
    relative = {'今天': 0, 'today': 0, '昨天': -1, 'yesterday': -1, 
                '明天': 1, 'tomorrow': 1, '后天': 2, '前天': -2}
    if lowered in relative:
        return (now + timedelta(days=relative[lowered])).strftime('%Y-%m-%d')
    
    # YYYY-MM-DD
    try:
        return datetime.strptime(text, '%Y-%m-%d').strftime('%Y-%m-%d')
    except ValueError:
        pass
    
    # MM-DD
    try:
        return datetime.strptime(f"{now.year}-{text}", '%Y-%m-%d').strftime('%Y-%m-%d')
    except ValueError:
        pass
    
    return None

def parse_date_required(date_str: str, now: Optional[datetime] = None) -> str:
    """解析日期字符串，失败抛出 ValueError"""
    parsed = parse_date_optional(date_str, now)
    if not parsed:
        raise ValueError(f"无法解析日期: {date_str}")
    return parsed

def _parse_ym_range(ym_str: str) -> tuple[datetime, datetime]:
    """解析 YYYY-MM 字符串为该月第一天和最后一天的 datetime"""
    dt = datetime.strptime(ym_str, '%Y-%m')
    start = dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if dt.month == 12:
        end = dt.replace(year=dt.year + 1, month=1, day=1) - timedelta(seconds=1)
    else:
        end = dt.replace(month=dt.month + 1, day=1) - timedelta(seconds=1)
    return start, end


def parse_event_time_range(time_range: str, now: Optional[datetime] = None) -> tuple[str, str]:
    """解析事件时间范围，返回 ISO start/end"""
    now = now or datetime.now()
    tr = (time_range or 'today').strip().lower()

    # 标准化：将3个以上连续点（如 ...）规范化为 2 个点
    tr = re.sub(r'\.{3,}', '..', tr)

    # last7d 格式
    match = re.search(r'last(\d+)d', tr)
    if match:
        days = int(match.group(1))
        start = now - timedelta(days=days)
        return start.isoformat(), now.isoformat()

    ranges = {
        'today': (now.replace(hour=0, minute=0, second=0, microsecond=0),
                  now.replace(hour=23, minute=59, second=59, microsecond=0)),
        'tomorrow': ((now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0),
                     (now + timedelta(days=1)).replace(hour=23, minute=59, second=59, microsecond=0)),
        'week': (now.replace(hour=0, minute=0, second=0, microsecond=0), now + timedelta(days=7)),
        'month': (now.replace(hour=0, minute=0, second=0, microsecond=0), now + timedelta(days=30)),
        'year': (now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0),
                 now.replace(month=12, day=31, hour=23, minute=59, second=59, microsecond=0)),
    }

    if tr in ranges:
        start, end = ranges[tr]
        return start.isoformat(), end.isoformat()

    # YYYY 格式
    if re.fullmatch(r'\d{4}', tr):
        year = int(tr)
        return datetime(year, 1, 1).isoformat(), datetime(year, 12, 31, 23, 59, 59).isoformat()

    # YYYY-MM 格式（独立）
    if re.fullmatch(r'\d{4}-\d{2}', tr):
        start, end = _parse_ym_range(tr)
        return start.isoformat(), end.isoformat()

    # start..end 格式（支持 YYYY-MM-DD 和 YYYY-MM）
    if '..' in tr:
        try:
            start_str, end_str = tr.split('..', 1)
            start_str, end_str = start_str.strip(), end_str.strip()

            # 解析 start
            try:
                start = datetime.strptime(start_str, '%Y-%m-%d')
            except ValueError:
                start, _ = _parse_ym_range(start_str)

            # 解析 end
            try:
                end = datetime.strptime(end_str, '%Y-%m-%d').replace(hour=23, minute=59, second=59, microsecond=0)
            except ValueError:
                _, end = _parse_ym_range(end_str)

            return start.isoformat(), end.isoformat()
        except (ValueError, AttributeError) as e:
            logger.warning("Failed to parse time range '%s': %s", time_range, e)
            pass

    # 默认今天
    logger.debug("Using default date range (today): %s", now.strftime('%Y-%m-%d'))
    return (now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(),
            now.replace(hour=23, minute=59, second=59, microsecond=0).isoformat())

def parse_search_date_range(range_str: str, now: Optional[datetime] = None) -> tuple[Optional[str], Optional[str]]:
    """解析搜索日期范围"""
    now = now or datetime.now()
    
    match = re.search(r'last(\d+)d', range_str)
    if match:
        days = int(match.group(1))
        start = now - timedelta(days=days)
        return start.isoformat(), now.isoformat()
    
    if '..' in range_str:
        parts = range_str.split('..')
        if len(parts) == 2:
            return parts[0] + 'T00:00:00', parts[1] + 'T23:59:59'
    
    return None, None

def parse_diary_range(range_str: str, now: Optional[datetime] = None) -> tuple[str, str]:
    """解析日记范围，返回 YYYY-MM-DD start/end"""
    now = now or datetime.now()
    tr = (range_str or 'today').strip().lower()
    
    if tr in ['today', '今天']:
        return now.strftime('%Y-%m-%d'), now.strftime('%Y-%m-%d')
    
    if tr in ['week', '本周']:
        start = now - timedelta(days=now.weekday())
        end = start + timedelta(days=6)
        return start.strftime('%Y-%m-%d'), end.strftime('%Y-%m-%d')
    
    if tr in ['month', '本月']:
        start = now.replace(day=1)
        if now.month == 12:
            end = now.replace(year=now.year + 1, month=1, day=1) - timedelta(days=1)
        else:
            end = now.replace(month=now.month + 1, day=1) - timedelta(days=1)
        return start.strftime('%Y-%m-%d'), end.strftime('%Y-%m-%d')
    
    if tr in ['year', '今年']:
        return now.replace(month=1, day=1).strftime('%Y-%m-%d'), now.replace(month=12, day=31).strftime('%Y-%m-%d')
    
    # YYYY-MM 格式
    if re.fullmatch(r'\d{4}-\d{2}', tr):
        dt = datetime.strptime(tr, '%Y-%m')
        start = dt.replace(day=1)
        if dt.month == 12:
            end = dt.replace(year=dt.year + 1, month=1, day=1) - timedelta(days=1)
        else:
            end = dt.replace(month=dt.month + 1, day=1) - timedelta(days=1)
        return start.strftime('%Y-%m-%d'), end.strftime('%Y-%m-%d')
    
    # last7d 格式
    match = re.search(r'last(\d+)d', tr)
    if match:
        days = int(match.group(1))
        start = now - timedelta(days=days)
        return start.strftime('%Y-%m-%d'), now.strftime('%Y-%m-%d')
    
    return (now - timedelta(days=30)).strftime('%Y-%m-%d'), now.strftime('%Y-%m-%d')

def parse_delay_time(delay_str: str, current_due: Optional[str] = None, now: Optional[datetime] = None) -> Optional[str]:
    """解析延后时间，返回 ISO 时间字符串"""
    if not delay_str:
        return None
    
    now = now or datetime.now()
    text = str(delay_str).strip().lower()
    
    # 相对时间: 1h, 30m, 2d
    match = re.match(r'^(\d+)([hmd])$', text)
    if match:
        num = int(match.group(1))
        unit = match.group(2)
        base = now
        if current_due:
            try:
                base = datetime.fromisoformat(current_due)
            except (ValueError, TypeError):
                # 解析失败，使用当前时间作为基准
                pass
        deltas = {'h': timedelta(hours=num), 'm': timedelta(minutes=num), 'd': timedelta(days=num)}
        return (base + deltas[unit]).isoformat()

    # 绝对时间: HH:MM
    if ':' in text:
        try:
            hour, minute = map(int, text.split(':'))
            candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if candidate < now:
                candidate += timedelta(days=1)
            return candidate.isoformat()
        except (ValueError, AttributeError):
            # 时间格式解析失败
            pass

    return None

def parse_hhmm_to_minutes(time_str: str) -> Optional[int]:
    """解析 HH:MM 到分钟数"""
    if not time_str:
        return None
    try:
        hour, minute = map(int, str(time_str).strip().split(':'))
        if 0 <= hour < 24 and 0 <= minute < 60:
            return hour * 60 + minute
    except (ValueError, AttributeError):
        # 时间格式解析失败
        pass
    return None

# ==================== 格式化工具（委托给 ItemFormatter） ====================

def format_datetime(dt_str: str, fmt: str = '%Y-%m-%d %H:%M') -> str:
    """格式化日期时间字符串"""
    return _ItemFormatter.format_datetime(dt_str, fmt)

def format_date(dt_str: str) -> str:
    """格式化日期"""
    return _ItemFormatter.format_date(dt_str)

def format_time(dt_str: str) -> str:
    """格式化时间"""
    return _ItemFormatter.format_time(dt_str)

def truncate_text(text: str, max_length: int = 100, suffix: str = '...') -> str:
    """截断文本"""
    if not text or len(text) <= max_length:
        return text or ''
    return text[:max_length - len(suffix)] + suffix

def parse_time_offset(offset_str: str) -> int:
    """解析时间偏移为分钟数"""
    match = re.match(r'^(\d+)([mhd])$', offset_str.lower())
    if not match:
        return 0
    value = int(match.group(1))
    unit = match.group(2)
    return {'m': value, 'h': value * 60, 'd': value * 1440}.get(unit, 0)

# ==================== 设置工具 ====================

def parse_custom_settings(settings: dict[str, Any]) -> dict[str, Any]:
    """解析自定义设置JSON"""
    custom = {}
    if settings.get('settings_json'):
        try:
            custom = json.loads(settings['settings_json'])
        except (json.JSONDecodeError, TypeError) as e:
            logger.warning("Failed to parse custom settings: %s", e)
    return custom

def save_user_setting(user_id: str, key: str, value: Any, db):
    """保存用户设置"""
    try:
        settings = db.settings.get_user_settings(user_id)
        custom = parse_custom_settings(settings)
        custom[key] = value
        settings['settings_json'] = json.dumps(custom, ensure_ascii=False)
        db.settings.update_user_settings(user_id, settings)
    except Exception as e:
        logger.exception("Failed to save setting for user %s: %s", user_id, e)
        raise
