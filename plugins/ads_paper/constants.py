"""Constants for ads_paper plugin."""
import re

# API Configuration
DEFAULT_MAX_RESULTS = 5
DEFAULT_MAX_AUTHORS = 3
DEFAULT_MAX_CITATIONS = 10
DEFAULT_MAX_REFERENCES = 10
DEFAULT_DAILY_PAPERS = 10

# Display Configuration
MAX_TITLE_DISPLAY_LENGTH = 50
MAX_TITLE_IN_OUTPUT = 60

# Regex Patterns (Pre-compiled for performance)
ARXIV_URL_PATTERN = re.compile(r"arxiv\.org/abs/([\w\-./]+?)(?:v\d+)?$")
ARXIV_VERSION_PATTERN = re.compile(r"v\d+$")
ARXIV_NEW_FORMAT_PATTERN = re.compile(r"\b(\d{4}\.\d{4,5})\b")
ARXIV_OLD_FORMAT_PATTERN = re.compile(r"\b([a-z\-]+/\d{7})\b")

# Date Format
DATE_FORMAT = "%Y-%m-%d"
DATETIME_FORMAT = "%Y-%m-%d %H:%M"
