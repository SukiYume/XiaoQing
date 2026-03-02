"""
Pendo插件配置管理
集中管理所有可配置项，避免硬编码
"""
from typing import Any

class PendoConfig:
    """Pendo插件配置类"""
    
    _env_overrides: dict[str, Any] = {}
    
    # 数据库配置
    DB_FILENAME = 'pendo.db'
    
    # 用户设置默认值
    DEFAULT_TIMEZONE = 'Asia/Shanghai'
    DEFAULT_QUIET_HOURS_START = '23:00'
    DEFAULT_QUIET_HOURS_END = '07:00'
    DEFAULT_DAILY_REPORT_TIME = '08:00'
    DEFAULT_DIARY_REMIND_TIME = '21:30'
    DEFAULT_CATEGORY = '未分类'
    
    # 提醒配置
    REMINDER_CHECK_WINDOW_SECONDS = 120  # 提醒检查时间窗口（秒）
    REMINDER_MAX_RETRY = 3  # 提醒发送最大重试次数
    REMINDER_REPEAT_INTERVAL_SECONDS = 300  # 未确认提醒重复间隔（秒），默认5分钟
    REMINDER_MAX_REPEATS = 3  # 未确认提醒最大重复次数
    
    # 撤销配置
    UNDO_DELETE_WINDOW_MINUTES = 5  # 撤销删除时间窗口（分钟）
    
    # 搜索配置
    DEFAULT_SEARCH_LIMIT = 50  # 默认搜索结果数量
    FTS_LANGUAGE = 'chinese'  # 全文搜索语言
    
    # 导出配置
    EXPORT_MAX_ITEMS = 10000  # 单次导出最大条目数
    EXPORT_FORMAT_DEFAULT = 'by_type'  # 默认导出格式
    
    # 日程配置
    EVENT_MAX_RRULE_COUNT = 365  # 重复日程最大次数
    EVENT_CONFLICT_WARNING = True  # 是否警告日程冲突
    
    # 任务配置
    TASK_TODAY_SHOW_OVERDUE = True  # 今日清单是否显示逾期任务
    TASK_OVERDUE_MAX_SHOW = 10  # 最多显示逾期任务数量
    
    # AI配置
    AI_PARSE_TIMEOUT = 30  # AI解析超时时间（秒）
    AI_PARSE_TEMPERATURE = 0.3  # AI解析温度参数
    AI_MAX_TOKENS = 1000  # AI最大token数
    AI_FALLBACK_TO_RULES = True  # AI失败时是否回退到规则解析
    
    # 日志配置
    LOG_OPERATION = True  # 是否记录操作日志
    LOG_OPERATION_RETENTION_DAYS = 90  # 操作日志保留天数
    # 消息配置
    MESSAGE_PRIVACY_MODE_DEFAULT = True  # 默认开启隐私模式
    
    # 分页配置
    LIST_PAGE_SIZE = 10  # 列表分页大小
    LIST_MAX_ITEMS_INLINE = 5  # 内联显示最大条目数

    # 任务展示配置
    TASK_OVERDUE_PREVIEW_COUNT = 5  # 逾期任务预览数量
    TASK_HIGH_PRIORITY_THRESHOLD = 2  # 高优先级阈值 (1=紧急, 2=高)
    
    # 事件冲突配置
    EVENT_CONFLICT_MAX_SHOW = 3  # 最多显示冲突事件数量
    
    # 搜索展示配置
    SEARCH_CONTENT_PREVIEW_LENGTH = 50  # 搜索结果内容预览长度
    
    # 会话配置
    SESSION_TIMEOUT_SECONDS = 300.0  # 会话超时时间（秒）
    SESSION_EXIT_COMMANDS = ['退出', 'exit', 'quit', 'cancel', '取消']  # 退出会话的命令
    
    # 确认命令
    CONFIRM_POSITIVE = ['yes', 'y', '是', '确认']
    CONFIRM_NEGATIVE = ['no', 'n', '否', '取消']
    
    # 会话类型常量
    SESSION_TYPE_DIARY_TEMPLATE = 'diary_template'
    SESSION_TYPE_EVENT_CONFLICT = 'event_conflict'
    SESSION_TYPE_EVENT_INFO = 'event_info'
    
    @classmethod
    def validate(cls):
        """验证配置合理性"""
        if cls.TASK_OVERDUE_PREVIEW_COUNT <= 0:
            raise ValueError("TASK_OVERDUE_PREVIEW_COUNT must be positive")
        if cls.LIST_PAGE_SIZE <= 0:
            raise ValueError("LIST_PAGE_SIZE must be positive")
        if cls.EVENT_MAX_RRULE_COUNT <= 0:
            raise ValueError("EVENT_MAX_RRULE_COUNT must be positive")
        if cls.REMINDER_CHECK_WINDOW_SECONDS <= 0:
            raise ValueError("REMINDER_CHECK_WINDOW_SECONDS must be positive")
    
    @classmethod
    def from_env(cls):
        """从环境变量加载配置（可选的环境变量覆盖）"""
        import os
        
        # 允许通过环境变量覆盖部分配置，存储在实例字典中
        if 'PENDO_TIMEZONE' in os.environ:
            cls._env_overrides['DEFAULT_TIMEZONE'] = os.environ['PENDO_TIMEZONE']
        if 'PENDO_REMINDER_CHECK_WINDOW' in os.environ:
            cls._env_overrides['REMINDER_CHECK_WINDOW_SECONDS'] = int(os.environ['PENDO_REMINDER_CHECK_WINDOW'])
    
    @classmethod
    def get(cls, key: str, default: Any = None) -> Any:
        """获取配置值，优先使用环境变量覆盖"""
        if key in cls._env_overrides:
            return cls._env_overrides[key]
        return getattr(cls, key, default)

    @classmethod
    def get_user_settings_defaults(cls) -> dict[str, Any]:
        """获取用户设置默认值字典"""
        return {
            'timezone': cls.DEFAULT_TIMEZONE,
            'quiet_hours_start': cls.DEFAULT_QUIET_HOURS_START,
            'quiet_hours_end': cls.DEFAULT_QUIET_HOURS_END,
            'daily_report_time': cls.DEFAULT_DAILY_REPORT_TIME,
            'diary_remind_time': cls.DEFAULT_DIARY_REMIND_TIME,
            'default_category': cls.DEFAULT_CATEGORY,
        }
    
    @classmethod
    def get_reminder_config(cls) -> dict[str, Any]:
        """获取提醒配置字典"""
        return {
            'check_window_seconds': cls.REMINDER_CHECK_WINDOW_SECONDS,
            'max_retry': cls.REMINDER_MAX_RETRY,
        }
    
    @classmethod
    def get_ai_config(cls) -> dict[str, Any]:
        """获取AI配置字典"""
        return {
            'timeout': cls.AI_PARSE_TIMEOUT,
            'temperature': cls.AI_PARSE_TEMPERATURE,
            'max_tokens': cls.AI_MAX_TOKENS,
            'fallback_to_rules': cls.AI_FALLBACK_TO_RULES,
        }

