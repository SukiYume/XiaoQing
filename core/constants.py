"""XiaoQing 各运行模块共享的稳定常量。"""

# 运行时默认值：这里只保留确实被生产代码消费的默认配置。
DEFAULT_SESSION_TIMEOUT_SEC                  = 300.0
DEFAULT_MAX_CONCURRENCY                      = 5
DEFAULT_INBOUND_WS_MAX_WORKERS               = 8
DEFAULT_INBOUND_WS_QUEUE_SIZE                = 200
DEFAULT_INBOUND_WS_BROADCAST_TIMEOUT_SECONDS = 5.0
DEFAULT_HTTP_TIMEOUT_SECONDS                 = 30.0
DEFAULT_HTTP_CONNECT_TIMEOUT_SECONDS         = 10.0
# 空闲连接保留时长。aiohttp 默认 15 秒；经代理到模型服务重新握手约多 1–1.5 秒，
# 两分钟内的连续对话和 agent 多轮调用都能复用连接。传输错误仍按 AI route 重试。
DEFAULT_HTTP_KEEPALIVE_SECONDS               = 120.0
DEFAULT_ONEBOT_HTTP_TIMEOUT_SECONDS          = 15.0
DEFAULT_ONEBOT_WS_ACTION_TIMEOUT_SECONDS     = 15.0
INBOUND_EVENT_DEDUP_TTL_SECONDS              = 300.0
MAX_INBOUND_EVENT_DEDUP_KEYS                 = 10000

# 时间换算
SECONDS_PER_MINUTE = 60
SECONDS_PER_HOUR   = 3600
SECONDS_PER_DAY    = 86400

# 会话交互
EXIT_COMMANDS_SET               = frozenset({"退出", "取消", "exit", "quit", "q"})
DEFAULT_BOT_NAME                = "小青"
DEFAULT_COMMAND_PREFIX          = "/"
DEFAULT_BOT_NAME_RESPONSES_LIST = ["叫我干嘛", "嗯？", "在的~", "有事吗？"]

# 插件安全边界
PLUGIN_INIT_TIMEOUT_SECONDS          = 30.0  # 插件 init、shutdown 单次回调的时限
DEFAULT_PLUGIN_POLL_INTERVAL_SECONDS = 3600.0  # 插件目录的轮询间隔
VALID_PLUGIN_NAME_PATTERN            = r"^[a-zA-Z0-9_]+$"  # 插件名称只能包含字母数字下划线

# 消息长度与发送节奏
MAX_MESSAGE_TEXT_LENGTH = 3000  # 单条消息最大文本长度（QQ平台限制）
MESSAGE_SPLIT_DELAY     = 0.3  # 分段发送间隔（秒）
