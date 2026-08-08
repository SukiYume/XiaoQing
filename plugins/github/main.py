"""抓取 GitHub 官方 Trending 页面，并保存有界历史快照。"""

from __future__ import annotations

import logging
import re
import stat
import threading
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Protocol, cast
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
from bs4.element import Tag

from core.args import tokenize
from core.bounded_http import (
    HTML_MIME_POLICY,
    BodyLimits,
    HttpStatusError,
    RedirectPolicy,
    aiohttp_request_bounded,
)
from core.clock import now_in_configured_timezone
from core.constants import MAX_MESSAGE_TEXT_LENGTH
from core.interfaces import PluginSettingsSnapshot
from core.plugin_base import (
    Segments,
    bounded_external_text,
    has_control_characters,
    run_sync,
    segments,
    write_json,
)
from core.public_errors import public_error_response
from core.safe_http import fetch_public_html

logger = logging.getLogger(__name__)

TimeRange = Literal["daily", "weekly", "monthly"]
RepositoryData = dict[str, str]

VALID_RANGES = frozenset({"daily", "weekly", "monthly"})
RANGE_NAMES: dict[TimeRange, str] = {
    "daily": "每日",
    "weekly": "每周",
    "monthly": "每月",
}

MAX_ARGUMENT_CHARS = 64
MAX_HTML_BYTES = 2 * 1024 * 1024
MAX_HTML_CHARS = 2 * 1024 * 1024
MAX_REPOSITORIES = 50
MAX_OUTPUT_REPOSITORIES = 10
MAX_DESCRIPTION_CHARS = 240
MAX_LANGUAGE_CHARS = 64
MAX_HISTORY_FILES_PER_RANGE = 90
MAX_PROXY_CHARS = 2_048

_HELP_ALIASES = frozenset({"help", "h", "帮助"})
_REPOSITORY_PATH_PATTERN = re.compile(
    r"/([A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?)/"
    r"([A-Za-z0-9_.-]{1,100})/?\Z"
)
_COUNT_PATTERN = re.compile(r"([0-9]+(?:,[0-9]{3})*)([kKmM]?)")
_GAIN_PATTERN = re.compile(
    r"\b([0-9]+(?:,[0-9]{3})*)\s+stars?\s+(today|this week|this month)\b",
    re.IGNORECASE,
)
_HISTORY_NAME_PATTERN = re.compile(
    r"trending_(daily|weekly|monthly)_[0-9]{4}-[0-9]{2}-[0-9]{2}\.json\Z"
)
_REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
}
_GITHUB_BODY_LIMITS = BodyLimits(
    max_wire_bytes=MAX_HTML_BYTES,
    max_decoded_bytes=MAX_HTML_BYTES,
    max_decompression_ratio=20,
    ratio_grace_bytes=64 * 1024,
    chunk_bytes=64 * 1024,
)
_GITHUB_PROXY_REDIRECTS = RedirectPolicy(
    max_hops=3,
    allowed_schemes=frozenset({"https"}),
    allowed_origins=frozenset({"https://github.com"}),
    same_origin_only=True,
)
_HISTORY_LOCK = threading.RLock()

HELP_TEXT = """📈 GitHub Trending

用法
/github  查看每日趋势
/github daily  查看每日趋势
/github weekly  查看每周趋势
/github monthly  查看每月趋势
/github help  显示帮助

每天 08:30 按调度器时区自动运行每日趋势任务。
"""


class _GitHubContext(Protocol):
    data_dir: Path
    http_session: Any

    def get_settings_snapshot(self) -> PluginSettingsSnapshot: ...


class GitHubCommandError(ValueError):
    """表示可直接向用户说明的命令格式错误。"""


