"""从中国地震台网速报微博获取快讯，并可靠投递定时通知。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import shlex
import stat
import threading
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, Literal, Protocol, cast
from urllib.parse import urlsplit

import requests
from bs4 import BeautifulSoup

from core.bounded_file_cache import BoundedFileCache, FileCacheLimits
from core.bounded_http import (
    JSON_MIME_POLICY,
    NO_REDIRECTS,
    BodyLimits,
    JsonLimits,
    MimePolicy,
    ResponseFormatError,
    ResponseLimitError,
    parse_bounded_json,
    requests_request_bounded,
)
from core.delivery import DeliveryReceipt, send_with_receipt
from core.durable_fanout import (
    PendingFanout,
    clear_pending,
    create_pending,
    default_group_targets,
    load_pending,
    mark_delivered,
)
from core.image_validation import (
    ImageValidationError,
    ImageValidationLimits,
    validate_image_bytes,
)
from core.plugin_base import (
    Segments,
    bounded_external_text,
    build_action,
    gather_bounded,
    has_control_characters,
    image,
    run_sync,
    segments,
    text,
    write_json,
)
from core.public_errors import public_error_message, public_error_response
from core.safe_http import SafeHttpError, SafeHttpResponse, fetch_public_bytes

logger = logging.getLogger(__name__)

# 用户参数和微博字段都来自不可信输入；在进入解析器前先施加独立硬上限。
MAX_ARGUMENT_CHARS      = 64
MAX_RAW_TEXT_CHARS      = 64 * 1024
MAX_CLEAN_TEXT_CHARS    = 16 * 1024
MAX_CLEAN_TEXT_BYTES    = 64 * 1024
MAX_FIGURE_URL_CHARS    = 2_048
MAX_STATE_BYTES         = 4 * 1024
MAX_IMAGE_BYTES         = 8 * 1024 * 1024
MAX_IMAGE_DIMENSION     = 16_384
MAX_IMAGE_PIXELS        = 32_000_000
MAX_DECODED_IMAGE_BYTES = 128 * 1024 * 1024

WEIBO_UID            = "1904228041"
CONTAINER_ID         = "1076031904228041"
_MAX_WEIBO_CARDS     = 200
_MAX_SINCE_ID_DIGITS = 32
_MIN_PUSH_MAGNITUDE  = 4.0
_EARTHQUAKE_MARKERS  = ("#地震快讯#", "中国地震台网正式测定")
_HELP_ALIASES        = frozenset({"help", "h", "帮助"})
_LATEST_ALIASES      = frozenset({"latest", "最新"})
_MAGNITUDE_PATTERN   = re.compile(r"发生([0-9]{1,2}(?:\.[0-9]{1,2})?)级地震")
_DECIMAL_ID_PATTERN  = re.compile(rf"[0-9]{{1,{_MAX_SINCE_ID_DIGITS}}}\Z")

_IMAGE_HOSTS = frozenset(
    {
        "wx1.sinaimg.cn",
        "wx2.sinaimg.cn",
        "wx3.sinaimg.cn",
        "wx4.sinaimg.cn",
    }
)
_IMAGE_MIME_FORMATS = {
    "image/jpeg": ("JPEG", ".jpg"),
    "image/png": ("PNG", ".png"),
    "image/webp": ("WEBP", ".webp"),
}
_IMAGE_FORMAT_EXTENSIONS = dict(_IMAGE_MIME_FORMATS.values())
_IMAGE_FORMAT_MODES      = {
    "JPEG": frozenset({"L", "RGB", "CMYK", "YCbCr"}),
    "PNG": frozenset({"1", "L", "LA", "P", "RGB", "RGBA"}),
    "WEBP": frozenset({"RGB", "RGBA"}),
}
_IMAGE_CACHE_LIMITS = FileCacheLimits(
    max_entries = 64,
    max_bytes   = 64 * 1024 * 1024,
    ttl_seconds = 30 * 24 * 60 * 60,
)

_VISITOR_MIME_POLICY = MimePolicy(
    exact=frozenset(
        {
            "application/javascript",
            "application/x-javascript",
            "text/javascript",
        }
    )
)
_VISITOR_BODY_LIMITS = BodyLimits(
    max_wire_bytes          = 256 * 1024,
    max_decoded_bytes       = 512 * 1024,
    max_decompression_ratio = 20,
    ratio_grace_bytes       = 16 * 1024,
    chunk_bytes             = 32 * 1024,
)
_CONFIG_BODY_LIMITS = BodyLimits(
    max_wire_bytes          = 256 * 1024,
    max_decoded_bytes       = 512 * 1024,
    max_decompression_ratio = 20,
    ratio_grace_bytes       = 16 * 1024,
    chunk_bytes             = 32 * 1024,
)
_CONFIG_JSON_LIMITS = JsonLimits(
    max_bytes        = _CONFIG_BODY_LIMITS.max_decoded_bytes,
    max_depth        = 12,
    max_nodes        = 5_000,
    max_string_chars = 128 * 1024,
    max_number_chars = 128,
)
_INDEX_BODY_LIMITS = BodyLimits(
    max_wire_bytes          = 1024 * 1024,
    max_decoded_bytes       = 2 * 1024 * 1024,
    max_decompression_ratio = 20,
    ratio_grace_bytes       = 32 * 1024,
    chunk_bytes             = 64 * 1024,
)
_INDEX_JSON_LIMITS = JsonLimits(
    max_bytes        = _INDEX_BODY_LIMITS.max_decoded_bytes,
    max_depth        = 24,
    max_nodes        = 30_000,
    max_string_chars = 512 * 1024,
    max_number_chars = 128,
)
_WEIBO_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
# 这些字段来自当前微博访客端点契约，不接受用户配置覆盖。若端点变更，连续失败会在
# 达到阈值后升级日志等级，避免长期静默重试。
_WEIBO_VISITOR_VERSION        = "20250916"
_WEIBO_VISITOR_RID            = "01Cn_5z8ew6CZHvNiTdPeyK2Qf740"
_BOOTSTRAP_FAILURE_ESCALATION = 3
_WEIBO_SESSION_TTL_SECONDS    = 15 * 60
_WEIBO_SESSION_STATE_KEY      = "earthquake_weibo_session"
_IMAGE_HEADERS                = {
    "User-Agent": _WEIBO_USER_AGENT,
    "Referer": "https://m.weibo.cn/",
    "Accept": "image/*",
    "Accept-Encoding": "identity",
}

_PENDING_CURSOR_KEY    = "earthquake_pending_since"
_PENDING_EVENT_IDS_KEY = "earthquake_pending_event_ids"
_DELIVERY_LOCK         = asyncio.Lock()
_WEIBO_SESSION_LOCK    = threading.Lock()

HELP_TEXT = """🌏 地震快讯

