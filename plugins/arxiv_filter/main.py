"""arXiv 论文筛选插件：统一调度 Transformer、k-NN 与多兴趣模型。"""

import asyncio
import datetime
import hashlib
import importlib
import json
import logging
import os
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from core.args import parse
from core.delivery import DeliveryReceipt, DeliverySegments
from core.plugin_base import (
    PluginContextProtocol,
    Segments,
    atomic_write_text,
    bounded_external_text,
    run_sync,
    segments,
)
from core.public_errors import public_error_message, public_error_response

from .codex_summary import schedule_codex_summary_from_filter_result
from .utils import load_plugin_config

logger = logging.getLogger(__name__)


# ============================================================
# 模块加载
# ============================================================

InferenceFunction = Callable[..., str]

_inference_func: InferenceFunction | None = None
_FILTER_CACHE: dict[tuple[str, str, str, InferenceFunction], str] = {}
_FILTER_INFLIGHT: dict[
    tuple[str, str, str, InferenceFunction],
    asyncio.Task[str],
] = {}
_FILTER_LOCK = asyncio.Lock()
_STATUS_LOCK = threading.Lock()
_MODEL_FINGERPRINT_REFRESH_SECONDS = 60.0
_MODEL_FINGERPRINT_CACHE: dict[str, tuple[float, str]] = {}
_MODEL_FINGERPRINT_LOCK = asyncio.Lock()


class FilterResult(list[dict[str, Any]]):
    """携带显式筛选结果的消息段，供定时投递决定提交还是重试。"""

    def __init__(
        self,
        values: Segments,
        *,
        succeeded: bool,
        outcome: str,
    ) -> None:
        if type(succeeded) is not bool:
            raise TypeError("succeeded must be a boolean")
        if not isinstance(outcome, str) or not outcome:
            raise ValueError("outcome must be a non-empty string")
        super().__init__(values)
        self.succeeded = succeeded
        self.outcome = outcome


def _filter_result(payload: Any, *, succeeded: bool, outcome: str) -> FilterResult:
    return FilterResult(list(segments(payload)), succeeded=succeeded, outcome=outcome)


