"""集中定义聊天、记忆、规划和媒体链共享的稳定边界。"""

from __future__ import annotations

# 把 local_id（如 m123）还原为正文时，最多向前扫描的消息数。
FIND_BY_LOCAL_ID_LIMIT = 200

# 每次生成回复时最多查询的未知词或行话数量。
UNKNOWN_WORDS_MAX = 6

# 同一会话两次表达学习之间的最短秒数。
EXPRESSION_LEARN_MIN_INTERVAL = 90.0

# 触发一次表达学习所需的最少新消息数。
EXPRESSION_LEARN_MIN_MESSAGES = 10


# “每分钟回复数”统计的滑动窗口：频控门和提示里的频率行都按这一分钟数回复条数。
REPLY_RATE_WINDOW_SECONDS = 60.0


# 读图摘要（画面描述加图上文字）整段写进聊天记录的 [图片：…] 标记，超出部分截掉。
MEDIA_SUMMARY_MAX_CHARS = 1000

# 媒体读不出内容时写进标记的描述：协议端没给出摘要时是前一句，视觉路由失败时是后一句。
MEDIA_READ_FAILED_LABEL  = "图片内容读取失败"
MEDIA_UNRECOGNIZED_LABEL = "图片内容暂时无法识别"

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
        MEDIA_READ_FAILED_LABEL,
        MEDIA_UNRECOGNIZED_LABEL,
    }
)


# 只喊一声名字（或只 @ 她）时的短应答：喊名字多半是在看她在不在，立刻应一声，不走模型。
DEFAULT_BOT_NAME_ONLY_REPLIES = (
    "在",
    "在呢",
    "嗯？",
    "咋了",
    "怎么啦",
    "我在",
    "来了来了",
    "在的在的",
    "叫我干嘛",
    "有事吗",
    "嗯嗯？",
    "在，咋啦",
)

# 该回却没读到内容时的短句（``fallback_idle_replies`` 的默认值；配置成空列表时也用这份）。
DEFAULT_FALLBACK_IDLE_REPLIES = ("我在听", "接着说", "我想一下")