def _parse_action(args: object) -> Literal["help", "daily", "weekly", "monthly"]:
    """完整消费这个单参数命令，不让多余 token 被静默忽略。"""

    if type(args) is not str:
        raise TypeError("github arguments must be a string")
    if len(args) > MAX_ARGUMENT_CHARS:
        raise GitHubCommandError(f"命令参数不能超过 {MAX_ARGUMENT_CHARS} 个字符")
    if has_control_characters(args, include_c1=True):
        raise GitHubCommandError("命令参数不能包含控制字符")
    try:
        tokens = tokenize(args)
    except ValueError as exc:
        raise GitHubCommandError("命令中的引号没有闭合") from exc
    if not tokens:
        return "daily"
    if len(tokens) != 1:
        raise GitHubCommandError("用法：/github [daily|weekly|monthly|help]")
    action = tokens[0].casefold()
    if action in _HELP_ALIASES:
        return "help"
    if action in VALID_RANGES:
        return cast(TimeRange, action)
    raise GitHubCommandError("未知时间范围；请使用 daily、weekly 或 monthly")


def _require_time_range(value: object) -> TimeRange:
    if type(value) is not str or value not in VALID_RANGES:
        raise ValueError("GitHub Trending time range is invalid")
    return cast(TimeRange, value)


def _get_proxy(context: _GitHubContext) -> str:
    """读取并验证管理员配置的 HTTP(S) 代理；代理值始终作为 secret 处理。"""

    raw_proxy = context.get_settings_snapshot().plugin_secrets("github").get("proxy")
    if raw_proxy is None:
        return ""
    if type(raw_proxy) is not str:
        raise GitHubCommandError("GitHub 代理配置必须是字符串")
    proxy = raw_proxy
    if not proxy:
        return ""
    if (
        len(proxy) > MAX_PROXY_CHARS
        or proxy != proxy.strip()
        or has_control_characters(proxy, include_c1=True)
    ):
        raise GitHubCommandError("GitHub 代理配置格式无效")
    try:
        parsed = urlsplit(proxy)
        port = parsed.port
    except ValueError as exc:
        raise GitHubCommandError("GitHub 代理 URL 无效") from exc
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not parsed.hostname
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or (port is not None and not 0 < port <= 65_535)
    ):
        raise GitHubCommandError("GitHub 代理 URL 无效")
    return proxy


async def handle(
    command: str,
    args: str,
    event: dict[str, Any],
    context: _GitHubContext,
) -> Segments:
    """处理手动查询；清单已经完成命令别名解析。"""

    del command, event
    try:
        action = _parse_action(args)
        if action == "help":
            return segments(HELP_TEXT)
        return await _fetch_trending(action, context)
    except GitHubCommandError as exc:
        return segments(str(exc))
    except Exception as exc:
        return public_error_response(context, exc, logger=logger, component="github.handle")


async def scheduled(context: _GitHubContext) -> Segments:
    """执行清单中的每日 08:30 趋势任务。"""

    return await _fetch_trending("daily", context)


def _decode_html(body: object, charset: object) -> str:
    """只解码 GitHub 当前使用的 UTF-8/ASCII 页面，并再次核对独立预算。"""

    if not isinstance(body, bytes):
        raise ValueError("GitHub HTML body must be bytes")
    if len(body) > MAX_HTML_BYTES:
        raise ValueError("GitHub HTML byte limit exceeded")
    if charset is None:
        encoding = "utf-8"
    elif type(charset) is str:
        encoding = charset.strip().casefold()
    else:
        raise ValueError("GitHub HTML charset must be a string")
    if encoding not in {"utf-8", "utf8", "us-ascii", "ascii"}:
        raise ValueError("GitHub HTML charset is not allowed")
    html = body.decode(encoding, errors="replace")
    if len(html) > MAX_HTML_CHARS:
        raise ValueError("GitHub HTML character limit exceeded")
    return html