def _configuration_fingerprint(config: Mapping[str, Any]) -> str:
    serialized = json.dumps(config, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _business_now(context: PluginContextProtocol | None = None) -> datetime.datetime:
    timezone_name = "Asia/Shanghai"
    if context is not None:
        configured = context.get_settings_snapshot().config.get("timezone")
        if isinstance(configured, str):
            timezone_name = configured
    try:
        zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        zone = ZoneInfo("Asia/Shanghai")
    return datetime.datetime.now(zone)


async def _cached_model_artifact_fingerprint(model_path: str) -> str:
    """返回按绝对模型路径受控刷新的目录指纹。

    模型目录通常在一次插件生命周期内保持不变。热路径在 60 秒窗口内完全复用
    已计算指纹，不再递归 rglob/stat/hash；窗口到期后重新计算一次，因此在线替换
    模型仍能最终使当日结果缓存和后端对象缓存失效。插件初始化或强制重载会立即清空。
    """

    from .inference.shared import model_artifact_fingerprint, resolve_model_path

    resolved_path = str(Path(resolve_model_path(str(model_path))).resolve())
    now = time.monotonic()
    cached = _MODEL_FINGERPRINT_CACHE.get(resolved_path)
    if cached is not None and cached[0] > now:
        return cached[1]

    async with _MODEL_FINGERPRINT_LOCK:
        now = time.monotonic()
        cached = _MODEL_FINGERPRINT_CACHE.get(resolved_path)
        if cached is not None and cached[0] > now:
            return cached[1]
        fingerprint = await run_sync(model_artifact_fingerprint, resolved_path)
        if not isinstance(fingerprint, str):
            raise TypeError("model artifact fingerprint must be text")
        expires_at = time.monotonic() + _MODEL_FINGERPRINT_REFRESH_SECONDS
        _MODEL_FINGERPRINT_CACHE[resolved_path] = (expires_at, fingerprint)
        for stale_path, (stale_expiry, _fingerprint) in list(_MODEL_FINGERPRINT_CACHE.items()):
            if stale_path != resolved_path and stale_expiry <= now:
                _MODEL_FINGERPRINT_CACHE.pop(stale_path, None)
        return fingerprint


async def _cached_inference(
    inference: InferenceFunction,
    model_path: str,
    config_fingerprint: str,
    source_date: str | None,
) -> str:
    artifact_fingerprint = await _cached_model_artifact_fingerprint(model_path)
    if source_date is None:
        result = await run_sync(
            lambda: inference(
                model_path=model_path,
                artifact_fingerprint=artifact_fingerprint,
            )
        )
        if not isinstance(result, str):
            raise TypeError("arXiv inference must return text")
        return result

    key = (source_date, artifact_fingerprint, config_fingerprint, inference)
    # arXiv 源列表日期是缓存语义的一部分。这样当天更新前读到的昨日列表不会污染
    # 更新后的同一业务日结果，同时也避免常驻进程无限保留旧列表。
    for stale_key in [candidate for candidate in _FILTER_CACHE if candidate[0] != source_date]:
        _FILTER_CACHE.pop(stale_key, None)
    cached = _FILTER_CACHE.get(key)
    if cached is not None:
        return cached
    async with _FILTER_LOCK:
        task = _FILTER_INFLIGHT.get(key)
        if task is None:
            task = asyncio.create_task(
                run_sync(
                    lambda: inference(
                        model_path=model_path,
                        artifact_fingerprint=artifact_fingerprint,
                    )
                )
            )
            _FILTER_INFLIGHT[key] = task
    try:
        result = await asyncio.shield(task)
        if not isinstance(result, str):
            raise TypeError("arXiv inference must return text")
        if not result.startswith("Error:"):
            _FILTER_CACHE[key] = result
        return result
    finally:
        if task.done():
            async with _FILTER_LOCK:
                if _FILTER_INFLIGHT.get(key) is task:
                    _FILTER_INFLIGHT.pop(key, None)


def _normalize_source_date(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    try:
        parsed = datetime.date.fromisoformat(cleaned)
    except ValueError:
        return None
    return cleaned if parsed.isoformat() == cleaned else None


async def _latest_arxiv_source_date(context: PluginContextProtocol) -> str | None:
    """读取 arXiv 当前列表的真实发布日期；失败时禁止用本地日期冒充。"""

    try:
        raw_date = await run_sync(
            lambda: importlib.import_module(f"{__package__}.arxiv_today").check_arxiv_update_date()
        )
    except Exception as exc:
        public_error_message(
            context,
            exc,
            logger=logger,
            component="arxiv_filter.source_date",
        )
        return None
    source_date = _normalize_source_date(raw_date)
    if raw_date is not None and source_date is None:
        logger.warning("忽略无效的 arXiv 源列表日期")
    return source_date


def _load_inference(
    context: PluginContextProtocol | None = None,
) -> InferenceFunction | None:
    """
    动态加载推理模块

    Returns:
        推理函数或 None
    """
    global _inference_func

    if _inference_func is not None:
        logger.debug("使用已缓存的推理函数")
        return _inference_func

    logger.info("开始加载推理模块")

    try:
        from .arxiv_inference import get_positive_arxiv_today_as_string

        _inference_func = get_positive_arxiv_today_as_string
        logger.info("成功加载 arxiv_inference 模块")
        return _inference_func
    except ImportError as exc:
        if context is not None:
            public_error_message(
                context,
                exc,
                logger=logger,
                component="arxiv_filter.load_inference",
            )
        else:
            logger.error("导入 arxiv_inference 模块失败: error_type=ImportError")
        logger.error("请确保安装了所需依赖: torch, transformers")
        return None
    except Exception as exc:
        if context is not None:
            public_error_message(
                context,
                exc,
                logger=logger,
                component="arxiv_filter.load_inference",
            )
        else:
            logger.error(
                "加载推理模块时发生异常: error_type=%s",
                type(exc).__name__,
            )
        return None


def init(context: PluginContextProtocol | None = None) -> None:
    """
    插件初始化

    清除模块缓存，下次调用时重新加载推理模块。
    这确保插件更新后能立即生效。

    Args:
        context: 插件上下文（可选）
    """
    # 保留 context 参数是插件生命周期契约的一部分；初始化只清理进程内缓存。
    del context
    global _inference_func
    _inference_func = None
    _FILTER_CACHE.clear()
    _MODEL_FINGERPRINT_CACHE.clear()
    logger.info("arXiv Filter 插件已初始化")


# ============================================================
# 主处理函数
# ============================================================


async def handle(
    command: str,
    args: str,
    event: dict[str, Any],
    context: PluginContextProtocol,
) -> Segments:
    """命令处理入口"""
    del command
    try:
        parsed = parse(args)

        # 解析子命令
        if parsed and parsed.first:
            if len(parsed) == 1 and parsed.first.lower() in {"help", "帮助"}:
                return segments(_show_help())
            unknown = bounded_external_text(
                parsed.first,
                max_chars=32,
                max_bytes=128,
                suffix="…",
            )
            return segments(f"未知命令: {unknown}\n输入 /arxiv help 查看帮助")

        source_date = await _latest_arxiv_source_date(context)
        return await _run_filter(
            context,
            allow_codex_sidecar=_is_admin_user(context, event.get("user_id")),
            source_date=source_date,
        )

    except Exception as exc:
        return public_error_response(context, exc, logger=logger, component="arxiv_filter.handle")


async def shutdown(context: PluginContextProtocol) -> None:
    tasks = list(_FILTER_INFLIGHT.values())
    state = getattr(context, "state", None)
    if isinstance(state, dict):
        background_tasks = state.get("arxiv_background_tasks", set())
        tasks.extend(background_tasks)
        background_tasks.clear()
    for task in set(tasks):
        task.cancel()
    if tasks:
        await asyncio.gather(*set(tasks), return_exceptions=True)
    _MODEL_FINGERPRINT_CACHE.clear()


def _is_admin_user(context: PluginContextProtocol, user_id: object) -> bool:
    is_global_admin = getattr(context, "is_global_admin", None)
    if callable(is_global_admin):
        return bool(is_global_admin(user_id))
    principal = getattr(context, "principal", None)
    capabilities = getattr(context, "capabilities", None)
    if principal is None or not getattr(capabilities, "is_bot_admin", False):
        return False
    if type(principal.user_id) is not int or type(user_id) is not int:
        return False
    return principal.user_id == user_id


def _scheduled_without_delivery_targets(context: PluginContextProtocol) -> bool:
    principal = getattr(context, "principal", None)
    return bool(
        principal is not None
        and getattr(principal, "kind", None) == "scheduled_system"
        and not tuple(getattr(principal, "delivery_targets", ()))
    )


async def scheduled_check(context: PluginContextProtocol) -> Segments:
    """定时检查 arXiv 是否更新"""
    return await _check_arxiv_update(context, is_final_check=False)


async def scheduled_final_check(context: PluginContextProtocol) -> Segments:
    """最后一次检查（12点），如果仍未更新则发送停更通知"""
    return await _check_arxiv_update(context, is_final_check=True)


async def _run_filter(
    context: PluginContextProtocol,
    *,
    allow_codex_sidecar: bool = False,
    source_date: str | None = None,
) -> FilterResult:
    """
    执行论文筛选

    Args:
        context: 插件上下文

    Returns:
        消息段列表
    """
    if source_date is not None:
        normalized_source_date = _normalize_source_date(source_date)
        if normalized_source_date is None:
            raise ValueError("source_date must be an ISO calendar date")
        source_date = normalized_source_date

    # 加载配置
    config = load_plugin_config()
    model_config = config.get("model", {})
    if not isinstance(model_config, Mapping):
        raise ValueError("arxiv_filter model config must be a JSON object")
    environment_model_path = os.environ.get("ARXIV_MODEL_PATH", "").strip()
    configured_model_path = environment_model_path or model_config.get("path", "best_model")
    if not isinstance(configured_model_path, str) or not configured_model_path.strip():
        raise ValueError("arxiv_filter model.path must be a non-empty string")
    configured_model_path = configured_model_path.strip()

    # 加载推理函数
    inference = _load_inference(context=context)
    if inference is None:
        error_msg = (
            "⚠️ 无法加载 AI 模型或依赖，请检查模型配置；"
            '缺少依赖时请运行 pip install "xiaoqing[arxiv-ml]"。'
        )
        logger.error("加载推理模块失败")
        return _filter_result(error_msg, succeeded=False, outcome="model_unavailable")

    try:
        logger.info(
            "开始执行 arXiv 论文筛选，model_path_configured=%s",
            bool(configured_model_path),
        )
        start_time = time.perf_counter()
        arxiv_text = await _cached_inference(
            inference,
            configured_model_path,
            _configuration_fingerprint(config),
            source_date,
        )
        elapsed = time.perf_counter() - start_time
        logger.info(
            "arXiv 论文筛选完成，耗时 %.2f 秒，返回内容长度: %d 字符",
            elapsed,
            len(arxiv_text),
        )

        # 检查是否有错误消息
        if arxiv_text.startswith("Error:"):
            return _filter_result(
                public_error_response(
                    context,
                    RuntimeError("arXiv inference returned an error result"),
                    logger=logger,
                    component="arxiv_filter.inference_result",
                ),
                succeeded=False,
                outcome="inference_error",
            )

        # 检查是否没有结果
        if "No positive predictions" in arxiv_text:
            logger.info("今日没有符合条件的论文")
            today = _business_now(context).date()
            if source_date is None:
                list_description = "arXiv 当前最新列表（日期未能确认）"
            elif source_date == today.isoformat():
                list_description = f"今天是 {today}"
            else:
                list_description = f"今天是 {today}，arXiv 当前最新列表日期为 {source_date}"
            return _filter_result(
                f"📚 {list_description}，暂时没有发现感兴趣的论文。",
                succeeded=True,
                outcome="no_positive_predictions",
            )

        if "No papers found" in arxiv_text:
            return _filter_result(
                public_error_response(
                    context,
                    RuntimeError("arXiv inference did not produce a usable paper list"),
                    logger=logger,
                    component="arxiv_filter.inference_result",
                ),
                succeeded=False,
                outcome="inference_error",
            )

        if "----- Positive #" not in arxiv_text:
            return _filter_result(
                public_error_response(
                    context,
                    RuntimeError("arXiv inference returned an unknown result format"),
                    logger=logger,
                    component="arxiv_filter.inference_result",
                ),
                succeeded=False,
                outcome="unknown_result",
            )

        logger.debug("筛选结果预览: %s...", arxiv_text[:200])

    except FileNotFoundError as exc:
        return _filter_result(
            public_error_response(
                context,
                exc,
                logger=logger,
                component="arxiv_filter.model_file",
            ),
            succeeded=False,
            outcome="model_file_error",
        )
    except ImportError as exc:
        return _filter_result(
            public_error_response(
                context,
                exc,
                logger=logger,
                component="arxiv_filter.dependencies",
            ),
            succeeded=False,
            outcome="dependency_error",
        )
    except Exception as exc:
        return _filter_result(
            public_error_response(
                context,
                exc,
                logger=logger,
                component="arxiv_filter.run",
            ),
            succeeded=False,
            outcome="runtime_error",
        )

    # 格式化输出
    today = _business_now(context).date()
    if source_date is None:
        list_description = "arXiv 当前最新列表（日期未能确认）"
    elif source_date == today.isoformat():
        list_description = f"今天是 {today}"
    else:
        list_description = f"今天是 {today}，arXiv 当前最新列表日期为 {source_date}"
    header = f"📚 {list_description}，以下是你可能感兴趣的论文：\n"
    if allow_codex_sidecar:
        if source_date is None:
            logger.warning("skip Codex arXiv summary: source list date could not be confirmed")
        else:
            try:
                schedule_codex_summary_from_filter_result(
                    context,
                    date=source_date,
                    filter_text=arxiv_text,
                )
            except Exception as exc:
                public_error_message(
                    context,
                    exc,
                    logger=logger,
                    component="arxiv_filter.codex_sidecar",
                )
    return _filter_result(header + arxiv_text, succeeded=True, outcome="papers")


def _show_help() -> str:
    """显示帮助信息"""
    return """
📚 **arXiv 论文筛选**

基于 AI 模型自动筛选今日感兴趣的 arXiv 论文

**使用方法:**
• /arxiv - 获取今日筛选的论文
• /arxiv help - 显示帮助信息

**功能特点:**
- 自动识别 Transformer、k-NN 或多兴趣模型
- 自动获取最新论文
- 根据研究兴趣推荐

输入 /arxiv 查看今日推荐论文
""".strip()


# ============================================================
# 状态管理
# ============================================================


def _get_status_file_path(data_dir: str | Path) -> Path:
    """Return the status path inside the core-assigned persistent data directory."""
    path = Path(data_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path / "update_status.json"


class _StatusFileError(RuntimeError):
    """持久投递状态不可可信读取，调用方必须停止自动广播。"""


def _validate_update_status(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise _StatusFileError("arXiv 状态文件根节点必须是 JSON 对象")

    last_sent_date = payload.get("last_sent_date")
    if last_sent_date is not None:
        if not isinstance(last_sent_date, str):
            raise _StatusFileError("arXiv 状态中的 last_sent_date 必须是字符串")
        try:
            datetime.date.fromisoformat(last_sent_date)
        except ValueError as exc:
            raise _StatusFileError("arXiv 状态中的 last_sent_date 不是 ISO 日期") from exc

    last_sent_time = payload.get("last_sent_time")
    if last_sent_time is not None:
        if not isinstance(last_sent_time, str):
            raise _StatusFileError("arXiv 状态中的 last_sent_time 必须是字符串")
        try:
            datetime.datetime.fromisoformat(last_sent_time)
        except ValueError as exc:
            raise _StatusFileError("arXiv 状态中的 last_sent_time 不是 ISO 时间") from exc
    return payload


def _load_update_status(data_dir: str | Path) -> dict[str, object]:
    """加载并校验投递状态；损坏状态不能被误判成“尚未发送”。"""

    status_file = _get_status_file_path(data_dir)
    if not status_file.exists():
        return {}
    try:
        with status_file.open(encoding="utf-8") as stream:
            payload = json.load(stream)
        return _validate_update_status(payload)
    except _StatusFileError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _StatusFileError(f"无法读取 arXiv 投递状态: {type(exc).__name__}") from exc


def _save_update_status(data_dir: str | Path, status: Mapping[str, object]) -> None:
    """保存今日更新状态"""
    status_file = _get_status_file_path(data_dir)
    try:
        atomic_write_text(status_file, json.dumps(status, ensure_ascii=False, indent=2))
    except Exception as exc:
        logger.error(
            "保存状态文件失败: error_type=%s",
            type(exc).__name__,
        )
        raise


def _should_send_today(data_dir: str | Path, business_date: str) -> bool:
    """按调用方已经解析好的配置时区日期检查发送状态。"""
    status = _load_update_status(data_dir)
    return status.get("last_sent_date") != business_date


def _claim_path(data_dir: str | Path, business_date: str) -> Path:
    return _get_status_file_path(data_dir).with_name(f"claim-{business_date}")


def _claim_send_today(data_dir: str | Path, business_date: str) -> bool:
    with _STATUS_LOCK:
        if not _should_send_today(data_dir, business_date):
            return False
        path = _claim_path(data_dir, business_date)
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(path) <= 3600:
                    return False
                os.unlink(path)
                descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except (FileNotFoundError, FileExistsError, OSError):
                return False
        os.close(descriptor)
        return True


def _release_claim(data_dir: str | Path, business_date: str) -> None:
    try:
        _claim_path(data_dir, business_date).unlink()
    except FileNotFoundError:
        pass


def _mark_sent_today(data_dir: str | Path, business_date: str) -> None:
    """标记调用方提供的配置时区业务日期已发送。"""
    status = {
        "last_sent_date": business_date,
        "last_sent_time": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    _save_update_status(data_dir, status)
    _release_claim(data_dir, business_date)


def _track_delivery(
    result: FilterResult,
    data_dir: str | Path,
    business_date: str,
) -> DeliverySegments:
    async def commit() -> None:
        try:
            await run_sync(_mark_sent_today, data_dir, business_date)
        except Exception:
            await run_sync(_release_claim, data_dir, business_date)
            raise

    async def rollback() -> None:
        await run_sync(_release_claim, data_dir, business_date)

    receipt = DeliveryReceipt(
        expected_actions=1,
        commit=commit,
        rollback=rollback,
        # 传输已提交但回执未知时采用 at-most-once：避免同一日报重复广播。
        unknown=commit,
    )
    return DeliverySegments(result, receipt)


# ============================================================
# arXiv 更新检查
# ============================================================


async def _check_arxiv_update(
    context: PluginContextProtocol,
    is_final_check: bool = False,
) -> Segments:
    """
    检查 arXiv 是否更新

    Args:
        context: 插件上下文
        is_final_check: 是否是最后一次检查（12点）

    Returns:
        消息段列表
    """
    if _scheduled_without_delivery_targets(context):
        logger.info("skip scheduled arXiv update check: no explicit delivery targets")
        return []

    data_dir = Path(context.data_dir)

    today = _business_now(context).date().isoformat()

    # 通过原子 claim 保证同一业务日期只有一个检查者进入发送路径。
    try:
        claimed = await run_sync(_claim_send_today, data_dir, today)
    except Exception as exc:
        # 状态不可信时采用 fail-closed：宁可漏掉一次自动播报，也不能重复群发。
        public_error_message(
            context,
            exc,
            logger=logger,
            component="arxiv_filter.claim_delivery",
        )
        return []
    if not claimed:
        logger.info("今天已经发送过 arXiv 更新，跳过此次检查")
        return []

    try:
        # 连同可选抓取依赖一起放到工作线程；测试或禁用路径无需提前导入。
        arxiv_date = await run_sync(
            lambda: importlib.import_module(f"{__package__}.arxiv_today").check_arxiv_update_date()
        )
    except Exception as exc:
        public_error_message(
            context,
            exc,
            logger=logger,
            component="arxiv_filter.check_date",
        )
        await run_sync(_release_claim, data_dir, today)
        return []

    # 如果 arXiv 已更新到今天
    if arxiv_date == today:
        logger.info("检测到 arXiv 已更新到 %s，开始筛选论文...", today)
        try:
            result = await _run_filter(
                context,
                allow_codex_sidecar=True,
                source_date=arxiv_date,
            )
        except asyncio.CancelledError:
            await run_sync(_release_claim, data_dir, today)
            raise
        except Exception as exc:
            await run_sync(_release_claim, data_dir, today)
            return _filter_result(
                public_error_response(
                    context,
                    exc,
                    logger=logger,
                    component="arxiv_filter.scheduled_filter",
                ),
                succeeded=False,
                outcome="runtime_error",
            )
        if isinstance(result, FilterResult) and result.succeeded:
            return _track_delivery(result, data_dir, today)
        await run_sync(_release_claim, data_dir, today)
        logger.warning("arXiv filter failed after update detection; keep retrying later today")
        return result

    # 如果是最后一次检查且仍未更新
    if is_final_check:
        logger.info("最后检查时间已到，arXiv 仍未更新（当前日期: %s），发送停更通知", arxiv_date)
        result = _filter_result(
            f"📚 arXiv 今日（{today}）暂未更新，可能稍后更新或今日停更。",
            succeeded=True,
            outcome="no_update",
        )
        return _track_delivery(result, data_dir, today)

    # 还不是最后检查，继续等待
    logger.info("arXiv 尚未更新到今天（当前: %s，期望: %s），等待下次检查", arxiv_date, today)
    await run_sync(_release_claim, data_dir, today)
    return []
