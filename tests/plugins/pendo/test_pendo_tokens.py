"""命令参数分词：各个子命令共用一份规则。"""

from __future__ import annotations

import pytest

from plugins.pendo.handlers.search import SearchHandler
from plugins.pendo.utils.tokens import shell_tokens


def test_quotes_group_words_and_apostrophes_inside_words_are_plain_text() -> None:
    assert shell_tokens('买菜 "Dinner in Paris" cat:生活', "参数") == [
        "买菜",
        "Dinner in Paris",
        "cat:生活",
    ]
    assert shell_tokens("don't forget 'two words'", "参数") == ["don't", "forget", "two words"]
    assert shell_tokens("", "参数") == []


def test_unclosed_quote_is_reported_with_the_callers_wording() -> None:
    with pytest.raises(ValueError, match="搜索参数中的引号未闭合"):
        shell_tokens('"没有闭合', "搜索参数")


def test_search_accepts_words_with_apostrophes_like_todo_does() -> None:
    query, filters = SearchHandler._tokenize_search_args("don't type:task")
    assert query == ["don't"] and filters == {"type": "task"}