从“中国地震台网速报”微博获取最近一条地震快讯。

命令
/earthquake 或 /地震  获取最近快讯
/earthquake latest  获取最近快讯
/earthquake help  显示帮助

说明
手动查询不限震级，也不会推进定时任务游标。
定时任务每 5 分钟检查一次，仅投递 4.0 级及以上快讯。
微博访客接口临时不可用时，会在下次轮询重试。"""


class _EarthquakeContext(Protocol):
    """本插件实际使用的最小上下文接口。"""

    data_dir: Path
    state: dict[str, Any]
    send_action: Callable[[dict[str, Any]], Awaitable[Any]]

    def default_groups(self) -> list[int]: ...


@dataclass(frozen=True, slots=True)
class _PreparedCard:
    """一条已经消毒、可以直接渲染的地震快讯。"""

    clean_text: str
    magnitude: float | None
    figure_url: str | None
    event_id: str = ""


@dataclass(frozen=True, slots=True)
class _FetchBatch:
    """一次扫描结果，以及扫描完成后才允许提交的游标。"""

    cards: tuple[_PreparedCard, ...]
    newest_seen_id: str

    @property
    def event_ids(self) -> tuple[str, ...]:
        return tuple(card.event_id for card in self.cards if card.event_id)


class EarthquakeCommandError(ValueError):
    """表示可以直接向用户说明的命令格式错误。"""


class EarthquakeStateCorruptionError(RuntimeError):
    """表示活动游标及检查点都已无法信任。"""


def _parse_action(args: object) -> Literal["help", "latest"]:
    """解析这个只有两个动作的命令，避免通用解析器产生隐式默认分支。"""

    if not isinstance(args, str):
        raise TypeError("earthquake arguments must be a string")
    if len(args) > MAX_ARGUMENT_CHARS:
        raise EarthquakeCommandError(f"命令参数不能超过 {MAX_ARGUMENT_CHARS} 个字符")
    if has_control_characters(args, include_c1=True):
        raise EarthquakeCommandError("命令参数不能包含控制字符")
    try:
        tokens = shlex.split(args, posix=True)
    except ValueError as exc:
        raise EarthquakeCommandError("命令中的引号没有闭合") from exc
    if not tokens:
        return "latest"
    if len(tokens) != 1:
        raise EarthquakeCommandError("用法：/earthquake [latest|help]")
    action = tokens[0].casefold()
    if action in _HELP_ALIASES:
        return "help"
    if action in _LATEST_ALIASES:
        return "latest"
    raise EarthquakeCommandError("未知子命令；请使用 latest 或 help")


# ---------------------------------------------------------------------------
# 游标状态
# ---------------------------------------------------------------------------


def _since_path(context: _EarthquakeContext) -> Path:
    return context.data_dir / "earthquake.json"


def _checkpoint_path(context: _EarthquakeContext) -> Path:
    return context.data_dir / "earthquake.checkpoint.json"


def _fanout_path(context: _EarthquakeContext) -> Path:
    return context.data_dir / "earthquake_delivery.json"


def _validate_since_payload(data: object) -> str:
    """只接受单字段 JSON 对象及非负 ASCII 十进制游标。"""

    if not isinstance(data, dict) or set(data) != {"since_id"}:
        raise ValueError("earthquake state must contain only since_id")
    raw_since_id = data["since_id"]
    if type(raw_since_id) is int:
        candidate = str(raw_since_id)
    elif type(raw_since_id) is str:
        candidate = raw_since_id
    else:
        raise ValueError("earthquake since_id must be an integer or string")
    if _DECIMAL_ID_PATTERN.fullmatch(candidate) is None:
        raise ValueError("earthquake since_id must be an ASCII decimal value")
    return str(int(candidate))


def _read_since_file(path: Path) -> str:
    """有界读取普通文件；拒绝链接、设备文件和读取期间发生变化的文件。"""

    file_info = path.lstat()
    if not stat.S_ISREG(file_info.st_mode) or file_info.st_size > MAX_STATE_BYTES:
        raise ValueError("earthquake state file is not a bounded regular file")
    payload = path.read_bytes()
    if len(payload) != file_info.st_size:
        raise ValueError("earthquake state changed while reading")
    decoded: object = json.loads(payload.decode("utf-8"))
    return _validate_since_payload(decoded)


def _quarantine_corrupt_state(path: Path) -> Path | None:
    """原子移走损坏文件，不再为了生成摘要而读取任意大小的内容。"""

    quarantine = path.with_name(f"{path.name}.corrupt-{uuid.uuid4().hex[:16]}")
    try:
        return path.replace(quarantine)
    except FileNotFoundError:
        return None


def _restore_from_checkpoint(
    context: _EarthquakeContext,
    active_path: Path,
    *,
    initialize_when_absent: bool,
) -> str:
    try:
        recovered = _read_since_file(_checkpoint_path(context))
    except FileNotFoundError as exc:
        if initialize_when_absent:
            _save_since(context, "0")
            return "0"
        raise EarthquakeStateCorruptionError(
            "earthquake cursor is corrupt and no checkpoint is available"
        ) from exc
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, OSError, TypeError) as exc:
        raise EarthquakeStateCorruptionError("earthquake checkpoint is invalid") from exc
    write_json(active_path, {"since_id": recovered})
    return recovered


def _load_since(context: _EarthquakeContext) -> str:
    """读取活动游标；损坏时隔离原件并从检查点恢复。"""

    path = _since_path(context)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        return _read_since_file(path)
    except FileNotFoundError:
        return _restore_from_checkpoint(context, path, initialize_when_absent=True)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, OSError, TypeError) as exc:
        public_error_message(context, exc, logger=logger, component="earthquake.load_state")
        _quarantine_corrupt_state(path)
        return _restore_from_checkpoint(context, path, initialize_when_absent=False)


def _save_since(context: _EarthquakeContext, since_id: str | int) -> None:
    """先写活动游标，再写恢复检查点；两份文件均使用原子替换。"""

    normalized = _validate_since_payload({"since_id": since_id})
    path       = _since_path(context)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, {"since_id": normalized})
    write_json(_checkpoint_path(context), {"since_id": normalized})


# ---------------------------------------------------------------------------
# 微博请求与响应整形
# ---------------------------------------------------------------------------


def _create_session() -> requests.Session:
    """只创建会话；所有网络 I/O 都在同一个有界工作线程内完成。"""

    return requests.Session()


def _close_session(session: object) -> None:
    close = getattr(session, "close", None)
    if callable(close):
        try:
            close()
        except Exception as exc:
            logger.debug("Weibo visitor session close failed: error_type=%s", type(exc).__name__)


def _get_cached_session(context: _EarthquakeContext) -> tuple[requests.Session, bool]:
    """取得插件代内共享的微博会话，并报告是否需要重新 bootstrap。"""

    now    = time.monotonic()
    record = context.state.get(_WEIBO_SESSION_STATE_KEY)
    if isinstance(record, dict):
        session    = record.get("session")
        expires_at = record.get("expires_at")
        if session is not None and isinstance(expires_at, (int, float)) and expires_at > now:
            return cast(requests.Session, session), not bool(record.get("bootstrapped"))
        if session is not None:
            _close_session(session)

    session                                 = _create_session()
    context.state[_WEIBO_SESSION_STATE_KEY] = {
        "session": session,
        "expires_at": now + _WEIBO_SESSION_TTL_SECONDS,
        "bootstrapped": False,
    }
    return session, True


def _record_bootstrap_result(
    context: _EarthquakeContext,
    session: requests.Session,
    *,
    succeeded: bool,
) -> None:
    record = context.state.get(_WEIBO_SESSION_STATE_KEY)
    if not isinstance(record, dict) or record.get("session") is not session:
        return
    record["bootstrapped"] = succeeded
    if succeeded:
        record["expires_at"] = time.monotonic() + _WEIBO_SESSION_TTL_SECONDS


def _mark_session_for_retry(context: _EarthquakeContext, session: requests.Session) -> None:
    record = context.state.get(_WEIBO_SESSION_STATE_KEY)
    if isinstance(record, dict) and record.get("session") is session:
        record["bootstrapped"] = False


def _close_cached_session(context: _EarthquakeContext) -> None:
    with _WEIBO_SESSION_LOCK:
        record = context.state.pop(_WEIBO_SESSION_STATE_KEY, None)
        if isinstance(record, dict) and record.get("session") is not None:
            _close_session(record["session"])


def _weibo_headers(*, referer: str = "https://m.weibo.cn/") -> dict[str, str]:
    return {"User-Agent": _WEIBO_USER_AGENT, "Referer": referer}


def _validate_api_envelope(payload: object, *, require_cards: bool) -> dict[str, Any]:
    """校验微博统一响应外壳；卡片节点由后续单次遍历负责。"""

    if not isinstance(payload, dict):
        raise ResponseFormatError("Weibo API response must be an object")
    root = cast(dict[str, Any], payload)
    if type(root.get("ok")) is not int or root["ok"] != 1:
        raise ResponseFormatError("Weibo API returned an unsuccessful envelope")
    data = root.get("data")
    if not isinstance(data, dict):
        raise ResponseFormatError("Weibo API data must be an object")
    if require_cards:
        cards = data.get("cards")
        if not isinstance(cards, list):
            raise ResponseFormatError("Weibo API cards must be an array")
        if len(cards) > _MAX_WEIBO_CARDS:
            raise ResponseLimitError("Weibo API returned too many cards")
    return root


def _iter_mblogs(cards: object) -> list[dict[str, Any]]:
    """按显示顺序展开顶层及分组卡片，并对所有嵌套节点共享同一预算。"""

    if not isinstance(cards, list):
        raise ResponseFormatError("Weibo API cards must be an array")
    nodes: list[object] = list(cards)
    if len(nodes) > _MAX_WEIBO_CARDS:
        raise ResponseLimitError("Weibo API returned too many card nodes")
    mblogs: list[dict[str, Any]] = []
    index                        = 0
    while index < len(nodes):
        card = nodes[index]
        index += 1
        if not isinstance(card, dict):
            raise ResponseFormatError("Weibo API card must be an object")
        direct = card.get("mblog")
        if direct is not None:
            if not isinstance(direct, dict):
                raise ResponseFormatError("Weibo API mblog must be an object")
            mblogs.append(cast(dict[str, Any], direct))
        group = card.get("card_group")
        if group is not None:
            if not isinstance(group, list):
                raise ResponseFormatError("Weibo API card_group must be an array")
            if len(nodes) + len(group) > _MAX_WEIBO_CARDS:
                raise ResponseLimitError("Weibo API returned too many nested card nodes")
            nodes[index:index] = group
    return mblogs


def _report_bootstrap_error(context: _EarthquakeContext | None, exc: Exception) -> None:
    if context is not None:
        public_error_message(context, exc, logger=logger, component="earthquake.bootstrap")
        return
    logger.warning(
        "Failed to bootstrap Weibo visitor session: error_type=%s",
        type(exc).__name__,
    )


def _bootstrap_session(
    session: requests.Session,
    context: _EarthquakeContext | None = None,
) -> bool:
    """尽力建立访客 Cookie；失败不降低后续索引响应的边界校验。"""

    visitor_url  = "https://visitor.passport.weibo.cn/visitor/genvisitor2"
    visitor_data = {
        "cb": "visitor_gray_callback",
        "ver": _WEIBO_VISITOR_VERSION,
        "request_id": uuid.uuid4().hex,
        "tid": "",
        "from": "weibo",
        "webdriver": "false",
        "rid": _WEIBO_VISITOR_RID,
        "return_url": f"https://m.weibo.cn/u/{WEIBO_UID}",
    }
    headers   = _weibo_headers()
    succeeded = True
    try:
        requests_request_bounded(
            "POST",
            visitor_url,
            session               = session,
            headers               = headers,
            limits                = _VISITOR_BODY_LIMITS,
            mime_policy           = _VISITOR_MIME_POLICY,
            redirect_policy       = NO_REDIRECTS,
            request_kwargs        = {"data": visitor_data, "timeout": (5.0, 15.0)},
            total_timeout_seconds = 20.0,
        )
    except Exception as exc:
        succeeded = False
        _report_bootstrap_error(context, exc)

    try:
        response = requests_request_bounded(
            "GET",
            "https://m.weibo.cn/api/config",
            session               = session,
            headers               = headers,
            limits                = _CONFIG_BODY_LIMITS,
            mime_policy           = JSON_MIME_POLICY,
            redirect_policy       = NO_REDIRECTS,
            request_kwargs        = {"timeout": (5.0, 15.0)},
            total_timeout_seconds = 20.0,
        )
        _validate_api_envelope(
            parse_bounded_json(response, limits=_CONFIG_JSON_LIMITS),
            require_cards=False,
        )
    except Exception as exc:
        succeeded = False
        _report_bootstrap_error(context, exc)
    if context is not None and isinstance(getattr(context, "state", None), dict):
        if succeeded:
            context.state.pop("earthquake_bootstrap_failures", None)
        else:
            failures = context.state.get("earthquake_bootstrap_failures", 0)
            failures = failures + 1 if type(failures) is int and failures >= 0 else 1
            context.state["earthquake_bootstrap_failures"] = failures
            if failures >= _BOOTSTRAP_FAILURE_ESCALATION:
                logger.error(
                    "Weibo visitor bootstrap has failed consecutively: failures=%s threshold=%s",
                    failures,
                    _BOOTSTRAP_FAILURE_ESCALATION,
                )
    return succeeded


def _fetch_weibo(session: requests.Session) -> list[dict[str, Any]]:
    """有界获取索引，并在一次遍历中展开所有微博卡片。"""

    headers = _weibo_headers(referer=f"https://m.weibo.cn/u/{WEIBO_UID}")
    headers["X-Requested-With"] = "XMLHttpRequest"
    response                    = requests_request_bounded(
        "GET",
        "https://m.weibo.cn/api/container/getIndex",
        session         = session,
        headers         = headers,
        limits          = _INDEX_BODY_LIMITS,
        mime_policy     = JSON_MIME_POLICY,
        redirect_policy = NO_REDIRECTS,
        request_kwargs  = {
            "params": {"type": "uid", "value": WEIBO_UID, "containerid": CONTAINER_ID},
            "timeout": (5.0, 15.0),
        },
        total_timeout_seconds=20.0,
    )
    root = _validate_api_envelope(
        parse_bounded_json(response, limits=_INDEX_JSON_LIMITS),
        require_cards=True,
    )
    data = cast(dict[str, Any], root["data"])
    return _iter_mblogs(data["cards"])


# ---------------------------------------------------------------------------
# 快讯字段清理与筛选
# ---------------------------------------------------------------------------


def _extract_clean_text(raw_text: str) -> str:
    """删除不可见节点、HTML 标签和控制字符，保留可读的正文顺序。"""

    if not isinstance(raw_text, str):
        raise ResponseFormatError("earthquake text must be a string")
    if len(raw_text) > MAX_RAW_TEXT_CHARS:
        raise ResponseLimitError("earthquake raw text limit exceeded")
    parsed = BeautifulSoup(raw_text, "html.parser")
    for hidden in parsed.find_all(("script", "style", "noscript", "template")):
        hidden.decompose()
    visible = str(parsed.get_text(" ", strip=True))
    collapsed = re.sub(r"[\s\u200b]+", " ", visible).strip()
    cleaned   = bounded_external_text(
        collapsed,
        max_chars = MAX_CLEAN_TEXT_CHARS,
        max_bytes = MAX_CLEAN_TEXT_BYTES,
        suffix    = "",
        truncate  = False,
    )
    if collapsed and not cleaned:
        raise ResponseLimitError("earthquake clean text limit exceeded")
    return cleaned


def _extract_magnitude(clean_text: str) -> float | None:
    """提取有限长度的 ASCII 震级；明显越界的数值视为无效。"""

    match = _MAGNITUDE_PATTERN.search(clean_text)
    if match is None:
        return None
    magnitude = float(match.group(1))
    return magnitude if 0 <= magnitude <= 12 else None


def _normalize_event_id(value: object) -> tuple[str, int] | None:
    if type(value) is int:
        candidate = str(value)
    elif type(value) is str:
        candidate = value
    else:
        return None
    if _DECIMAL_ID_PATTERN.fullmatch(candidate) is None:
        return None
    normalized = str(int(candidate))
    numeric    = int(normalized)
    return (normalized, numeric) if numeric > 0 else None


def _normalize_figure_url(value: object) -> str | None:
    if not isinstance(value, str) or not value or len(value) > MAX_FIGURE_URL_CHARS:
        return None
    if has_control_characters(value, include_c1=True):
        return None
    try:
        parsed = urlsplit(value)
        host   = (parsed.hostname or "").rstrip(".").casefold()
        port   = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme.casefold() != "https"
        or host not in _IMAGE_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
    ):
        return None
    return value


def _prepare_mblog(mblog: Mapping[str, Any]) -> _PreparedCard | None:
    """把单条外部微博转换为受信内部结构；畸形单条数据不会中断整页扫描。"""

    event    = _normalize_event_id(mblog.get("id"))
    raw_text = mblog.get("text")
    if event is None or not isinstance(raw_text, str):
        return None
    try:
        clean_text = _extract_clean_text(raw_text)
    except (ResponseFormatError, ResponseLimitError):
        logger.warning("Skipped malformed earthquake card: event_id_valid=true")
        return None
    if not clean_text or any(marker not in clean_text for marker in _EARTHQUAKE_MARKERS):
        return None
    return _PreparedCard(
        clean_text = clean_text,
        magnitude  = _extract_magnitude(clean_text),
        figure_url = _normalize_figure_url(mblog.get("original_pic")),
        event_id   = event[0],
    )


def _select_earthquakes(
    mblogs: Sequence[Mapping[str, Any]],
    *,
    since_id: str,
    force: bool,
) -> _FetchBatch:
    """完整扫描有界 feed；去重排序后取最近一条或返回全部新事件。"""

    since_numeric                            = int(since_id)
    prepared_by_id: dict[int, _PreparedCard] = {}
    for mblog in mblogs:
        prepared = _prepare_mblog(mblog)
        if prepared is None:
            continue
        event_numeric = int(prepared.event_id)
        prepared_by_id.setdefault(event_numeric, prepared)

    ordered = sorted(prepared_by_id.items())
    if force:
        cards = (ordered[-1][1],) if ordered else ()
        return _FetchBatch(cards, since_id)

    new_events = [
        (event_id, prepared) for event_id, prepared in ordered if event_id > since_numeric
    ]
    newest_seen = max((event_id for event_id, _prepared in new_events), default=since_numeric)
    selected: list[_PreparedCard] = []
    for _event_id, prepared in new_events:
        if prepared.magnitude is not None and prepared.magnitude >= _MIN_PUSH_MAGNITUDE:
            selected.append(prepared)
        else:
            logger.info(
                "Skipped earthquake below push threshold: magnitude=%s",
                f"{prepared.magnitude:.1f}" if prepared.magnitude is not None else "unknown",
            )
    return _FetchBatch(tuple(selected), str(newest_seen))


def _fetch_batch(
    context: _EarthquakeContext,
    *,
    since_id: str,
    force: bool,
) -> _FetchBatch:
    """在同一个工作线程和同一个 requests 会话中完成一次微博扫描。"""

    with _WEIBO_SESSION_LOCK:
        session, needs_bootstrap = _get_cached_session(context)
        if needs_bootstrap:
            _record_bootstrap_result(
                context,
                session,
                succeeded=_bootstrap_session(session, context),
            )
        try:
            mblogs = _fetch_weibo(session)
        except Exception:
            _mark_session_for_retry(context, session)
            raise
    return _select_earthquakes(mblogs, since_id=since_id, force=force)


# ---------------------------------------------------------------------------
# 图片校验与有界缓存
# ---------------------------------------------------------------------------


def _image_media_type(headers: object) -> str:
    raw_value = headers.get("Content-Type") if isinstance(headers, Mapping) else None
    if not isinstance(raw_value, str) or not raw_value.strip() or "," in raw_value:
        raise ResponseFormatError("earthquake image has no valid Content-Type")
    media_type = raw_value.split(";", 1)[0].strip().casefold()
    if media_type not in _IMAGE_MIME_FORMATS:
        raise ResponseFormatError("earthquake image MIME type is not allowed")
    return media_type


def _validate_image_bytes(payload: bytes, *, media_type: str) -> str:
    """通过 core 完整解码单帧图片，并映射为 HTTP 响应错误类别。"""

    format_spec = _IMAGE_MIME_FORMATS.get(media_type)
    if format_spec is None:
        raise ResponseFormatError("earthquake image MIME type is not allowed")
    expected_format, extension = format_spec
    try:
        validate_image_bytes(
            payload,
            limits=ImageValidationLimits(
                max_bytes         = MAX_IMAGE_BYTES,
                max_pixels        = MAX_IMAGE_PIXELS,
                max_frames        = 1,
                max_dimension     = MAX_IMAGE_DIMENSION,
                max_decoded_bytes = MAX_DECODED_IMAGE_BYTES,
            ),
            format_extensions = _IMAGE_FORMAT_EXTENSIONS,
            expected_format   = expected_format,
            allowed_modes     = _IMAGE_FORMAT_MODES,
            allow_animation   = False,
        )
    except ImageValidationError as exc:
        if exc.reason in {
            "bytes_limit",
            "decoded_bytes_limit",
            "decompression_bomb",
            "dimension_limit",
            "frame_limit",
            "pixel_limit",
        }:
            raise ResponseLimitError(f"earthquake {exc}") from exc
        if exc.reason == "animation_not_allowed":
            raise ResponseFormatError("animated earthquake images are not allowed") from exc
        if exc.reason == "format_mismatch":
            raise ResponseFormatError("earthquake image MIME and format do not match") from exc
        if exc.reason == "mode_not_allowed":
            raise ResponseFormatError("earthquake image mode is not allowed") from exc
        if exc.reason == "invalid_container":
            raise ResponseFormatError(
                "earthquake image has trailing or invalid container data"
            ) from exc
        if exc.reason == "empty_image":
            raise ResponseFormatError("earthquake image body is empty") from exc
        raise ResponseFormatError("earthquake image is invalid") from exc
    return extension


def _validate_and_store_figure(
    context: _EarthquakeContext,
    response: SafeHttpResponse,
) -> Path:
    media_type = _image_media_type(response.headers)
    extension = _validate_image_bytes(response.body, media_type=media_type)
    digest = hashlib.sha256(response.body).hexdigest()
    cache  = BoundedFileCache(context.data_dir / "EarthquakeFigures", _IMAGE_CACHE_LIMITS)
    file_path, _created = cache.put_if_absent(f"{digest}{extension}", response.body)
    if file_path is None:
        raise OSError("earthquake image cache rejected the validated payload")
    return cast(Path, file_path)


async def _download_figure(context: _EarthquakeContext, figure_url: str) -> Path:
    response = await fetch_public_bytes(
        figure_url,
        headers                       = _IMAGE_HEADERS,
        timeout_seconds               = 20.0,
        max_bytes                     = MAX_IMAGE_BYTES,
        allowed_content_type_prefixes = (),
        allowed_content_types         = tuple(_IMAGE_MIME_FORMATS),
        allowed_hosts                 = _IMAGE_HOSTS,
        allowed_schemes               = ("https",),
    )
    if response is None:
        raise SafeHttpError("earthquake image request failed")
    return cast(Path, await run_sync(_validate_and_store_figure, context, response))


async def _render_cards(
    context: _EarthquakeContext,
    cards: Sequence[_PreparedCard],
) -> Segments:
    async def render_figure(prepared: _PreparedCard) -> Path | None:
        if not prepared.figure_url:
            return None
        try:
            return await _download_figure(context, prepared.figure_url)
        except Exception as exc:
            public_error_message(
                context,
                exc,
                logger    = logger,
                component = "earthquake.download_image",
            )
            return None

    figure_paths = await gather_bounded(
        (render_figure(prepared) for prepared in cards),
        limit=3,
    )
    output: Segments = []
    for prepared, file_path in zip(cards, figure_paths, strict=True):
        output.append(text(prepared.clean_text))
        if file_path is not None:
            output.append(image(str(file_path)))
        logger.info(
            "Earthquake notification prepared: magnitude=%s text_chars=%d",
            f"{prepared.magnitude:.1f}" if prepared.magnitude is not None else "unknown",
            len(prepared.clean_text),
        )
    return output


# ---------------------------------------------------------------------------
# 命令、扫描游标与可靠群发
# ---------------------------------------------------------------------------


async def handle(
    command: str,
    args: str,
    event: dict[str, Any],
    context: _EarthquakeContext,
) -> Segments:
    """处理手动查询；任何手动请求都不会改动定时游标。"""

    del command, event
    try:
        action = _parse_action(args)
        if action == "help":
            return segments(HELP_TEXT)
        return await _fetch_earthquake_news(context, force=True)
    except EarthquakeCommandError as exc:
        return segments(str(exc))
    except Exception as exc:
        return public_error_response(context, exc, logger=logger, component="earthquake.handle")


async def _record_scan_progress(
    context: _EarthquakeContext,
    *,
    batch: _FetchBatch,
    previous_since_id: str,
    advance_cursor: bool,
) -> None:
    if batch.newest_seen_id == previous_since_id:
        return
    if advance_cursor or not batch.cards:
        await asyncio.to_thread(_save_since, context, batch.newest_seen_id)
        return
    context.state[_PENDING_CURSOR_KEY]    = batch.newest_seen_id
    context.state[_PENDING_EVENT_IDS_KEY] = list(batch.event_ids)


async def _fetch_earthquake_news(
    context: _EarthquakeContext,
    force: bool = False,
    *,
    advance_cursor: bool = True,
) -> Segments:
    """获取并渲染快讯；定时模式在投递成功前只生成候选游标。"""

    try:
        # 手动查询既不读取也不初始化定时游标，损坏状态不会阻断只读查询。
        since_id = "0" if force else await asyncio.to_thread(_load_since, context)
        batch = await run_sync(_fetch_batch, context, since_id=since_id, force=force)
    except Exception as exc:
        if not force:
            public_error_message(
                context,
                exc,
                logger    = logger,
                component = "earthquake.fetch_scheduled",
            )
            return []
        return public_error_response(context, exc, logger=logger, component="earthquake.fetch")

    if not force:
        await _record_scan_progress(
            context,
            batch             = batch,
            previous_since_id = since_id,
            advance_cursor    = advance_cursor,
        )
    if not batch.cards:
        return segments("未获取到地震快讯数据") if force else []
    return await _render_cards(context, batch.cards)


def _normalize_notification_event_ids(event_ids: object) -> tuple[str, ...]:
    if not isinstance(event_ids, (list, tuple)):
        return ()
    normalized: list[str] = []
    for value in event_ids:
        event = _normalize_event_id(value)
        if event is None:
            raise ValueError("earthquake notification contains an invalid event id")
        normalized.append(event[0])
    return tuple(sorted(set(normalized), key=int))


def _notification_id(since_id: str, event_ids: object) -> str:
    """根据规范化游标和事件集合生成稳定、无外部原文的幂等键。"""

    normalized_since = _validate_since_payload({"since_id": since_id})
    material         = json.dumps(
        {
            "since_id": normalized_since,
            "event_ids": _normalize_notification_event_ids(event_ids),
        },
        sort_keys  = True,
        separators = (",", ":"),
    ).encode("utf-8")
    return f"earthquake:{hashlib.sha256(material).hexdigest()}"


async def _deliver_pending(context: _EarthquakeContext, pending: PendingFanout) -> bool:
    """逐目标确认投递；所有目标完成后才提交游标并清空 outbox。"""

    path = _fanout_path(context)
    for target in pending.pending_targets():
        user_id  = target.target_id if target.kind == "private" else None
        group_id = target.target_id if target.kind == "group" else None
        action   = build_action(pending.payload, user_id, group_id)
        if action is None:
            logger.error("Earthquake fanout could not build target action")
            continue

        confirm_target = partial(asyncio.to_thread, mark_delivered, path, pending, target)

        receipt = DeliveryReceipt(
            expected_actions = 1,
            commit           = confirm_target,
            rollback         = lambda: None,
            # 地震告警采用 at-most-once；回执丢失时不重复推送可能已送达的告警。
            unknown=confirm_target,
        )
        try:
            await send_with_receipt(context.send_action, action, receipt)
        except Exception as exc:
            public_error_message(context, exc, logger=logger, component="earthquake.delivery")
            await receipt.record(False)
        if receipt.callback_error is not None:
            public_error_message(
                context,
                receipt.callback_error,
                logger    = logger,
                component = "earthquake.delivery_ack",
            )
            return False
        if not receipt.resolved or receipt.outcome is False:
            logger.warning("Earthquake target delivery failed; retained for retry")
        elif receipt.outcome is None:
            logger.warning(
                "Earthquake target outcome unknown; acknowledged under at-most-once policy"
            )

    if not pending.complete:
        return False
    since_id = _validate_since_payload({"since_id": pending.commit.get("since_id")})
    await asyncio.to_thread(_save_since, context, since_id)
    await asyncio.to_thread(clear_pending, path)
    return True


async def scheduled(context: _EarthquakeContext) -> Segments:
    """每次只允许一个轮询/重试事务进入持久投递区。"""

    async with _DELIVERY_LOCK:
        try:
            return await _scheduled_locked(context)
        except Exception as exc:
            public_error_message(context, exc, logger=logger, component="earthquake.scheduled")
            return []


async def shutdown(context: _EarthquakeContext | None = None) -> None:
    """关闭当前插件代缓存的同步微博会话。"""

    if context is not None:
        await run_sync(_close_cached_session, context)


async def _scheduled_locked(context: _EarthquakeContext) -> Segments:
    try:
        pending = await asyncio.to_thread(load_pending, _fanout_path(context))
    except Exception as exc:
        public_error_message(
            context,
            exc,
            logger    = logger,
            component = "earthquake.load_delivery_state",
        )
        return []
    if pending is not None:
        try:
            await _deliver_pending(context, pending)
        except Exception as exc:
            public_error_message(
                context,
                exc,
                logger    = logger,
                component = "earthquake.delivery_state",
            )
        return []

    # 丢弃上一次未形成 outbox 的内存候选；新的扫描会重新生成它。
    context.state.pop(_PENDING_CURSOR_KEY, None)
    context.state.pop(_PENDING_EVENT_IDS_KEY, None)
    result = await _fetch_earthquake_news(context, force=False, advance_cursor=False)
    pending_since     = context.state.pop(_PENDING_CURSOR_KEY, None)
    pending_event_ids = context.state.pop(_PENDING_EVENT_IDS_KEY, ())
    if not result:
        if isinstance(pending_since, str):
            await asyncio.to_thread(_save_since, context, pending_since)
        return []

    targets = default_group_targets(context)
    if not targets:
        logger.warning("Earthquake notification has no configured target group; cursor retained")
        return []
    if not isinstance(pending_since, str):
        logger.error("Earthquake notification has no durable cursor candidate")
        return []
    try:
        pending = await asyncio.to_thread(
            create_pending,
            _fanout_path(context),
            event_id = _notification_id(pending_since, pending_event_ids),
            payload  = result,
            targets  = targets,
            commit   = {"since_id": pending_since},
        )
        await _deliver_pending(context, pending)
    except Exception as exc:
        public_error_message(
            context,
            exc,
            logger    = logger,
            component = "earthquake.delivery_state",
        )
    return []
