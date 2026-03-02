"""
统一错误处理装饰器

提供一致的错误处理机制，包括：
- 业务异常的友好提示
- 未预期异常的详细日志
- 错误ID生成用于追踪
"""
import uuid
import logging
import functools
from typing import Any, Callable

from ..core.exceptions import PendoException

logger = logging.getLogger(__name__)

def handle_command_errors(func: Callable = None, *, return_segments: bool = False) -> Callable:
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
    def decorator(target: Callable) -> Callable:
        @functools.wraps(target)
        async def wrapper(*args, **kwargs) -> Any:
            try:
                return await target(*args, **kwargs)
            except PendoException as e:
                e.log_error()
                if return_segments:
                    return [{"type": "text", "data": {"text": e.get_user_message()}}]
                return {
                    'status': 'error',
                    'message': e.get_user_message(),
                    'error_code': e.error_code
                }
            except Exception as e:
                error_id = uuid.uuid4().hex[:8]
                logger.exception("[%s] Unexpected error in %s: %s", error_id, target.__name__, e)
                if return_segments:
                    return [{
                        "type": "text",
                        "data": {"text": f"❌ 系统错误 (ID: {error_id})\n请联系管理员"}
                    }]
                return {
                    'status': 'error',
                    'message': f"❌ 系统错误 (ID: {error_id})\n请联系管理员",
                    'error_id': error_id
                }

        return wrapper

    if func is not None:
        return decorator(func)

    return decorator

def handle_command_errors_with_segments(func: Callable) -> Callable:
    """兼容旧接口：返回消息段列表"""
    return handle_command_errors(func, return_segments=True)

def handle_scheduled_task_errors(task_name: str) -> Callable:
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
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def wrapper(*args, **kwargs) -> list[dict[str, Any]]:
            try:
                result = await func(*args, **kwargs)
                return result if result else []
            except Exception as e:
                logger.exception("Scheduled task '%s' failed: %s", task_name, e)
                return []
        
        return wrapper
    
    return decorator

def log_exceptions(logger_instance: logging.Logger = None) -> Callable:
    """通用异常日志装饰器
    
    记录函数执行中的所有异常，但不影响异常传播。
    适用于需要记录异常但不处理的场景。
    
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
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs):
            log = logger_instance or logger
            try:
                return await func(*args, **kwargs)
            except Exception as e:
                log.exception("Exception in %s: %s", func.__name__, e)
                raise
        
        @functools.wraps(func)
        def sync_wrapper(*args, **kwargs):
            log = logger_instance or logger
            try:
                return func(*args, **kwargs)
            except Exception as e:
                log.exception("Exception in %s: %s", func.__name__, e)
                raise
        
        # 判断是否为协程函数
        if functools.iscoroutinefunction(func):
            return async_wrapper
        else:
            return sync_wrapper
    
    return decorator
