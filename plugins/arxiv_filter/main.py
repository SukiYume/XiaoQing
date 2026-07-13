"""
arXiv 论文筛选插件
基于 BERT 模型筛选感兴趣的 arXiv 论文
"""

import asyncio
import datetime
import importlib
import json
import logging
import os
import sys
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from core.public_errors import public_error_message, public_error_response

from .codex_summary import schedule_codex_summary_from_filter_result
from .utils import load_plugin_config

plugin_base = importlib.import_module("core.plugin_base")
args_module = importlib.import_module("core.args")

segments = plugin_base.segments
run_sync = plugin_base.run_sync
atomic_write_text = plugin_base.atomic_write_text
parse = args_module.parse


logger = logging.getLogger(__name__)

_FILTER_ERROR_MARKERS = (
    "无法加载AI模型",
    "论文获取失败",
    "模型文件不完整",
    "系统依赖不完整",
    "论文筛选服务暂时不可用",
)


# ============================================================
# 模块加载
# ============================================================

_inference_func = None
_FILTER_CACHE: dict[tuple[str, str, Any], str] = {}
_FILTER_INFLIGHT: dict[tuple[str, str, Any], asyncio.Task[str]] = {}
_FILTER_LOCK = asyncio.Lock()
_STATUS_LOCK = threading.Lock()


def _business_now(context: Any | None = None) -> datetime.datetime:
    timezone_name = "Asia/Shanghai"
    if context is not None:
        timezone_name = str((getattr(context, "config", {}) or {}).get("timezone", timezone_name))
    try:
        zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        zone = ZoneInfo("Asia/Shanghai")
    return datetime.datetime.now(zone)


async def _cached_inference(
    context: Any,
    inference: Any,
    model_path: str,
) -> str:
    business_date = _business_now(context).date().isoformat()
    key = (business_date, str(model_path), inference)
    cached = _FILTER_CACHE.get(key)
    if cached is not None:
        return cached
    async with _FILTER_LOCK:
        task = _FILTER_INFLIGHT.get(key)
        if task is None:
            task = asyncio.create_task(
                run_sync(lambda: inference(model_path=model_path))
            )
            _FILTER_INFLIGHT[key] = task
    try:
        result = await asyncio.shield(task)
        if not result.startswith("Error:"):
            _FILTER_CACHE[key] = result
        return result
    finally:
        if task.done():
            async with _FILTER_LOCK:
                if _FILTER_INFLIGHT.get(key) is task:
                    _FILTER_INFLIGHT.pop(key, None)


def _load_inference(force_reload: bool = False, context: Any | None = None):
    """
    动态加载推理模块

    Args:
        force_reload: 是否强制重新加载

    Returns:
        推理函数或 None
    """
    global _inference_func

    if _inference_func is not None and not force_reload:
        logger.debug("使用已缓存的推理函数")
        return _inference_func

    logger.info(f"开始加载推理模块，force_reload={force_reload}")

    # 强制重新加载时，刷新 sys.modules 中已缓存的模块
    if force_reload:
        reloaded_modules = []
        package_prefix = __package__ or ""
        inference_module_name = f"{package_prefix}.arxiv_inference" if package_prefix else ""
        inference_package_name = f"{package_prefix}.inference" if package_prefix else ""
        for key in list(sys.modules):
            if inference_module_name and key == inference_module_name:
                importlib.reload(sys.modules[key])
                reloaded_modules.append(key)
            elif inference_package_name and (
                key == inference_package_name or key.startswith(f"{inference_package_name}.")
            ):
                importlib.reload(sys.modules[key])
                reloaded_modules.append(key)
        if reloaded_modules:
            logger.info("已重新加载推理相关模块: %s", ", ".join(sorted(reloaded_modules)))

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


def init(context=None) -> None:
    """
    插件初始化

    清除模块缓存，下次调用时重新加载推理模块。
    这确保插件更新后能立即生效。

    Args:
        context: 插件上下文（可选）
    """
    global _inference_func
    _inference_func = None
    _FILTER_CACHE.clear()
    logger.info("arXiv Filter 插件已初始化")


# ============================================================
# 主处理函数
# ============================================================


async def handle(command: str, args: str, event: dict[str, Any], context) -> Any:
    """命令处理入口"""
    try:
        parsed = parse(args)

        # 解析子命令
        if parsed and parsed.first:
            subcommand = parsed.first.lower()

            if subcommand == "help" or subcommand == "帮助":
                return segments(_show_help())

        return await _run_filter(
            context,
            allow_codex_sidecar=_is_admin_user(context, event.get("user_id")),
        )

    except Exception as exc:
        return public_error_response(context, exc, logger=logger, component="arxiv_filter.handle")


