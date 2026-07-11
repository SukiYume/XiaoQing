from __future__ import annotations

from typing import Any


async def safe_end_session(context: Any) -> bool:
    """Safely end an active session when the context supports it."""
    if context is None:
        return False

    end_session = getattr(context, "end_session", None)
    if not callable(end_session):
        return False

    result = await end_session()
    return bool(result)


async def safe_create_session(
    context: Any,
    initial_data: dict[str, Any] | None = None,
    timeout: float = 300.0,
) -> bool:
    """Safely create a session when the context supports it."""
    if context is None:
        return False

    create_session = getattr(context, "create_session", None)
    if not callable(create_session):
        return False

    await create_session(initial_data=initial_data, timeout=timeout)
    return True


async def safe_create_reply_scoped_session(
    context: Any,
    initial_data: dict[str, Any] | None = None,
    timeout: float = 300.0,
) -> bool:
    """Create a session in the chat where the next prompt will be answered.

    Pendo can turn group replies into private messages for privacy. In that
    case, the visible continuation happens in private chat, so the session must
    also be keyed as a private session while preserving the source group in
    ``initial_data``.
    """
    if not _should_create_private_reply_session(context):
        return await safe_create_session(context, initial_data=initial_data, timeout=timeout)

    session_manager = getattr(context, "session_manager", None)
    create = getattr(session_manager, "create", None)
    current_user_id = getattr(context, "current_user_id", None)
    plugin_name = getattr(context, "plugin_name", None)
    if callable(create) and current_user_id is not None and plugin_name:
        await create(
            user_id=current_user_id,
            group_id=None,
            plugin_name=plugin_name,
            initial_data=initial_data,
            timeout=timeout,
        )
        return True

    if hasattr(context, "current_group_id"):
        original_group_id = context.current_group_id
        try:
            context.current_group_id = None
            return await safe_create_session(context, initial_data=initial_data, timeout=timeout)
        finally:
            context.current_group_id = original_group_id

    return await safe_create_session(context, initial_data=initial_data, timeout=timeout)


def _should_create_private_reply_session(context: Any) -> bool:
    return bool(getattr(context, "_pendo_reply_private", False))
