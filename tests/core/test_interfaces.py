"""投递目标和插件身份的验证、权限与不可变性测试。"""

from dataclasses import FrozenInstanceError

import pytest

from core.interfaces import (
    DeliveryTarget,
    PluginPrincipal,
)


def test_delivery_target_is_validated_and_immutable() -> None:
    private = DeliveryTarget("private", 123)
    group   = DeliveryTarget("group", 456)

    assert (private.user_id, private.group_id) == (123, None)
    assert (group.user_id, group.group_id) == (None, 456)
    with pytest.raises(FrozenInstanceError):
        group.target_id = 789  # type: ignore[misc]
    for invalid in (0, -1, True):
        with pytest.raises((TypeError, ValueError)):
            DeliveryTarget("group", invalid)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        DeliveryTarget("broadcast", 1)


def test_plugin_principal_validates_identity_and_group_authority() -> None:
    principal = PluginPrincipal(
        kind       = "user",
        user_id    = 123,
        group_id   = 456,
        group_role = "admin",
    )

    assert principal.can_manage_group(456) is True
    assert principal.can_manage_group(True) is False
    assert principal.can_manage_group("456") is False  # type: ignore[arg-type]
    for invalid_id in (0, -1, True, "123"):
        with pytest.raises((TypeError, ValueError)):
            PluginPrincipal(kind="user", user_id=invalid_id)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        PluginPrincipal(kind="unknown")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        PluginPrincipal(kind="user", user_id=1, is_private=1)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="requires user_id"):
        PluginPrincipal(kind="user")
    with pytest.raises(ValueError, match="must not have group_id"):
        PluginPrincipal(kind="user", user_id=1, group_id=2, is_private=True)
    with pytest.raises(ValueError, match="must not carry user scope"):
        PluginPrincipal(kind="lifecycle", user_id=1)
    scheduled = PluginPrincipal(kind="scheduled_system", schedule_delivery="silent")
    assert scheduled.schedule_delivery == "silent"
    with pytest.raises(ValueError, match="only scheduled principals"):
        PluginPrincipal(kind="lifecycle", schedule_delivery="silent")
    with pytest.raises(ValueError, match="delivery mode"):
        PluginPrincipal(
            kind              = "scheduled_system",
            schedule_delivery = "plugin",  # type: ignore[arg-type]
        )
