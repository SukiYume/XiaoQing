"""ADS Paper 插件的显示、查询和标识符常量。"""

import re

# API 默认上限
DEFAULT_MAX_RESULTS    = 5
DEFAULT_MAX_AUTHORS    = 3
DEFAULT_MAX_CITATIONS  = 10
DEFAULT_MAX_REFERENCES = 10
DEFAULT_DAILY_PAPERS   = 10

# 显示上限
MAX_TITLE_DISPLAY_LENGTH = 50

# 论文标识符模式
ARXIV_URL_PATTERN        = re.compile(r"arxiv\.org/abs/([\w\-./]+?)(?:v\d+)?$")
ARXIV_VERSION_PATTERN    = re.compile(r"v\d+$")
ARXIV_NEW_FORMAT_PATTERN = re.compile(r"\b(\d{4}\.\d{4,5})\b")
ARXIV_OLD_FORMAT_PATTERN = re.compile(r"\b([a-z\-]+/\d{7})\b")
ADS_BIBCODE_PATTERN      = re.compile(r"\d{4}[A-Za-z0-9.&]{15}")

# 本地日期格式
DATE_FORMAT     = "%Y-%m-%d"
DATETIME_FORMAT = "%Y-%m-%d %H:%M"
