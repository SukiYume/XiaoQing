"""Shared constants for XiaoQing."""

# Default configuration values
DEFAULT_SESSION_TIMEOUT_SEC = 300.0
DEFAULT_INBOUND_PORT = 12000
DEFAULT_WS_PATH = "/ws"
DEFAULT_MAX_CONCURRENCY = 5
DEFAULT_INBOUND_WS_MAX_WORKERS = 8
DEFAULT_INBOUND_WS_QUEUE_SIZE = 200
DEFAULT_LOG_TRUNCATE_LEN = 50

# Time conversion
SECONDS_PER_MINUTE = 60
MINUTES_PER_HOUR = 60
SECONDS_PER_HOUR = 3600
SECONDS_PER_DAY = 86400

# Session exit commands
EXIT_COMMANDS_SET = frozenset({"退出", "取消", "exit", "quit", "q"})

# Default responses
DEFAULT_BOT_NAME_RESPONSES_LIST = ["叫我干嘛", "嗯？", "在的~", "有事吗？"]

# Plugin security
PLUGIN_INIT_TIMEOUT_SECONDS = 30.0  # 插件 init 函数超时时间
VALID_PLUGIN_NAME_PATTERN = r"^[a-zA-Z0-9_]+$"  # 插件名称只能包含字母数字下划线

# Message preview length
MAX_MESSAGE_PREVIEW_LENGTH = 220  # 消息预览最大长度（用于日志）
MAX_SHORT_TEXT_LENGTH = 60  # 短文本最大长度

# Message splitting
MAX_MESSAGE_TEXT_LENGTH = 3000  # 单条消息最大文本长度（QQ平台限制）
MESSAGE_SPLIT_DELAY = 0.3  # 分段发送间隔（秒）
