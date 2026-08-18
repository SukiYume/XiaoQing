"""Twitter 抓取目标的显式配置契约。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from plugins.twitter import main as twitter
from tests.helpers.settings_snapshot import with_settings_reader


@pytest.fixture
def context() -> SimpleNamespace:
    return with_settings_reader(
        SimpleNamespace(
            secrets={"plugins": {"twitter": {"user_id": "123456789"}}},
        )
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (" exact-id ", "exact-id"),
        ('id"\\Unicode-用户', 'id"\\Unicode-用户'),
        (123456, "123456"),
    ],
)
def test_user_id_normalization(
    context: SimpleNamespace,
    value: object,
    expected: str,
) -> None:
    context.secrets["plugins"]["twitter"]["user_id"] = value
    assert twitter._get_user_id(context) == expected


@pytest.mark.parametrize(
    "value",
    [0, True, None, "", "bad\nvalue", "x" * (twitter.MAX_USER_ID_CHARS + 1)],
)
def test_user_id_is_required(context: SimpleNamespace, value: object) -> None:
    context.secrets["plugins"]["twitter"]["user_id"] = value

    with pytest.raises(twitter.TwitterConfigurationError, match="user_id"):
        twitter._get_user_id(context)


@pytest.mark.asyncio
async def test_background_fetch_reports_missing_user_id(
    context: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        twitter,
        "_fetch_twitter_images",
        AsyncMock(side_effect=twitter.TwitterConfigurationError()),
    )

    outcome = await twitter._run_background_fetch(context)

    assert outcome.succeeded is False
    assert outcome.count == 0
    assert "config/secrets.json" in outcome.message
    assert "user_id" in outcome.message
