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

from core.clock import now_in_configured_timezone
from core.delivery import DeliveryReceipt, DeliverySegments
from core.lifecycle import DeferredCancellation, await_owned_task
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

_inference_func : InferenceFunction | None                           = None
_FILTER_CACHE   : dict[tuple[str, str, str, InferenceFunction], str] = {}
_FILTER_INFLIGHT: dict[
    tuple[str, str, str, InferenceFunction],
    asyncio.Task[str],
] = {}
_FILTER_LOCK                                           = asyncio.Lock()
_STATUS_LOCK                                           = threading.Lock()
_MODEL_FINGERPRINT_REFRESH_SECONDS                     = 60.0
_MODEL_FINGERPRINT_CACHE: dict[str, tuple[float, str]] = {}
_MODEL_FINGERPRINT_LOCK                                = asyncio.Lock()


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
        self.outcome   = outcome


def _filter_result(payload: Any, *, succeeded: bool, outcome: str) -> FilterResult:
    return FilterResult(list(segments(payload)), succeeded=succeeded, outcome=outcome)


def _failure(
    context: PluginContextProtocol, exc: BaseException, component: str, outcome: str
) -> FilterResult:
    """失败结果：记录日志并只向用户展示公开错误文本。"""

    return _filter_result(
        public_error_response(context, exc, logger=logger, component=component),
        succeeded = False,
        outcome   = outcome,
    )


def _list_description(context: PluginContextProtocol, source_date: str | None) -> str:
    """说明今天的日期以及 arXiv 列表实际对应的日期。"""

    today = _business_now(context).date()
    if source_date is None:
        return "arXiv 当前最新列表（日期未能确认）"
    if source_date == today.isoformat():
        return f"今天是 {today}"
    return f"今天是 {today}，arXiv 当前最新列表日期为 {source_date}"