async def scheduled(context) -> Any:
    """定时任务入口"""
    if _scheduled_without_delivery_targets(context):
        logger.info("skip scheduled arXiv filter: no explicit delivery targets")
        return []
    return await _run_filter(context, allow_codex_sidecar=True)


async def shutdown(context) -> None:
    tasks = list(_FILTER_INFLIGHT.values())
    state = getattr(context, "state", None)
    if isinstance(state, dict):
        tasks.extend(state.get("arxiv_background_tasks", set()))
        state.get("arxiv_background_tasks", set()).clear()
    for task in set(tasks):
        task.cancel()
    if tasks:
        await asyncio.gather(*set(tasks), return_exceptions=True)


def _is_admin_user(context: Any, user_id: Any) -> bool:
    principal = getattr(context, "principal", None)
    capabilities = getattr(context, "capabilities", None)
    if principal is None or not getattr(capabilities, "is_bot_admin", False):
        return False
    try:
        return int(principal.user_id) == int(user_id)
    except (TypeError, ValueError):
        return False


def _scheduled_without_delivery_targets(context: Any) -> bool:
    principal = getattr(context, "principal", None)
    return bool(
        principal is not None
        and getattr(principal, "kind", None) == "scheduled_system"
        and not tuple(getattr(principal, "delivery_targets", ()))
    )
async def scheduled_check(context) -> Any:
    """定时检查 arXiv 是否更新"""
    return await _check_arxiv_update(context, is_final_check=False)


async def scheduled_final_check(context) -> Any:
    """最后一次检查（12点），如果仍未更新则发送停更通知"""
    return await _check_arxiv_update(context, is_final_check=True)


async def _run_filter(
    context,
    *,
    allow_codex_sidecar: bool = False,
) -> Any:
    """
    执行论文筛选

    Args:
        context: 插件上下文

    Returns:
        消息段列表
    """
    # 加载配置
    config = load_plugin_config()
    model_config = config.get("model", {})
    environment_model_path = os.environ.get("ARXIV_MODEL_PATH", "").strip()
    configured_model_path = environment_model_path or model_config.get("path", "best_model")

    # 加载推理函数
    inference = _load_inference(context=context)
    if inference is None:
        error_msg = (
            "⚠️ 无法加载 AI 模型或依赖，请检查模型配置；"
            '缺少依赖时请运行 pip install "xiaoqing[arxiv-ml]"。'
        )
        logger.error("加载推理模块失败")
        return segments(error_msg)

    try:
        logger.info(
            "开始执行 arXiv 论文筛选，model_path_configured=%s",
            bool(configured_model_path),
        )
        start_time = time.time()
        arxiv_text = await _cached_inference(context, inference, configured_model_path)
        elapsed = time.time() - start_time
        logger.info(
            f"arXiv 论文筛选完成，耗时 {elapsed:.2f} 秒，返回内容长度: {len(arxiv_text)} 字符"
        )

        # 检查是否有错误消息
        if arxiv_text.startswith("Error:"):
            return public_error_response(
                context,
                RuntimeError("arXiv inference returned an error result"),
                logger=logger,
                component="arxiv_filter.inference_result",
            )

        # 检查是否没有结果
        if "No positive predictions" in arxiv_text:
            logger.info("今日没有符合条件的论文")
            today = _business_now(context).date()
            return segments(f"📚 今天是 {today}，暂时没有发现感兴趣的论文。")

        logger.debug(f"筛选结果预览: {arxiv_text[:200]}...")

    except FileNotFoundError as exc:
        return public_error_response(
            context,
            exc,
            logger=logger,
            component="arxiv_filter.model_file",
        )
    except ImportError as exc:
        return public_error_response(
            context,
            exc,
            logger=logger,
            component="arxiv_filter.dependencies",
        )
    except Exception as exc:
        return public_error_response(
            context,
            exc,
            logger=logger,
            component="arxiv_filter.run",
        )

    # 格式化输出
    today = _business_now(context).date()
    header = f"📚 今天是 {today}，以下是你可能感兴趣的论文：\n"
    if allow_codex_sidecar:
        try:
            schedule_codex_summary_from_filter_result(
                context,
                date=today.isoformat(),
                filter_text=arxiv_text,
            )
        except Exception as exc:
            public_error_message(
                context,
                exc,
                logger=logger,
                component="arxiv_filter.codex_sidecar",
            )
    return segments(header + arxiv_text)


def _show_help() -> str:
    """显示帮助信息"""
    return """
📚 **arXiv 论文筛选**

基于 AI 模型自动筛选今日感兴趣的 arXiv 论文

**使用方法:**
• /arxiv - 获取今日筛选的论文
• /arxiv help - 显示帮助信息

**功能特点:**
- 基于 BERT 模型智能筛选
- 自动获取最新论文
- 根据研究兴趣推荐

输入 /arxiv 查看今日推荐论文
""".strip()