async def _download_trending_html(context: _GitHubContext, time_range: TimeRange) -> str | None:
    """根据是否配置代理选择一条有界传输路径，目标始终固定为 GitHub。"""

    url = f"https://github.com/trending?since={time_range}"
    proxy = _get_proxy(context)
    if not proxy:
        fetched = await fetch_public_html(
            url,
            headers=_REQUEST_HEADERS,
            timeout_seconds=15,
            allowed_hosts={"github.com"},
        )
        if fetched is None:
            return None
        body = fetched.body
        charset = fetched.charset
    else:
        if context.http_session is None:
            raise RuntimeError("GitHub proxy requires an HTTP session")
        response = await aiohttp_request_bounded(
            context.http_session,
            "GET",
            url,
            limits=_GITHUB_BODY_LIMITS,
            mime_policy=HTML_MIME_POLICY,
            redirect_policy=_GITHUB_PROXY_REDIRECTS,
            headers=_REQUEST_HEADERS,
            request_kwargs={"proxy": proxy, "timeout": 15},
        )
        body = response.body
        charset = response.charset
    return _decode_html(body, charset)


async def _fetch_trending(time_range: str, context: _GitHubContext) -> Segments:
    """下载、解析、保存并格式化一个完整趋势事务。"""

    try:
        normalized_range = _require_time_range(time_range)
        html = await _download_trending_html(context, normalized_range)
        if html is None:
            return segments("❌ GitHub 返回了无效响应")
        repositories = await run_sync(_parse_trending_html, html)
        if not repositories:
            return segments("❌ 未找到趋势项目")
        now = now_in_configured_timezone(context)
        await run_sync(_save_history, repositories, normalized_range, context, now)
        return segments(_format_trending(repositories, normalized_range, now=now))
    except HttpStatusError as exc:
        return segments(f"❌ HTTP {exc.status}")
    except GitHubCommandError as exc:
        return segments(str(exc))
    except Exception as exc:
        return public_error_response(context, exc, logger=logger, component="github.fetch")


def _clean_element_text(element: Tag | None, *, fallback: str, max_chars: int) -> str:
    if element is None:
        return fallback
    raw = element.get_text(" ", strip=True)
    visible = bounded_external_text(
        raw,
        max_chars=max_chars,
        max_bytes=max_chars * 4,
        default=fallback,
    )
    cleaned = re.sub(r"\s+", " ", visible).strip()
    return cleaned or fallback


def _normalize_count(value: str) -> str:
    match = _COUNT_PATTERN.search(value)
    if match is None:
        return "0"
    digits = match.group(1).replace(",", "")
    if len(digits) > 15:
        return "0"
    return f"{digits}{match.group(2).casefold()}"


def _extract_link_count(article: Tag, repository_path: str, suffix: str) -> str:
    link = article.select_one(f'a[href="{repository_path}/{suffix}"]')
    return _normalize_count(_clean_element_text(link, fallback="0", max_chars=64))


def _extract_gained_stars(article: Tag) -> str:
    for element in article.select("span")[:100]:
        candidate = _clean_element_text(element, fallback="", max_chars=128)
        match = _GAIN_PATTERN.search(candidate)
        if match is not None:
            count = _normalize_count(match.group(1))
            return f"{count} stars {match.group(2).casefold()}"
    return ""


def _parse_repository_article(article: Tag) -> RepositoryData | None:
    link = article.select_one("h2 a[href]")
    href = link.get("href") if link is not None else None
    if not isinstance(href, str):
        return None
    match = _REPOSITORY_PATH_PATTERN.fullmatch(href)
    if match is None:
        return None
    owner, name = match.groups()
    if name in {".", ".."}:
        return None
    repository_path = f"/{owner}/{name}"
    return {
        "owner": owner,
        "name": name,
        "full_name": f"{owner}/{name}",
        "url": f"https://github.com{repository_path}",
        "description": _clean_element_text(
            article.select_one("p"),
            fallback="无描述",
            max_chars=MAX_DESCRIPTION_CHARS,
        ),
        "language": _clean_element_text(
            article.select_one("span[itemprop='programmingLanguage']"),
            fallback="未知",
            max_chars=MAX_LANGUAGE_CHARS,
        ),
        "stars": _extract_link_count(article, repository_path, "stargazers"),
        "forks": _extract_link_count(article, repository_path, "forks"),
        "stars_gained": _extract_gained_stars(article),
    }


