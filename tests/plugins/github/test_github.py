"""GitHub Trending 插件的命令、HTML、网络和历史边界测试。"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest

from core.bounded_http import BoundedHttpResponse, HttpStatusError
from core.constants import MAX_MESSAGE_TEXT_LENGTH
from core.safe_http import SafeHttpResponse
from plugins.github import main as github
from tests.helpers.settings_snapshot import with_settings_reader

SAMPLE_TRENDING_HTML = """
<!doctype html>
<html><body>
  <article class="Box-row">
    <h2><a href="/octocat/Hello-World">octocat / Hello-World</a></h2>
    <p>A sample repository for testing</p>
    <span itemprop="programmingLanguage">Python</span>
    <a href="/octocat/Hello-World/stargazers">1,234</a>
    <a href="/octocat/Hello-World/forks">56</a>
    <span class="d-inline-block float-sm-right">78 stars today</span>
  </article>
  <article class="Box-row">
    <h2><a href="/torvalds/linux">torvalds / linux</a></h2>
    <p>Linux kernel source tree</p>
    <span itemprop="programmingLanguage">C</span>
    <a href="/torvalds/linux/stargazers">150k</a>
    <a href="/torvalds/linux/forks">10,000</a>
    <span>123 stars this week</span>
  </article>
</body></html>
"""


@pytest.fixture
def context(tmp_path: Path) -> SimpleNamespace:
    return with_settings_reader(
        SimpleNamespace(
            data_dir     = tmp_path,
            secrets      = {"plugins": {"github": {"proxy": ""}}},
            http_session = None,
            request_id   = "github-test",
        )
    )


@pytest.fixture
def event() -> dict[str, Any]:
    return {"user_id": 12345}


def _safe_html(html: str, *, charset: str | None = "utf-8") -> SafeHttpResponse:
    return SafeHttpResponse(
        url     = "https://github.com/trending?since=daily",
        status  = 200,
        body    = html.encode("utf-8"),
        charset = charset,
        headers = {"Content-Type": "text/html; charset=utf-8"},
    )


def _bounded_html(html: str) -> BoundedHttpResponse:
    payload = html.encode("utf-8")
    return BoundedHttpResponse(
        url           = "https://github.com/trending?since=daily",
        status        = 200,
        body          = payload,
        media_type    = "text/html",
        charset       = "utf-8",
        headers       = {"Content-Type": "text/html; charset=utf-8"},
        wire_bytes    = len(payload),
        decoded_bytes = len(payload),
    )


def _repository(index: int = 1, *, description: str = "description") -> dict[str, str]:
    return {
        "owner": "owner",
        "name": f"repo-{index}",
        "full_name": f"owner/repo-{index}",
        "url": f"https://github.com/owner/repo-{index}",
        "description": description,
        "language": "Python",
        "stars": "100",
        "forks": "10",
        "stars_gained": "5 stars today",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("args", "expected_range"),
    [("", "daily"), ("daily", "daily"), ("weekly", "weekly"), ("monthly", "monthly")],
)
async def test_handle_routes_complete_range_argument(
    context: SimpleNamespace,
    event: dict[str, Any],
    args: str,
    expected_range: str,
) -> None:
    fetch = AsyncMock(return_value=github.segments("result"))
    with patch.object(github, "_fetch_trending", new=fetch):
        result = await github.handle("github", args, event, context)
    assert result == github.segments("result")
    fetch.assert_awaited_once_with(expected_range, context)


@pytest.mark.asyncio
@pytest.mark.parametrize("args", ["help", "h", "帮助"])
async def test_handle_help_aliases(
    context: SimpleNamespace,
    event: dict[str, Any],
    args: str,
) -> None:
    assert await github.handle("github", args, event, context) == github.segments(github.HELP_TEXT)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "args",
    ["yearly", "daily extra", '"unfinished', "daily\n", "x" * 65],
)
async def test_handle_rejects_invalid_arguments_without_fetching(
    context: SimpleNamespace,
    event: dict[str, Any],
    args: str,
) -> None:
    fetch = AsyncMock(return_value=github.segments("unexpected"))
    with patch.object(github, "_fetch_trending", new=fetch):
        result = await github.handle("github", args, event, context)
    assert result
    fetch.assert_not_awaited()


def test_private_parser_rejects_non_string_arguments() -> None:
    with pytest.raises(TypeError):
        github._parse_action(None)


@pytest.mark.asyncio
async def test_direct_fetch_is_pinned_bounded_and_saved(context: SimpleNamespace) -> None:
    fetch = AsyncMock(return_value=_safe_html(SAMPLE_TRENDING_HTML))
    with patch.object(github, "fetch_public_html", new=fetch):
        result = await github._fetch_trending("daily", context)

    rendered = str(result)
    assert "octocat/Hello-World" in rendered
    assert "78 stars today" in rendered
    assert (context.data_dir / "trending_daily_latest.json").is_file()
    kwargs = fetch.await_args.kwargs
    assert kwargs["timeout_seconds"] == 15
    assert kwargs["allowed_hosts"] == {"github.com"}
    assert "Authorization" not in kwargs["headers"]


@pytest.mark.asyncio
async def test_proxy_fetch_uses_exact_bounded_transport(context: SimpleNamespace) -> None:
    context.secrets["plugins"]["github"]["proxy"] = "http://proxy.example:8080"
    context.http_session                          = object()
    fetch = AsyncMock(return_value=_bounded_html(SAMPLE_TRENDING_HTML))
    with patch.object(github, "aiohttp_request_bounded", new=fetch):
        result = await github._fetch_trending("daily", context)

    assert "octocat/Hello-World" in str(result)
    args   = fetch.await_args.args
    kwargs = fetch.await_args.kwargs
    assert args[1:3] == ("GET", "https://github.com/trending?since=daily")
    assert kwargs["request_kwargs"] == {
        "proxy": "http://proxy.example:8080",
        "timeout": 15,
    }
    assert kwargs["limits"].max_decoded_bytes == github.MAX_HTML_BYTES
    assert kwargs["redirect_policy"].allowed_origins == {"https://github.com"}


@pytest.mark.asyncio
async def test_proxy_requires_http_session(context: SimpleNamespace) -> None:
    context.secrets["plugins"]["github"]["proxy"] = "http://proxy.example:8080"
    result                                        = await github._fetch_trending("daily", context)
    assert "XQ-PLUGIN-UNEXPECTED" in str(result)


@pytest.mark.asyncio
async def test_invalid_proxy_returns_actionable_configuration_error(
    context: SimpleNamespace,
) -> None:
    context.secrets["plugins"]["github"]["proxy"] = "ftp://proxy.example"
    result                                        = await github.handle("github", "", {}, context)
    assert "代理" in str(result)
    assert "XQ-PLUGIN-UNEXPECTED" not in str(result)


@pytest.mark.asyncio
async def test_fetch_none_and_empty_page_return_stable_messages(context: SimpleNamespace) -> None:
    with patch.object(github, "fetch_public_html", new=AsyncMock(return_value=None)):
        assert "无效响应" in str(await github._fetch_trending("daily", context))
    with patch.object(
        github,
        "fetch_public_html",
        new=AsyncMock(return_value=_safe_html("<html></html>")),
    ):
        assert "未找到" in str(await github._fetch_trending("daily", context))


@pytest.mark.asyncio
async def test_fetch_errors_are_bounded_for_manual_and_scheduled_paths(
    context: SimpleNamespace,
) -> None:
    with patch.object(
        github,
        "_download_trending_html",
        new=AsyncMock(side_effect=HttpStatusError(502)),
    ):
        assert "HTTP 502" in str(await github._fetch_trending("daily", context))
    with patch.object(
        github,
        "fetch_public_html",
        new=AsyncMock(side_effect=aiohttp.ClientError("network detail")),
    ):
        assert "XQ-PLUGIN-UNEXPECTED" in str(await github.scheduled(context))


@pytest.mark.asyncio
async def test_private_fetch_rejects_invalid_range_without_network(
    context: SimpleNamespace,
) -> None:
    fetch = AsyncMock(return_value=_safe_html(SAMPLE_TRENDING_HTML))
    with patch.object(github, "fetch_public_html", new=fetch):
        result = await github._fetch_trending("yearly", context)
    assert "XQ-PLUGIN-UNEXPECTED" in str(result)
    fetch.assert_not_awaited()


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("", ""),
        (None, ""),
        ("http://proxy.example:8080", "http://proxy.example:8080"),
        ("https://user:pass@proxy.example", "https://user:pass@proxy.example"),
    ],
)
def test_proxy_configuration_accepts_only_explicit_valid_values(
    context: SimpleNamespace,
    value: object,
    expected: str,
) -> None:
    context.secrets["plugins"]["github"]["proxy"] = value
    assert github._get_proxy(context) == expected


@pytest.mark.parametrize(
    "value",
    [
        False,
        {},
        " http://proxy.example",
        "http://proxy.example/path",
        "http://proxy.example?q=1",
        "http://proxy.example#fragment",
        "ftp://proxy.example",
        "http://",
        "http://proxy.example:99999",
        "http://proxy.example\x00",
        pytest.param("h" * 2_049, id="too-long"),
    ],
)
def test_proxy_configuration_rejects_malformed_values(
    context: SimpleNamespace,
    value: object,
) -> None:
    context.secrets["plugins"]["github"]["proxy"] = value
    with pytest.raises(ValueError):
        github._get_proxy(context)


def test_proxy_configuration_handles_missing_or_wrong_secret_shapes(
    context: SimpleNamespace,
) -> None:
    for secrets in ({}, {"plugins": []}, {"plugins": {"github": []}}):
        context.secrets = secrets
        assert github._get_proxy(context) == ""


def test_html_decode_accepts_utf8_ascii_and_rejects_other_boundaries(monkeypatch) -> None:
    assert github._decode_html("中文".encode(), "UTF-8") == "中文"
    assert github._decode_html(b"ascii", None) == "ascii"
    assert github._decode_html(b"ascii", "us-ascii") == "ascii"
    with pytest.raises(ValueError, match="bytes"):
        github._decode_html("not bytes", "utf-8")
    with pytest.raises(ValueError, match="charset"):
        github._decode_html(b"html", "utf-16")
    with pytest.raises(ValueError, match="charset"):
        github._decode_html(b"html", 123)
    monkeypatch.setattr(github, "MAX_HTML_BYTES", 3)
    with pytest.raises(ValueError, match="byte"):
        github._decode_html(b"four", "utf-8")


def test_parse_current_shape_and_period_stats() -> None:
    repositories = github._parse_trending_html(SAMPLE_TRENDING_HTML)
    assert len(repositories) == 2
    assert repositories[0] == {
        "owner": "octocat",
        "name": "Hello-World",
        "full_name": "octocat/Hello-World",
        "url": "https://github.com/octocat/Hello-World",
        "description": "A sample repository for testing",
        "language": "Python",
        "stars": "1234",
        "forks": "56",
        "stars_gained": "78 stars today",
    }
    assert repositories[1]["stars"] == "150k"
    assert repositories[1]["forks"] == "10000"
    assert repositories[1]["stars_gained"] == "123 stars this week"


@pytest.mark.parametrize("period", ["today", "this week", "this month"])
def test_parse_recognizes_all_official_gain_periods(period: str) -> None:
    html = f"""
    <article class="Box-row">
      <h2><a href="/owner/repo">owner/repo</a></h2>
      <span>1,234 stars {period}</span>
    </article>
    """
    assert github._parse_trending_html(html)[0]["stars_gained"] == f"1234 stars {period}"


def test_parse_uses_defaults_and_skips_bad_or_duplicate_paths() -> None:
    html = """
    <article class="Box-row"><h2><a href="/Owner/Repo">spoofed text</a></h2></article>
    <article class="Box-row"><h2><a href="/owner/repo">duplicate</a></h2></article>
    <article class="Box-row"><h2><a href="/single">bad</a></h2></article>
    <article class="Box-row"><h2><a href="https://evil.example/a/b">bad</a></h2></article>
    <article class="Box-row"><h2><a href="/owner/.">bad</a></h2></article>
    <article class="Box-row"><h2>missing link</h2></article>
    """
    repositories = github._parse_trending_html(html)
    assert len(repositories) == 1
    assert repositories[0]["full_name"] == "Owner/Repo"
    assert repositories[0]["description"] == "无描述"
    assert repositories[0]["language"] == "未知"
    assert repositories[0]["stars"] == "0"


def test_parse_removes_hidden_content_controls_and_bounds_fields() -> None:
    description = "visible\x00" + "x" * 400
    language    = "Py\x7fthon"
    html        = f"""
    <script>page secret</script>
    <article class="Box-row">
      <h2><a href="/owner/repo">owner/repo</a></h2>
      <p><script>field secret</script>{description}</p>
      <span itemprop="programmingLanguage">{language}</span>
    </article>
    """
    repository = github._parse_trending_html(html)[0]
    assert "secret" not in str(repository)
    assert "\x00" not in repository["description"]
    assert "\x7f" not in repository["language"]
    assert len(repository["description"]) == github.MAX_DESCRIPTION_CHARS


def test_parse_has_html_and_article_budgets(monkeypatch) -> None:
    monkeypatch.setattr(github, "MAX_HTML_CHARS", 3)
    with pytest.raises(ValueError, match="character"):
        github._parse_trending_html("four")
    with pytest.raises(TypeError):
        github._parse_trending_html(b"html")  # type: ignore[arg-type]

    monkeypatch.setattr(github, "MAX_HTML_CHARS", 2 * 1024 * 1024)
    articles = "".join(
        f'<article class="Box-row"><h2><a href="/o/r{index}">o/r{index}</a></h2></article>'
        for index in range(github.MAX_REPOSITORIES + 5)
    )
    assert len(github._parse_trending_html(articles)) == github.MAX_REPOSITORIES


def test_format_never_exceeds_onebot_text_budget() -> None:
    repositories = [
        _repository(index, description="x" * github.MAX_DESCRIPTION_CHARS) for index in range(1, 30)
    ]
    rendered = github._format_trending(
        repositories,
        "monthly",
        now=datetime(2030, 1, 1, tzinfo=UTC),
    )
    assert len(rendered) <= MAX_MESSAGE_TEXT_LENGTH
    assert "GitHub 每月趋势" in rendered
    assert rendered.count("https://github.com/") <= github.MAX_OUTPUT_REPOSITORIES


def test_save_history_writes_latest_snapshot_and_prunes_per_range(
    context: SimpleNamespace,
) -> None:
    history_dir = context.data_dir / "history"
    history_dir.mkdir()
    start = date(2020, 1, 1)
    for offset in range(github.MAX_HISTORY_FILES_PER_RANGE + 5):
        snapshot_date = start + timedelta(days=offset)
        (history_dir / f"trending_daily_{snapshot_date.isoformat()}.json").write_text("{}")
    unrelated = history_dir / "keep.txt"
    unrelated.write_text("keep", encoding="utf-8")

    github._save_history([_repository()], "daily", context)

    latest = json.loads(
        (context.data_dir / "trending_daily_latest.json").read_text(encoding="utf-8")
    )
    assert latest["time_range"] == "daily"
    assert latest["count"] == 1
    assert len(list(history_dir.glob("trending_daily_*.json"))) == 90
    assert unrelated.read_text(encoding="utf-8") == "keep"


def test_save_history_rejects_invalid_inputs(context: SimpleNamespace) -> None:
    with pytest.raises(ValueError, match="range"):
        github._save_history([_repository()], "yearly", context)
    with pytest.raises(ValueError, match="repositories"):
        github._save_history([{}] * (github.MAX_REPOSITORIES + 1), "daily", context)
    with pytest.raises(ValueError, match="repositories"):
        github._save_history([object()], "daily", context)  # type: ignore[list-item]


def test_save_history_refuses_symlinked_history_directory(
    context: SimpleNamespace,
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    history = context.data_dir / "history"
    try:
        history.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks are unavailable: {exc}")
    with pytest.raises(ValueError, match="real directory"):
        github._save_history([_repository()], "daily", context)
    assert not list(outside.iterdir())
