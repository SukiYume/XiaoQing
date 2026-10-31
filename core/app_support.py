# 应用共享状态：凭据快照解析、生命周期记录与致命异常包装。
"""Shared application credentials, lifecycle records, and fatal-error carriers."""

import asyncio
import logging
import math
from collections.abc import Awaitable, Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal, cast, overload

from .config import ConfigSnapshot, ConfigSourceStatus

logger = logging.getLogger(__name__)

_PLUGIN_WATCH_RESTART_BASE_DELAY_SECONDS    = 0.1
_PLUGIN_WATCH_RESTART_MAX_DELAY_SECONDS     = 30.0
_PLUGIN_WATCH_STABLE_RESET_SECONDS          = 30.0
_STARTUP_OWNERSHIP_MAX_ATTEMPTS             = 64
_STARTUP_OWNERSHIP_TIMEOUT_SECONDS          = 5.0
_STARTUP_OWNERSHIP_RETRY_BASE_DELAY_SECONDS = 0.001
_STARTUP_OWNERSHIP_RETRY_MAX_DELAY_SECONDS  = 0.05

Action     = dict[str, Any]
ActionSink = Callable[[Action], Awaitable[None]]
current_action_sink: ContextVar[ActionSink | None] = ContextVar("current_action_sink", default=None)


@overload
def _coerce_runtime_number(
    raw_value: Any,
    *,
    key: str,
    default: int | float,
    integer: Literal[True],
    minimum: int | float,
    maximum: int | float,
) -> int: ...


@overload
def _coerce_runtime_number(
    raw_value: Any,
    *,
    key: str,
    default: int | float,
    integer: Literal[False],
    minimum: int | float,
    maximum: int | float,
) -> float: ...


def _coerce_runtime_number(
    raw_value: Any,
    *,
    key: str,
    default: int | float,
    integer: bool,
    minimum: int | float,
    maximum: int | float,
) -> int | float:
    """Parse one runtime scalar before publishing any configuration side effects."""

    value = default if raw_value is None else raw_value
    if isinstance(value, bool):
        raise ValueError(f"configuration field {key!r} must be numeric, not bool")
    try:
        if integer:
            if isinstance(value, float) and not value.is_integer():
                raise ValueError
            parsed: int | float = int(value, 10) if isinstance(value, str) else int(value)
            if isinstance(value, float) and parsed != value:
                raise ValueError
        else:
            parsed = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"invalid numeric configuration field {key!r}: {value!r}") from exc
    if not math.isfinite(float(parsed)) or parsed < minimum or parsed > maximum:
        raise ValueError(f"configuration field {key!r} must be between {minimum} and {maximum}")
    return parsed


def _trusted_secrets(snapshot: ConfigSnapshot) -> Mapping[str, Any]:
    """Expose secret values only when their source was read successfully."""

    if snapshot.secrets_status is ConfigSourceStatus.VALID:
        return cast(Mapping[str, Any], snapshot.secrets)
    return {}


def _onebot_credentials(snapshot: ConfigSnapshot) -> tuple[str, bool]:
    """Return a token only when its source and value type are trustworthy.

    A missing key or explicit empty string in a VALID secrets document is the
    documented anonymous mode.  Source failure and malformed values are a
    distinct revoked state and must never be converted into anonymous access.
    """

    trusted = _trusted_secrets(snapshot)
    if not trusted and snapshot.secrets_status is not ConfigSourceStatus.VALID:
        return "", False
    raw_token = trusted.get("onebot_token", "")
    if type(raw_token) is not str:
        logger.error(
            "Invalid onebot_token type in a valid secrets snapshot: %s; outbound OneBot "
            "transports are revoked",
            type(raw_token).__name__,
        )
        return "", False
    return raw_token, True


def _inbound_credentials(snapshot: ConfigSnapshot) -> str:
    """Return an exact string token from a healthy secrets source only."""

    trusted   = _trusted_secrets(snapshot)
    raw_token = trusted.get("inbound_token", "")
    if type(raw_token) is not str:
        logger.error(
            "Invalid inbound_token type in a valid secrets snapshot: %s; inbound "
            "authentication is revoked",
            type(raw_token).__name__,
        )
        return ""
    return raw_token


def _parse_positive_ids(raw_ids: object, *, field_name: str) -> tuple[int, ...]:
    """Parse a complete positive-ID list without partial acceptance."""

    if not isinstance(raw_ids, (list, tuple)):
        raise TypeError(f"{field_name} must be a list")
    parsed: list[int] = []
    for raw_id in raw_ids:
        if type(raw_id) is int:
            user_id = raw_id
        elif type(raw_id) is str and raw_id and raw_id.isascii() and raw_id.isdecimal():
            user_id = int(raw_id)
        else:
            raise TypeError(f"{field_name} entries must be integers or decimal strings")
        if user_id <= 0:
            raise ValueError(f"{field_name} entries must be positive")
        parsed.append(user_id)
    return tuple(parsed)


def _require_onebot_holder_credentials(
    holder: Any,
    *,
    endpoint_attribute: str,
    expected_endpoint: str,
    expected_token: str,
    expected_trust: bool,
) -> None:
    """Prove that a legacy/mocked holder actually applied an auth update."""

    actual_token    = getattr(holder, "auth_token", None)
    actual_trust    = getattr(holder, "credentials_trusted", None)
    actual_endpoint = getattr(holder, endpoint_attribute, None)
    if (
        actual_endpoint != expected_endpoint
        or actual_token != expected_token
        or actual_trust is not expected_trust
    ):
        raise RuntimeError("OneBot holder did not apply the requested credential state")


class _AppLifecycleState(Enum):
    NEW      = "new"
    STARTING = "starting"
    RUNNING  = "running"
    STOPPING = "stopping"
    STOPPED  = "stopped"
    FAILED   = "failed"


@dataclass(frozen=True, slots=True)
class _ConfigApplyOwner:
    """Identity of the only runtime-config task allowed to publish side effects."""

    generation         : int
    revision           : int
    security_generation: int


class InboundReconcileError(RuntimeError):
    """Both an inbound candidate and restoration of the previous listener failed."""

    def __init__(self, candidate_error: BaseException, restore_error: BaseException) -> None:
        super().__init__(
            "inbound candidate failed and the previous listener could not be restored: "
            f"candidate={type(candidate_error).__name__}: {candidate_error}; "
            f"restore={type(restore_error).__name__}: {restore_error}"
        )
        self.candidate_error = candidate_error
        self.restore_error   = restore_error


class ApplicationLifecycleFatalError(RuntimeError):
    """Task-safe carrier for a non-Exception application lifecycle failure."""

    def __init__(self, original: BaseException) -> None:
        super().__init__(f"application lifecycle failed: {type(original).__name__}: {original}")
        self.original = original


async def _run_background_operation(operation_factory: Callable[[], Awaitable[Any]]) -> Any:
    """Convert fatal child failures without changing ordinary cancellation."""

    try:
        return await operation_factory()
    except (asyncio.CancelledError, Exception):
        # 普通异常和取消保持原样，只包装 KeyboardInterrupt 这类致命异常。
        raise
    except BaseException as exc:
        raise ApplicationLifecycleFatalError(exc) from None
