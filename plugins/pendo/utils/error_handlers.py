"""
统一错误处理装饰器

提供一致的错误处理机制，包括：
- 业务异常的友好提示
- 未预期异常的详细日志
- 错误ID生成用于追踪
"""

import functools
import inspect
import logging
from collections.abc import Callable
from contextvars import ContextVar
from typing import Any

from core.public_errors import public_error_message

from ..core.exceptions import PendoException

logger = logging.getLogger(__name__)
_command_error_depth: ContextVar[int] = ContextVar("pendo_command_error_depth", default=0)


def _context_from_call(
    target: Callable[..., Any], args: tuple[Any, ...], kwargs: dict[str, Any]
) -> Any | None:
    """Resolve a named context argument without guessing from positional slots."""
    explicit = kwargs.get("context")
    if explicit is not None:
        return explicit
    try:
        return inspect.signature(target).bind_partial(*args, **kwargs).arguments.get("context")
    except (TypeError, ValueError):
        return None


def build_result(status: str, message: str, **extra: Any) -> dict[str, Any]:
    """Build a consistent command result payload."""
    result: dict[str, Any] = {"status": status, "message": message}
    result.update(extra)
    return result


def success_result(message: str, **extra: Any) -> dict[str, Any]:
    return build_result("success", message, **extra)


def error_result(message: str, **extra: Any) -> dict[str, Any]:
    return build_result("error", message, **extra)


def info_result(message: str, **extra: Any) -> dict[str, Any]:
    return build_result("info", message, **extra)


def preview_result(message: str, **extra: Any) -> dict[str, Any]:
    return build_result("preview", message, **extra)


def handle_command_errors(
    func: Callable[..., Any] | None = None, *, return_segments: bool = False
) -> Callable[..., Any]:
    """统一的命令错误处理装饰器

    自动处理命令执行中的异常，提供一致的错误响应格式。

    处理两类异常：
    1. PendoException: 业务异常，返回用户友好的错误消息
    2. Exception: 未预期异常，生成错误ID并记录完整堆栈

    Args:
        func: 要装饰的异步函数
        return_segments: 是否返回消息段列表格式

    Returns:
        包装后的函数

    Examples:
        >>> @handle_command_errors
        ... async def handle(command: str, args: str, event: dict, context):
        ...     return {'status': 'success', 'message': '操作成功'}
        >>> @handle_command_errors(return_segments=True)
        ... async def handle_segments(command: str, args: str, event: dict, context):
        ...     return [{"type": "text", "data": {"text": "成功"}}]
    """

    def decorator(target: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(target)
        async def wrapper(*args, **kwargs) -> Any:
            parent_depth = _command_error_depth.get()
            depth_token = _command_error_depth.set(parent_depth + 1)
            try:
                return await target(*args, **kwargs)
            except PendoException as exc:
                if parent_depth:
                    raise
                context = _context_from_call(target, args, kwargs)
                request_id = str(getattr(context, "request_id", "") or "-")
                logger.warning(
                    "Pendo business error error_code=%s error_type=%s",
                    exc.error_code,
                    type(exc).__name__,
                    extra={"request_id": request_id},
                )
                if return_segments:
                    return [{"type": "text", "data": {"text": exc.get_user_message()}}]
                return error_result(exc.get_user_message(), error_code=exc.error_code)
            except Exception as exc:
                if parent_depth:
                    raise
                context = _context_from_call(target, args, kwargs)
                log = getattr(context, "logger", None) or logger
                message = public_error_message(
                    context,
                    exc,
                    logger=log,
                    component=f"pendo.{target.__name__}",
                )
                if return_segments:
                    return [{"type": "text", "data": {"text": message}}]
                return error_result(message)
            finally:
                _command_error_depth.reset(depth_token)

        return wrapper

    if func is not None:
        return decorator(func)

    return decorator


def handle_command_errors_with_segments(func: Callable[..., Any]) -> Callable[..., Any]:
    """兼容旧接口：返回消息段列表"""
    return handle_command_errors(func, return_segments=True)


def handle_scheduled_task_errors(task_name: str) -> Callable[..., Any]:
    """定时任务错误处理装饰器

    专门用于定时任务的错误处理，异常时返回空列表而不是错误消息。
    适合不需要向用户通知错误的后台任务。

    Args:
        task_name: 任务名称，用于日志记录

    Returns:
        装饰器函数

    Examples:
        >>> @handle_scheduled_task_errors("check_reminders")
        ... async def check_reminders(context):
        ...     # 检查提醒...
        ...     return []
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(func)
        async def wrapper(*args, **kwargs) -> list[dict[str, Any]]:
            try:
                result = await func(*args, **kwargs)
                return result if result else []
            except Exception as exc:
                context = _context_from_call(func, args, kwargs)
                log = getattr(context, "logger", None) or logger
                public_error_message(
                    context,
                    exc,
                    logger=log,
                    component=f"pendo.scheduled.{task_name}",
                )
                return []

        return wrapper

    return decorator


def log_exceptions(logger_instance: logging.Logger | None = None) -> Callable[..., Any]:
    """兼容旧接口：保留包装器，但把异常原样交给最外层边界。

    Args:
        logger_instance: 日志记录器实例，不提供则使用模块logger

    Returns:
        装饰器函数

    Examples:
        >>> @log_exceptions(my_logger)
        ... def some_function():
        ...     # 函数逻辑...
        ...     pass
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        _ = logger_instance

        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs):
            return await func(*args, **kwargs)

        @functools.wraps(func)
        def sync_wrapper(*args, **kwargs):
            return func(*args, **kwargs)

        # 判断是否为协程函数
        if inspect.iscoroutinefunction(func):
            return async_wrapper
        else:
            return sync_wrapper

    return decorator
