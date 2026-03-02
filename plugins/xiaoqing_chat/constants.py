from __future__ import annotations


# Maximum character length considered as "short text" for display/truncation purposes.
DEFAULT_SHORT_TEXT_LIMIT = 120

# Maximum character length for text logged in step-level debug output.
LOG_TEXT_LIMIT = 140

# How many recent local_ids to track per chat for auto-increment allocation.
LOCAL_ID_HISTORY_LIMIT = 50

# How many messages to scan backwards when resolving a local_id (e.g. "m123") to its content.
FIND_BY_LOCAL_ID_LIMIT = 200

# Timeout (seconds) for the foreground memory retrieval task before giving up.
MEMORY_RETRIEVAL_TIMEOUT = 1.5

# Maximum number of unknown/jargon words to look up per reply generation.
UNKNOWN_WORDS_MAX = 6

# Default maximum number of expression habits injected into the prompt.
EXPRESSION_MAX_INJ_DEFAULT = 10

# Maximum number of reply regeneration attempts when reply-checker rejects the output.
REGENERATION_MAX_ATTEMPTS = 3

# Minimum interval (seconds) between expression learning runs for the same chat.
EXPRESSION_LEARN_MIN_INTERVAL = 90.0

# Minimum number of new messages before triggering an expression learning cycle.
EXPRESSION_LEARN_MIN_MESSAGES = 10
