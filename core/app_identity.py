# 应用身份边界：管理员视图与主体授权随可信配置代发布。
"""Application-owned administrator and principal authority boundary."""

from __future__ import annotations

import logging
import weakref
from collections.abc import Mapping
from typing import Any

from .app_support import _parse_positive_ids
from .interfaces import DeliveryTarget, PluginPrincipal, ScheduleDeliveryMode

logger = logging.getLogger(__name__)


class AppIdentityService:
    """Own administrator state and every principal minted by one application."""

    def __init__(self) -> None:
        self._admin_ids: set[int] = set()
        # 按对象身份记录本应用签发的主体，伪造或跨应用的主体都不会被承认。
        self._issued: weakref.WeakKeyDictionary[PluginPrincipal, str] = weakref.WeakKeyDictionary()

    @property
    def admin_ids(self) -> set[int]:
        """Return current administrator IDs for application-owned operations."""

        return self._admin_ids

    def load_admins(self, secrets: Mapping[str, Any]) -> None:
        """Atomically replace the administrator set or revoke it on invalid input."""

        try:
            replacement = set(
                _parse_positive_ids(secrets.get("admin_user_ids", []), field_name="admin_user_ids")
            )
        except (TypeError, ValueError):
            logger.warning("Invalid admin_user_ids in secrets")
            replacement = set()
        self._admin_ids.clear()
        self._admin_ids.update(replacement)

    def is_admin(self, user_id: int | None) -> bool:
        if type(user_id) is not int or user_id <= 0:
            return False
        return user_id in self._admin_ids

    def issue_user_principal(
        self,
        event: Mapping[str, Any],
        *,
        user_id: int | None,
        group_id: int | None,
        is_private: bool,
    ) -> PluginPrincipal:
        """Mint a user principal after binding any verified group role."""

        role   = "unknown"
        sender = event.get("sender")
        if group_id is not None and isinstance(sender, Mapping):
            sender_user_id = sender.get("user_id")
            try:
                sender_matches = (
                    sender_user_id is not None
                    and user_id is not None
                    and int(sender_user_id) == int(user_id)
                )
            except (TypeError, ValueError):
                sender_matches = False
            candidate_role = str(sender.get("role", "") or "").strip().lower()
            if sender_matches and candidate_role in {"owner", "admin", "member"}:
                role = candidate_role
        return self.issue(
            kind         = "user",
            user_id      = user_id,
            group_id     = group_id,
            is_bot_admin = self.is_admin(user_id),
            is_private   = is_private,
            group_role   = role,
        )

    def issue(
        self,
        *,
        kind: str,
        user_id: int | None                                 = None,
        group_id: int | None                                = None,
        is_bot_admin: bool                                  = False,
        is_private: bool                                    = False,
        group_role: str                                     = "unknown",
        delivery_targets: tuple[DeliveryTarget, ...] | None = None,
        schedule_delivery: ScheduleDeliveryMode | None      = None,
    ) -> PluginPrincipal:
        """Mint and register one principal; user principals default to their own chat."""

        if delivery_targets is None:
            if kind == "user" and group_id is not None:
                delivery_targets = (DeliveryTarget("group", int(group_id)),)
            elif kind == "user" and user_id is not None:
                delivery_targets = (DeliveryTarget("private", int(user_id)),)
            else:
                delivery_targets = ()
        principal = PluginPrincipal(
            kind              = kind,  # type: ignore[arg-type]
            user_id           = user_id,
            group_id          = group_id,
            is_bot_admin      = is_bot_admin,
            is_private        = is_private,
            group_role        = group_role,  # type: ignore[arg-type]
            delivery_targets  = delivery_targets,
            schedule_delivery = schedule_delivery,
        )
        self._issued[principal] = kind
        return principal

    def owns(self, principal: PluginPrincipal) -> bool:
        return bool(self._issued.get(principal) == principal.kind)
