"""把业务异常和内部异常转换成统一、安全的命令响应。"""

import functools
import inspect
import logging
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from typing import Any, TypeVar, cast, overload

from core.public_errors import public_error_message

from ..core.exceptions import PendoException

logger = logging.getLogger(__name__)
_command_error_depth: ContextVar[int] = ContextVar("pendo_command_error_depth", default=0)
_AsyncCallable = TypeVar("_AsyncCallable", bound=Callable[..., Awaitable[Any]])


def _context_from_call(
    target: Callable[..., Any], args: tuple[Any, ...], kwargs: dict[str, Any]
) -> Any | None:
    """按函数签名解析具名 ``context``，避免猜测位置参数。"""
    explicit = kwargs.get("context")
    if explicit is not None:
        return explicit
    try:
        return inspect.signature(target).bind_partial(*args, **kwargs).arguments.get("context")
    except (TypeError, ValueError):
        return None


def success_result(message: str, **extra: Any) -> dict[str, Any]:
    """构造成功响应。"""
    return {"status": "success", "message": message, **extra}


def error_result(message: str, **extra: Any) -> dict[str, Any]:
    """构造失败响应。"""
    return {"status": "error", "message": message, **extra}


def info_result(message: str, **extra: Any) -> dict[str, Any]:
    """构造不改变数据的提示响应。"""
    return {"status": "info", "message": message, **extra}


@overload
def handle_command_errors(
    func: _AsyncCallable, *, return_segments: bool = False
) -> _AsyncCallable: ...


@overload
def handle_command_errors(
    func: None = None, *, return_segments: bool = False
) -> Callable[[_AsyncCallable], _AsyncCallable]: ...


def handle_command_errors(
    func: _AsyncCallable | None = None, *, return_segments: bool = False
) -> _AsyncCallable | Callable[[_AsyncCallable], _AsyncCallable]:
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

    def decorator(target: _AsyncCallable) -> _AsyncCallable:
        @functools.wraps(target)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            parent_depth = _command_error_depth.get()
            depth_token  = _command_error_depth.set(parent_depth + 1)
            try:
                return await target(*args, **kwargs)
            except PendoException as exc:
                if parent_depth:
                    raise
                context    = _context_from_call(target, args, kwargs)
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
                log     = getattr(context, "logger", None) or logger
                message = public_error_message(
                    context,
                    exc,
                    logger    = log,
                    component = f"pendo.{target.__name__}",
                )
                if return_segments:
                    return [{"type": "text", "data": {"text": message}}]
                return error_result(message)
            finally:
                _command_error_depth.reset(depth_token)

        return cast(_AsyncCallable, wrapper)

    if func is not None:
        return decorator(func)

    return decorator