# ============================================================
# 状态管理
# ============================================================


def _get_status_file_path(plugin_dir: str) -> str:
    """获取状态文件路径"""
    data_dir = os.path.join(plugin_dir, "data")
    os.makedirs(data_dir, exist_ok=True)
    return os.path.join(data_dir, "update_status.json")


def _load_update_status(plugin_dir: str) -> dict[str, object]:
    """加载今日更新状态"""
    status_file = _get_status_file_path(plugin_dir)
    if os.path.exists(status_file):
        try:
            with open(status_file, encoding="utf-8") as f:
                return json.load(f)
        except Exception as exc:
            logger.warning(
                "加载状态文件失败: error_type=%s",
                type(exc).__name__,
            )
    return {}


def _save_update_status(plugin_dir: str, status: Mapping[str, object]) -> None:
    """保存今日更新状态"""
    status_file = _get_status_file_path(plugin_dir)
    try:
        atomic_write_text(Path(status_file), json.dumps(status, ensure_ascii=False, indent=2))
    except Exception as exc:
        logger.error(
            "保存状态文件失败: error_type=%s",
            type(exc).__name__,
        )


def _should_send_today(plugin_dir: str, business_date: str | None = None) -> bool:
    """检查今天是否已经发送过"""
    status = _load_update_status(plugin_dir)
    today = business_date or _business_now().date().isoformat()
    return status.get("last_sent_date") != today


def _claim_path(plugin_dir: str, business_date: str) -> str:
    return os.path.join(os.path.dirname(_get_status_file_path(plugin_dir)), f"claim-{business_date}")


def _claim_send_today(plugin_dir: str, business_date: str) -> bool:
    with _STATUS_LOCK:
        if not _should_send_today(plugin_dir, business_date):
            return False
        path = _claim_path(plugin_dir, business_date)
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


def _release_claim(plugin_dir: str, business_date: str) -> None:
    try:
        os.unlink(_claim_path(plugin_dir, business_date))
    except FileNotFoundError:
        pass


def _mark_sent_today(plugin_dir: str, business_date: str | None = None) -> None:
    """标记今天已发送"""
    today = business_date or _business_now().date().isoformat()
    status = {
        "last_sent_date": today,
        "last_sent_time": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    _save_update_status(plugin_dir, status)
    _release_claim(plugin_dir, today)


def _extract_result_text(result: Any) -> str:
    if isinstance(result, str):
        return result
    if not isinstance(result, list):
        return ""

    chunks: list[str] = []
    for segment in result:
        if segment.get("type") != "text":
            continue
        chunks.append(str(segment.get("data", {}).get("text", "")))
    return "\n".join(chunks)


def _is_successful_filter_result(result: Any) -> bool:
    payload = _extract_result_text(result)
    return not any(marker in payload for marker in _FILTER_ERROR_MARKERS)


# ============================================================
# arXiv 更新检查
# ============================================================


async def _check_arxiv_update(context, is_final_check: bool = False) -> Any:
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

    plugin_dir = str(context.plugin_dir)

    today = _business_now(context).date().isoformat()

    # 通过原子 claim 保证同一业务日期只有一个检查者进入发送路径。
    if not _claim_send_today(plugin_dir, today):
        logger.info("今天已经发送过 arXiv 更新，跳过此次检查")
        return []

    # 检查 arXiv 页面日期
    def _check_date():
        """检查日期（阻塞操作）"""
        from .arxiv_today import check_arxiv_update_date

        return check_arxiv_update_date()

    try:
        arxiv_date = await run_sync(_check_date)
    except Exception as exc:
        public_error_message(
            context,
            exc,
            logger=logger,
            component="arxiv_filter.check_date",
        )
        _release_claim(plugin_dir, today)
        return []

    # 如果 arXiv 已更新到今天
    if arxiv_date == today:
        logger.info(f"检测到 arXiv 已更新到 {today}，开始筛选论文...")
        result = await _run_filter(context, allow_codex_sidecar=True)
        if _is_successful_filter_result(result):
            _mark_sent_today(plugin_dir, today)
        else:
            _release_claim(plugin_dir, today)
            logger.warning("arXiv filter failed after update detection; keep retrying later today")
        return result

    # 如果是最后一次检查且仍未更新
    if is_final_check:
        logger.info(f"最后检查时间已到，arXiv 仍未更新（当前日期: {arxiv_date}），发送停更通知")
        _mark_sent_today(plugin_dir, today)
        return segments(f"📚 arXiv 今日（{today}）暂未更新，可能稍后更新或今日停更。")

    # 还不是最后检查，继续等待
    logger.info(f"arXiv 尚未更新到今天（当前: {arxiv_date}，期望: {today}），等待下次检查")
    _release_claim(plugin_dir, today)
    return []
