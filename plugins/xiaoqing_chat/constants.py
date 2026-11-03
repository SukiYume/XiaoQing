"""集中定义聊天、记忆、规划和媒体链共享的稳定边界。"""

from __future__ import annotations

# 把 local_id（如 m123）还原为正文时，最多向前扫描的消息数。
FIND_BY_LOCAL_ID_LIMIT = 200

# 前台记忆检索任务放弃前的超时秒数。
MEMORY_RETRIEVAL_TIMEOUT = 4.5

# 每次生成回复时最多查询的未知词或行话数量。
UNKNOWN_WORDS_MAX = 6

# 注入提示词的表达习惯默认上限。
EXPRESSION_MAX_INJ_DEFAULT = 10

# 同一会话两次表达学习之间的最短秒数。
EXPRESSION_LEARN_MIN_INTERVAL = 90.0

# 触发一次表达学习所需的最少新消息数。
EXPRESSION_LEARN_MIN_MESSAGES = 10


# 读图摘要（画面描述加图上文字）整段写进聊天记录的 [图片：…] 标记，超出部分截掉。
MEDIA_SUMMARY_MAX_CHARS = 1000

# 只说明“有媒体”而没有可读内容的标签；这类消息不提供可接的话题。
GENERIC_MEDIA_LABELS = frozenset(
    {
        "图片",
        "表情",
        "表情包",
        "动画表情",
        "聊天表情包",
        "一张图片",
        "一张表情包",
        "一张聊天表情包",
        "图片内容读取失败",
        "图片内容暂时无法识别",
        "无法读取图片内容",
    }
)
