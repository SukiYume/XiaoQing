"""ADS Paper 插件的查询上限、展示上限和日期格式。"""

# 检索：每页条数与可翻到的最深位置
DEFAULT_SEARCH_ROWS = 10
MAX_SEARCH_ROWS     = 25
# 带摘要节选或命中片段时每篇多出几百字：一页控制在 agent 单次能读完的一万字以内。
MAX_DETAILED_ROWS = 10
MAX_SEARCH_START  = 2000

# 一条命令可带的论文标识数量
MAX_PAPER_IDS = 8

# 列表与概览
DEFAULT_MAX_AUTHORS  = 3
DEFAULT_DAILY_PAPERS = 10
NETWORK_PREVIEW_ROWS = 5
RELATED_ROWS         = 8
JOURNAL_ROWS         = 15
STATS_FACET_ROWS     = 8
STATS_YEAR_ROWS      = 40

# 展示上限
MAX_TITLE_DISPLAY_LENGTH           = 50
MAX_REFERENCE_TITLE_DISPLAY_LENGTH = 60
ABSTRACT_EXCERPT_CHARS             = 400
MAX_HIGHLIGHT_SNIPPETS             = 3

# 本地日期格式
DATE_FORMAT     = "%Y-%m-%d"
DATETIME_FORMAT = "%Y-%m-%d %H:%M"
