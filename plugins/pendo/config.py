"""Pendo 的静态默认值与少量运行期覆盖项。"""

from collections.abc import Mapping
from dataclasses import dataclass
from threading import Lock
from typing import Any, ClassVar


@dataclass(frozen=True, slots=True)
class PendoRuntimeSettings:
    """Atomically published settings that may change while the plugin is loaded."""

    web_enabled: bool = True
    web_host: str = "127.0.0.1"
    web_port: int = 12001
    web_session_cookie_secure: bool = False
    web_demo_enabled: bool = False


class PendoConfig:
    """Pendo插件配置类"""

    # 数据库配置
    DB_FILENAME = "pendo.db"

    # 用户设置默认值
    DEFAULT_TIMEZONE = "Asia/Shanghai"
    DEFAULT_QUIET_HOURS_START = "23:00"
    DEFAULT_QUIET_HOURS_END = "07:00"
    DEFAULT_DAILY_REPORT_TIME = "08:00"
    DEFAULT_DIARY_REMIND_TIME = "21:30"
    DEFAULT_CATEGORY = "未分类"

    # 提醒配置
    REMINDER_CHECK_WINDOW_SECONDS = 120  # 提醒检查时间窗口（秒）
    REMINDER_CLAIM_LEASE_SECONDS = 120  # 原子领取后的 worker lease
    REMINDER_MAX_RETRY = 3  # 提醒发送最大重试次数
    REMINDER_REPEAT_INTERVAL_SECONDS = 300  # 未确认提醒重复间隔（秒），默认5分钟
    REMINDER_MAX_REPEATS = 3  # 未确认提醒最大重复次数
    REMINDER_AUTO_CONFIRM_AFTER_FINAL_SEND_SECONDS = 600
    REMINDER_STALE_AFTER_SECONDS = 24 * 60 * 60  # 服务长时间离线后不复活旧提醒
    REMINDER_LOG_RETENTION_DAYS = 90  # 已确认提醒历史保留天数

    # 搜索配置
    DEFAULT_SEARCH_LIMIT = 50  # 默认搜索结果数量

    # 日程配置
    EVENT_MAX_RRULE_COUNT = 365  # 重复日程最大次数

    # AI配置
    AI_PARSE_TIMEOUT = 30  # AI解析超时时间（秒）
    AI_PARSE_TEMPERATURE = 0.3  # AI解析温度参数
    AI_MAX_TOKENS = 1000  # AI最大token数
    AI_FALLBACK_TO_RULES = True  # AI失败时是否回退到规则解析

    LOG_OPERATION_RETENTION_DAYS = 90  # 操作日志保留天数
    UNDO_WINDOW_MINUTES = 5  # 删除与编辑共享的固定可撤销窗口
    UNDO_HINT = f"💡 {UNDO_WINDOW_MINUTES}分钟内可用 /pendo undo 撤销"
    # 分页配置
    LIST_PAGE_SIZE = 10  # 列表分页大小

    # 任务展示配置
    TASK_OVERDUE_PREVIEW_COUNT = 5  # 逾期任务预览数量

    # 搜索展示配置
    SEARCH_CONTENT_PREVIEW_LENGTH = 50  # 搜索结果内容预览长度

    # 会话配置
    SESSION_TIMEOUT_SECONDS = 300.0  # 会话超时时间（秒）
    SESSION_EXIT_COMMANDS: ClassVar[tuple[str, ...]] = (
        "退出",
        "exit",
        "quit",
        "cancel",
        "取消",
    )  # 退出会话的命令

    # 确认命令
    CONFIRM_POSITIVE: ClassVar[tuple[str, ...]] = ("yes", "y", "是", "确认")
    CONFIRM_NEGATIVE: ClassVar[tuple[str, ...]] = ("no", "n", "否", "取消")

    # 会话类型常量
    SESSION_TYPE_DIARY_TEMPLATE = "diary_template"
    SESSION_TYPE_EVENT_CONFLICT = "event_conflict"
    SESSION_TYPE_EVENT_INFO = "event_info"
    SESSION_TYPE_TASK_ADD = "task_add"
    SESSION_TYPE_LEDGER_ADD = "ledger_add"

    # Web UI
    WEB_LOGIN_CODE_EXPIRE_SECONDS = 7 * 24 * 60 * 60
    WEB_SESSION_EXPIRE_SECONDS = 8 * 60 * 60
    WEB_WIDGET_TOKEN_EXPIRE_HOURS = 24 * 365
    WEB_DEMO_EXPIRE_HOURS = 6
    WEB_DEMO_MAX_ACTIVE_SESSIONS = 20
    WEB_DEMO_REQUESTS_PER_HOUR = 3
    _RUNTIME_DEFAULTS: ClassVar[PendoRuntimeSettings] = PendoRuntimeSettings()
    _runtime_settings: ClassVar[PendoRuntimeSettings] = _RUNTIME_DEFAULTS
    _runtime_revision: ClassVar[int | None] = None
    _runtime_lock: ClassVar[Lock] = Lock()

    @classmethod
    def validate(cls) -> None:
        """验证配置合理性"""
        if cls.TASK_OVERDUE_PREVIEW_COUNT <= 0:
            raise ValueError("TASK_OVERDUE_PREVIEW_COUNT must be positive")
        if cls.LIST_PAGE_SIZE <= 0:
            raise ValueError("LIST_PAGE_SIZE must be positive")
        if cls.EVENT_MAX_RRULE_COUNT <= 0:
            raise ValueError("EVENT_MAX_RRULE_COUNT must be positive")
        if cls.REMINDER_CHECK_WINDOW_SECONDS <= 0:
            raise ValueError("REMINDER_CHECK_WINDOW_SECONDS must be positive")
        if cls.UNDO_WINDOW_MINUTES <= 0:
            raise ValueError("UNDO_WINDOW_MINUTES must be positive")
        if cls.REMINDER_STALE_AFTER_SECONDS <= cls.REMINDER_REPEAT_INTERVAL_SECONDS:
            raise ValueError("REMINDER_STALE_AFTER_SECONDS must exceed the repeat interval")
        if cls.WEB_LOGIN_CODE_EXPIRE_SECONDS <= 0:
            raise ValueError("WEB_LOGIN_CODE_EXPIRE_SECONDS must be positive")
        if cls.WEB_WIDGET_TOKEN_EXPIRE_HOURS <= 0:
            raise ValueError("WEB_WIDGET_TOKEN_EXPIRE_HOURS must be positive")

    @classmethod
    def reset_runtime_config(cls) -> None:
        """重置运行期可覆盖配置，避免热更新和测试间串值。"""
        with cls._runtime_lock:
            cls._runtime_settings = cls._RUNTIME_DEFAULTS
            cls._runtime_revision = None

    @classmethod
    def runtime(cls) -> PendoRuntimeSettings:
        """Return one immutable runtime settings generation."""

        with cls._runtime_lock:
            return cls._runtime_settings

    @staticmethod
    def _boolean(config: Mapping[str, Any], key: str, default: bool) -> bool:
        value = config.get(key, default)
        if type(value) is not bool:
            raise TypeError(f"plugins.pendo.{key} must be a boolean")
        return value

    @classmethod
    def _host(cls, config: Mapping[str, Any]) -> str:
        value = config.get("web_host", cls._RUNTIME_DEFAULTS.web_host)
        if (
            type(value) is not str
            or not value
            or value != value.strip()
            or len(value) > 255
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            raise ValueError("plugins.pendo.web_host must be a non-empty host string")
        return value

    @classmethod
    def _port(cls, config: Mapping[str, Any]) -> int:
        value = config.get("web_port", cls._RUNTIME_DEFAULTS.web_port)
        if type(value) is not int or not 1 <= value <= 65_535:
            raise ValueError("plugins.pendo.web_port must be an integer from 1 to 65535")
        return value

    @classmethod
    def configure(
        cls,
        config: Mapping[str, Any],
        *,
        settings_revision: int | None = None,
    ) -> bool:
        """Validate and atomically publish one ``plugins.pendo`` generation."""

        if not isinstance(config, Mapping):
            raise TypeError("plugins.pendo must be a mapping")
        if settings_revision is not None and (
            type(settings_revision) is not int or settings_revision < 0
        ):
            raise ValueError("Pendo settings revision must be a non-negative integer")

        candidate = PendoRuntimeSettings(
            web_enabled=cls._boolean(config, "web_enabled", cls._RUNTIME_DEFAULTS.web_enabled),
            web_host=cls._host(config),
            web_port=cls._port(config),
            web_session_cookie_secure=cls._boolean(
                config,
                "web_session_cookie_secure",
                cls._RUNTIME_DEFAULTS.web_session_cookie_secure,
            ),
            web_demo_enabled=cls._boolean(
                config,
                "web_demo_enabled",
                cls._RUNTIME_DEFAULTS.web_demo_enabled,
            ),
        )

        with cls._runtime_lock:
            current_revision = cls._runtime_revision
            if (
                settings_revision is not None
                and current_revision is not None
                and settings_revision < current_revision
            ):
                return False
            if (
                settings_revision is not None
                and settings_revision == current_revision
                and candidate != cls._runtime_settings
            ):
                raise RuntimeError("conflicting Pendo settings for the same revision")
            changed = candidate != cls._runtime_settings
            cls._runtime_settings = candidate
            if settings_revision is not None:
                cls._runtime_revision = settings_revision
            return changed


# 日记模板配置
DIARY_TEMPLATES = {
    "three_good": {
        "name": "三件好事",
        "prompts": ["今天发生的第一件好事:", "今天发生的第二件好事:", "今天发生的第三件好事:"],
    },
    "summary": {
        "name": "今日总结",
        "prompts": ["今天做了什么:", "今天学到了什么:", "有什么可以改进:", "明天最重要的事:"],
    },
    "mood": {
        "name": "情绪记录",
        "prompts": ["今天的心情 (1-10):", "为什么会有这样的心情:", "有什么让你开心/难过的事:"],
    },
}

DIARY_MOODS = [
    {"id": "happy", "label": "开心", "emoji": "😊"},
    {"id": "calm", "label": "平静", "emoji": "😌"},
    {"id": "excited", "label": "兴奋", "emoji": "🤩"},
    {"id": "sad", "label": "难过", "emoji": "😢"},
    {"id": "angry", "label": "生气", "emoji": "😠"},
    {"id": "tired", "label": "疲惫", "emoji": "😴"},
    {"id": "anxious", "label": "焦虑", "emoji": "😰"},
    {"id": "grateful", "label": "感恩", "emoji": "🙏"},
    {"id": "neutral", "label": "普通", "emoji": "😐"},
]

# 情绪分析配置
MOOD_ANALYSIS_CONFIG = {
    "positive_words": [
        "开心",
        "高兴",
        "快乐",
        "幸福",
        "满足",
        "愉快",
        "兴奋",
        "棒",
        "好",
        "优秀",
        "成功",
        "顺利",
        "喜欢",
        "爱",
        "棒极了",
        "太好了",
        "充满希望",
    ],
    "negative_words": [
        "难过",
        "伤心",
        "痛苦",
        "失望",
        "焦虑",
        "压力",
        "烦恼",
        "累",
        "差",
        "糟糕",
        "失败",
        "讨厌",
        "愤怒",
        "沮丧",
        "郁闷",
        "崩溃",
        "无助",
    ],
    "calm_words": ["平静", "安宁", "放松", "舒适", "还行", "一般", "平常", "普通", "淡定", "冷静"],
    "excited_words": ["激动", "兴奋", "期待", "迫不及待", "热血沸腾"],
    "angry_words": ["生气", "愤怒", "恼火", "不爽", "讨厌", "烦"],
    "tired_words": ["疲惫", "疲劳", "困", "没精神", "累瘫"],
    "anxious_words": ["焦虑", "担心", "紧张", "不安", "慌"],
    "grateful_words": ["感恩", "感谢", "珍惜", "幸运", "被照顾"],
    # 情绪类型到emoji的映射
    "mood_emojis": {row["id"]: row["emoji"] for row in DIARY_MOODS},
    "mood_labels": {row["id"]: row["label"] for row in DIARY_MOODS},
    "allowed_moods": [row["id"] for row in DIARY_MOODS],
    # 基础分数配置
    "base_scores": {
        "happy": 6,
        "sad": 5,
        "calm": 5,
        "excited": 8,
        "angry": 3,
        "tired": 4,
        "anxious": 3,
        "grateful": 7,
        "neutral": 5,
    },
    # 每个匹配词的分值增量
    "score_increment": 1,
}

# 记账分类配置
LEDGER_EXPENSE_CATEGORIES = [
    {"id": "food", "name": "餐饮", "icon": "🍜"},
    {"id": "transport", "name": "交通", "icon": "🚌"},
    {"id": "shopping", "name": "购物", "icon": "🛒"},
    {"id": "housing", "name": "居住", "icon": "🏠"},
    {"id": "entertainment", "name": "娱乐", "icon": "🎮"},
    {"id": "medical", "name": "医疗", "icon": "💊"},
    {"id": "education", "name": "教育", "icon": "📚"},
    {"id": "telecom", "name": "通讯", "icon": "📱"},
    {"id": "social", "name": "社交", "icon": "🎁"},
    {"id": "pet", "name": "宠物", "icon": "🐾"},
    {"id": "sport", "name": "运动", "icon": "🏃"},
    {"id": "service", "name": "服务", "icon": "💆"},
    {"id": "travel", "name": "旅行", "icon": "✈️"},
    {"id": "other", "name": "其他", "icon": "📌"},
]

LEDGER_INCOME_CATEGORIES = [
    {"id": "salary", "name": "工资", "icon": "💰"},
    {"id": "parttime", "name": "兼职", "icon": "💼"},
    {"id": "invest", "name": "理财", "icon": "📈"},
    {"id": "hongbao", "name": "红包", "icon": "🧧"},
    {"id": "reimburse", "name": "报销", "icon": "🧾"},
    {"id": "other", "name": "其他", "icon": "📌"},
]