def _parse_trending_html(html: str) -> list[RepositoryData]:
    """一次解析有界 HTML；按仓库名去重并忽略结构异常的文章。"""

    if not isinstance(html, str):
        raise TypeError("GitHub Trending HTML must be a string")
    if len(html) > MAX_HTML_CHARS:
        raise ValueError("GitHub Trending HTML character limit exceeded")
    soup = BeautifulSoup(html, "html.parser")
    for hidden in soup.find_all(("script", "style", "template", "noscript")):
        hidden.decompose()
    repositories: list[RepositoryData] = []
    seen: set[str] = set()
    for article in soup.select("article.Box-row")[:MAX_REPOSITORIES]:
        repository = _parse_repository_article(article)
        if repository is None:
            continue
        identity = repository["full_name"].casefold()
        if identity in seen:
            continue
        repositories.append(repository)
        seen.add(identity)
    return repositories


def _format_trending(
    repositories: Sequence[Mapping[str, str]],
    time_range: TimeRange,
    *,
    now: datetime,
) -> str:
    """在单条 QQ 文本预算内按页面顺序格式化最多十个仓库。"""

    today = now.strftime("%Y-%m-%d")
    output = f"📈 GitHub {RANGE_NAMES[time_range]}趋势 ({today})\n"
    for index, repository in enumerate(repositories[:MAX_OUTPUT_REPOSITORIES], 1):
        stars = repository.get("stars_gained") or repository.get("stars", "0")
        block = (
            f"\n{index}. {repository['full_name']} ({repository['language']})\n"
            f"   ⭐ {stars}\n"
            f"   📝 {repository['description']}\n"
            f"   🔗 {repository['url']}\n"
        )
        if len(output) + len(block) > MAX_MESSAGE_TEXT_LENGTH:
            break
        output += block
    return output.rstrip()


def _prune_history(history_dir: Path, time_range: TimeRange) -> None:
    """每个时间范围只保留按文件名排序最新的有限快照。"""

    candidates: list[Path] = []
    for path in history_dir.iterdir():
        match = _HISTORY_NAME_PATTERN.fullmatch(path.name)
        if match is None or match.group(1) != time_range or path.is_symlink():
            continue
        try:
            if stat.S_ISREG(path.lstat().st_mode):
                candidates.append(path)
        except FileNotFoundError:
            continue
    candidates.sort(key=lambda path: path.name, reverse=True)
    for expired in candidates[MAX_HISTORY_FILES_PER_RANGE:]:
        expired.unlink(missing_ok=True)


def _save_history(
    repositories: Sequence[Mapping[str, Any]],
    time_range: str,
    context: _GitHubContext,
    now: datetime | None = None,
) -> None:
    """原子更新 latest/当日快照，并在同一锁内执行保留策略。"""

    normalized_range = _require_time_range(time_range)
    if len(repositories) > MAX_REPOSITORIES or any(
        not isinstance(repository, Mapping) for repository in repositories
    ):
        raise ValueError("GitHub history repositories are invalid")
    data_dir = Path(context.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    if not stat.S_ISDIR(data_dir.lstat().st_mode):
        raise ValueError("GitHub data directory must be a real directory")
    history_dir = data_dir / "history"
    history_dir.mkdir(parents=True, exist_ok=True)
    if not stat.S_ISDIR(history_dir.lstat().st_mode):
        raise ValueError("GitHub history directory must be a real directory")

    current = now if now is not None else now_in_configured_timezone(context)
    payload = {
        "date": current.isoformat(),
        "time_range": normalized_range,
        "count": len(repositories),
        "repositories": [dict(repository) for repository in repositories],
    }
    date = current.strftime("%Y-%m-%d")
    with _HISTORY_LOCK:
        write_json(data_dir / f"trending_{normalized_range}_latest.json", payload)
        write_json(history_dir / f"trending_{normalized_range}_{date}.json", payload)
        _prune_history(history_dir, normalized_range)