def _configuration_fingerprint(config: Mapping[str, Any]) -> str:
    serialized = json.dumps(config, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _business_now(context: PluginContextProtocol | None = None) -> datetime.datetime:
    """按全局时区配置的当前时间；没有上下文时使用默认时区。"""

    return now_in_configured_timezone(context)


async def _cached_model_artifact_fingerprint(model_path: str) -> str:
    """返回按绝对模型路径受控刷新的目录指纹。

    模型目录通常在一次插件生命周期内保持不变。热路径在 60 秒窗口内完全复用
    已计算指纹，不再递归 rglob/stat/hash；窗口到期后重新计算一次，因此在线替换
    模型仍能最终使当日结果缓存和后端对象缓存失效。插件初始化或强制重载会立即清空。
    """

    from .inference.shared import resolve_model_path, runtime_artifact_fingerprint

    resolved_path = str(Path(resolve_model_path(str(model_path))).resolve())
    now           = time.monotonic()
    cached        = _MODEL_FINGERPRINT_CACHE.get(resolved_path)
    if cached is not None and cached[0] > now:
        return cached[1]

    async with _MODEL_FINGERPRINT_LOCK:
        now    = time.monotonic()
        cached = _MODEL_FINGERPRINT_CACHE.get(resolved_path)
        if cached is not None and cached[0] > now:
            return cached[1]
        fingerprint = await run_sync(runtime_artifact_fingerprint, resolved_path)
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
    """同一源列表日期、模型和配置只推理一次；并发请求共享同一个推理任务。"""

    artifact_fingerprint = await _cached_model_artifact_fingerprint(model_path)

    def run_inference() -> str:
        return inference(model_path=model_path, artifact_fingerprint=artifact_fingerprint)

    if source_date is None:
        result = await run_sync(run_inference)
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
            task                  = asyncio.create_task(run_sync(run_inference))
            _FILTER_INFLIGHT[key] = task
    try:
        result = await asyncio.shield(task)
        if not isinstance(result, str):
            raise TypeError("arXiv inference must return text")
        # 只缓存明确成功的结果；抓取失败的文本进了当日缓存会挡住网络恢复后的重试。
        if _is_successful_inference(result):
            _FILTER_CACHE[key] = result
        return result
    finally:
        if task.done():
            async with _FILTER_LOCK:
                if _FILTER_INFLIGHT.get(key) is task:
                    _FILTER_INFLIGHT.pop(key, None)


def _is_successful_inference(text: str) -> bool:
    """推理结果的成功契约：给出了正例列表，或明确确认没有正例。"""

    if text.startswith("Error:") or "No papers found" in text:
        return False
    return "----- Positive #" in text or "No positive predictions" in text


def _check_arxiv_update_date() -> Any:
    """在工作线程中连同可选抓取依赖一起导入；测试或禁用路径无需提前导入。"""

    return importlib.import_module(f"{__package__}.arxiv_today").check_arxiv_update_date()


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
        raw_date = await run_sync(_check_arxiv_update_date)
    except Exception as exc:
        public_error_message(
            context,
            exc,
            logger    = logger,
            component = "arxiv_filter.source_date",
        )
        return None
    source_date = _normalize_source_date(raw_date)
    if raw_date is not None and source_date is None:
        logger.warning("忽略无效的 arXiv 源列表日期")
    return source_date


def _load_inference(
    context: PluginContextProtocol | None = None,
) -> InferenceFunction | None:
    """首次调用时导入推理模块并缓存推理函数；缺少依赖时返回 None。"""
    global _inference_func

    if _inference_func is not None:
        logger.debug("使用已缓存的推理函数")
        return _inference_func

    logger.info("开始加载推理模块")

    try:
        from .inference.runner import get_positive_arxiv_today_as_string

        _inference_func = get_positive_arxiv_today_as_string
        logger.info("成功加载推理模块")
        return _inference_func
    except ImportError as exc:
        if context is not None:
            public_error_message(
                context,
                exc,
                logger    = logger,
                component = "arxiv_filter.load_inference",
            )
        else:
            logger.error("导入推理模块失败: error_type=ImportError")
        logger.error("请确保安装了所需依赖: torch, transformers")
        return None
    except Exception as exc:
        if context is not None:
            public_error_message(
                context,
                exc,
                logger    = logger,
                component = "arxiv_filter.load_inference",
            )
        else:
            logger.error(
                "加载推理模块时发生异常: error_type=%s",
                type(exc).__name__,
            )
        return None


def init(context: PluginContextProtocol | None = None) -> None:
    """新一代插件加载时清空进程内缓存，下次调用重新加载推理模块。"""

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
        raw_args = args.strip()

        # 只接受空参数或单独的 help；--date 这类未声明选项不能被当成空参数执行筛选。
        if raw_args:
            if raw_args.lower() in {"help", "帮助"}:
                return segments(_HELP_TEXT)
            unknown = bounded_external_text(
                raw_args,
                max_chars = 32,
                max_bytes = 128,
                suffix    = "…",
            )
            return segments(f"未知命令: {unknown}\n输入 /arxiv help 查看帮助")

        source_date = await _latest_arxiv_source_date(context)
        return await _run_filter(
            context,
            allow_codex_sidecar = _codex_summary_granted(context),
            source_date         = source_date,
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


def _codex_summary_granted(context: PluginContextProtocol) -> bool:
    """Core 只在此人此刻能用 ``/codex``（plugin.json 声明）时发出 Codex 摘要能力。"""

    capabilities = getattr(context, "capabilities", None)
    return getattr(capabilities, "codex_arxiv_summary", None) is not None


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
    source_date: str | None   = None,
) -> FilterResult:
    """执行论文筛选；结果的 succeeded/outcome 供定时投递决定提交还是重试。"""
    if source_date is not None:
        normalized_source_date = _normalize_source_date(source_date)
        if normalized_source_date is None:
            raise ValueError("source_date must be an ISO calendar date")
        source_date = normalized_source_date

    # 加载配置
    config       = load_plugin_config()
    model_config = config.get("model", {})
    if not isinstance(model_config, Mapping):
        raise ValueError("arxiv_filter model config must be a JSON object")
    environment_model_path = os.environ.get("ARXIV_MODEL_PATH", "").strip()
    configured_model_path  = environment_model_path or model_config.get("path", "best_model")
    if not isinstance(configured_model_path, str) or not configured_model_path.strip():
        raise ValueError("arxiv_filter model.path must be a non-empty string")
    configured_model_path = configured_model_path.strip()

    # 加载推理函数
    inference = _load_inference(context=context)
    if inference is None:
        logger.error("加载推理模块失败")
        return _filter_result(
            '⚠️ 无法加载 AI 模型或依赖，请检查模型配置；缺少依赖时请运行 pip install "xiaoqing[arxiv-ml]"。',
            succeeded = False,
            outcome   = "model_unavailable",
        )

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
            return _failure(
                context,
                RuntimeError("arXiv inference returned an error result"),
                "arxiv_filter.inference_result",
                "inference_error",
            )

        # 检查是否没有结果
        if "No positive predictions" in arxiv_text:
            logger.info("今日没有符合条件的论文")
            return _filter_result(
                f"📚 {_list_description(context, source_date)}，暂时没有发现感兴趣的论文。",
                succeeded = True,
                outcome   = "no_positive_predictions",
            )

        if "No papers found" in arxiv_text:
            return _failure(
                context,
                RuntimeError("arXiv inference did not produce a usable paper list"),
                "arxiv_filter.inference_result",
                "inference_error",
            )

        if "----- Positive #" not in arxiv_text:
            return _failure(
                context,
                RuntimeError("arXiv inference returned an unknown result format"),
                "arxiv_filter.inference_result",
                "unknown_result",
            )

        logger.debug("筛选结果预览: %s...", arxiv_text[:200])

    except FileNotFoundError as exc:
        return _failure(context, exc, "arxiv_filter.model_file", "model_file_error")
    except ImportError as exc:
        return _failure(context, exc, "arxiv_filter.dependencies", "dependency_error")
    except Exception as exc:
        return _failure(context, exc, "arxiv_filter.run", "runtime_error")

    # 格式化输出
    header = f"📚 {_list_description(context, source_date)}，以下是你可能感兴趣的论文：\n"
    if allow_codex_sidecar:
        if source_date is None:
            logger.warning("skip Codex arXiv summary: source list date could not be confirmed")
        else:
            try:
                schedule_codex_summary_from_filter_result(
                    context,
                    date        = source_date,
                    filter_text = arxiv_text,
                )
            except Exception as exc:
                public_error_message(
                    context,
                    exc,
                    logger    = logger,
                    component = "arxiv_filter.codex_sidecar",
                )
    return _filter_result(header + arxiv_text, succeeded=True, outcome="papers")


_HELP_TEXT = """
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
    try:
        atomic_write_text(
            _get_status_file_path(data_dir), json.dumps(status, ensure_ascii=False, indent=2)
        )
    except Exception as exc:
        logger.error("保存状态文件失败: error_type=%s", type(exc).__name__)
        raise


def _should_send_today(data_dir: str | Path, business_date: str) -> bool:
    """按调用方已经解析好的配置时区日期检查发送状态。"""

    return _load_update_status(data_dir).get("last_sent_date") != business_date


def _claim_path(data_dir: str | Path, business_date: str) -> Path:
    return _get_status_file_path(data_dir).with_name(f"claim-{business_date}")


def _claim_send_today(data_dir: str | Path, business_date: str) -> bool:
    """用 O_EXCL 创建当日 claim 文件，保证同一业务日只有一个检查者进入发送路径；超过 1 小时的 claim 视为遗留。"""

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
    _save_update_status(
        data_dir,
        {
            "last_sent_date": business_date,
            "last_sent_time": datetime.datetime.now(datetime.UTC).isoformat(),
        },
    )
    _release_claim(data_dir, business_date)


def _track_delivery(
    result: FilterResult,
    data_dir: str | Path,
    business_date: str,
) -> DeliverySegments:
    # 传输回执会在插件 handler 返回后到达，此时插件执行作用域已经关闭。
    # 状态落盘只依赖线程安全的文件操作，因此使用独立线程，不再借用插件 bulkhead。
    async def commit() -> None:
        try:
            await asyncio.to_thread(_mark_sent_today, data_dir, business_date)
        except Exception:
            await asyncio.to_thread(_release_claim, data_dir, business_date)
            raise

    async def rollback() -> None:
        await asyncio.to_thread(_release_claim, data_dir, business_date)

    receipt = DeliveryReceipt(
        expected_actions = 1,
        commit           = commit,
        rollback         = rollback,
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
    """定时检查 arXiv 是否已更新到今天；is_final_check 为最后一次（12 点）检查，仍未更新时发停更通知。"""
    if _scheduled_without_delivery_targets(context):
        logger.info("skip scheduled arXiv update check: no explicit delivery targets")
        return []

    data_dir = Path(context.data_dir)

    today = _business_now(context).date().isoformat()

    # 通过原子 claim 保证同一业务日期只有一个检查者进入发送路径。
    try:
        cancellation = DeferredCancellation()
        claim_task   = asyncio.create_task(run_sync(_claim_send_today, data_dir, today))
        claimed      = await await_owned_task(claim_task, cancellation)
        if cancellation.error is not None:
            if claimed:
                release_task = asyncio.create_task(run_sync(_release_claim, data_dir, today))
                await await_owned_task(release_task, cancellation)
            cancellation.raise_if_requested()
    except Exception as exc:
        # 状态不可信时采用 fail-closed：宁可漏掉一次自动播报，也不能重复群发。
        public_error_message(
            context,
            exc,
            logger    = logger,
            component = "arxiv_filter.claim_delivery",
        )
        return []
    if not claimed:
        logger.info("今天已经发送过 arXiv 更新，跳过此次检查")
        return []

    transferred = False
    try:
        try:
            # 连同可选抓取依赖一起放到工作线程；测试或禁用路径无需提前导入。
            arxiv_date = await run_sync(_check_arxiv_update_date)
        except Exception as exc:
            public_error_message(
                context,
                exc,
                logger    = logger,
                component = "arxiv_filter.check_date",
            )
            return []

        # 如果 arXiv 已更新到今天
        if arxiv_date == today:
            logger.info("检测到 arXiv 已更新到 %s，开始筛选论文...", today)
            try:
                result = await _run_filter(
                    context,
                    allow_codex_sidecar = True,
                    source_date         = arxiv_date,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                return _failure(context, exc, "arxiv_filter.scheduled_filter", "runtime_error")
            if isinstance(result, FilterResult) and result.succeeded:
                transferred = True
                return _track_delivery(result, data_dir, today)
            logger.warning("arXiv filter failed after update detection; keep retrying later today")
            return result

        # 如果是最后一次检查且仍未更新
        if is_final_check:
            logger.info(
                "最后检查时间已到，arXiv 仍未更新（当前日期: %s），发送停更通知", arxiv_date
            )
            result = _filter_result(
                f"📚 arXiv 今日（{today}）暂未更新，可能稍后更新或今日停更。",
                succeeded = True,
                outcome   = "no_update",
            )
            transferred = True
            return _track_delivery(result, data_dir, today)

        # 还不是最后检查，继续等待
        logger.info("arXiv 尚未更新到今天（当前: %s，期望: %s），等待下次检查", arxiv_date, today)
        return []
    finally:
        if not transferred:
            cleanup      = asyncio.create_task(run_sync(_release_claim, data_dir, today))
            cancellation = DeferredCancellation()
            await await_owned_task(cleanup, cancellation)
            cancellation.raise_if_requested()