# 提醒策略配置
REMINDER_POLICIES = {
    'meeting': {
        'name': '会议提醒',
        'reminders': [
            {'offset_days': -1, 'offset_hours': 12, 'message': '明天有会议'},
            {'offset_hours': -2, 'message': '2小时后有会议'},
            {'offset_minutes': -15, 'message': '15分钟后会议开始'},
        ]
    },
    'travel': {
        'name': '出行提醒',
        'reminders': [
            {'offset_days': -1, 'message': '明天有行程'},
            {'offset_hours': -3, 'message': '3小时后出发'},
            {'offset_hours': -1, 'message': '1小时后出发'},
        ]
    },
    'deadline': {
        'name': '截止日期提醒',
        'reminders': [
            {'offset_days': -7, 'message': '还有7天截止'},
            {'offset_days': -3, 'message': '还有3天截止'},
            {'offset_days': -1, 'message': '明天截止'},
            {'offset_hours': -2, 'message': '2小时后截止'},
        ]
    },
    'habit': {
        'name': '习惯打卡提醒',
        'reminders': [
            {'offset_minutes': 0, 'message': '该打卡了'},
        ]
    },
    'default': {
        'name': '默认提醒',
        'reminders': [
            {'offset_hours': -1, 'message': '1小时后'},
        ]
    }
}

# 日记模板配置
DIARY_TEMPLATES = {
    'default': {
        'name': '自由日记',
        'prompts': []
    },
    'three_good': {
        'name': '三件好事',
        'prompts': [
            '今天发生的第一件好事:',
            '今天发生的第二件好事:',
            '今天发生的第三件好事:'
        ]
    },
    'summary': {
        'name': '今日总结',
        'prompts': [
            '今天做了什么:',
            '今天学到了什么:',
            '有什么可以改进:',
            '明天最重要的事:'
        ]
    },
    'mood': {
        'name': '情绪记录',
        'prompts': [
            '今天的心情 (1-10):',
            '为什么会有这样的心情:',
            '有什么让你开心/难过的事:'
        ]
    }
}

# 情绪分析配置
MOOD_ANALYSIS_CONFIG = {
    'positive_words': [
        '开心', '高兴', '快乐', '幸福', '满足', '愉快',
        '兴奋', '棒', '好', '优秀', '成功', '顺利', '喜欢',
        '爱', '棒极了', '太好了', '充满希望'
    ],
    'negative_words': [
        '难过', '伤心', '痛苦', '失望', '焦虑', '压力',
        '烦恼', '累', '差', '糟糕', '失败', '讨厌',
        '愤怒', '沮丧', '郁闷', '崩溃', '无助'
    ],
    'calm_words': [
        '平静', '安宁', '放松', '舒适', '还行', '一般',
        '平常', '普通', '淡定', '冷静'
    ],
    'excited_words': [
        '激动', '兴奋', '期待', '迫不及待', '热血沸腾'
    ],
    'angry_words': [
        '生气', '愤怒', '恼火', '不爽', '讨厌', '烦'
    ],
    # 情绪类型到emoji的映射
    'mood_emojis': {
        'happy': '😊',
        'sad': '😢',
        'calm': '😌',
        'excited': '🤩',
        'angry': '😠'
    },
    # 基础分数配置
    'base_scores': {
        'happy': 6,
        'sad': 5,
        'calm': 5,
        'excited': 8,
        'angry': 3
    },
    # 每个匹配词的分值增量
    'score_increment': 1
}
