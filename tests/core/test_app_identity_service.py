# 验证应用主体签发、管理员权限与可信配置代的关联。
"""Focused tests for the application identity service boundary."""

from __future__ import annotations

from core.app_identity import AppIdentityService


def test_admin_replacement_is_atomic_and_keeps_the_live_set_identity() -> None:
    service  = AppIdentityService()
    live_ids = service.admin_ids

    service.load_admins({"admin_user_ids": [123, "456"]})
    assert live_ids == {123, 456}

    service.load_admins({"admin_user_ids": [789, "invalid"]})
    assert service.admin_ids is live_ids
    assert live_ids == set()


def test_user_principal_role_requires_a_matching_sender() -> None:
    service = AppIdentityService()
    service.load_admins({"admin_user_ids": [123]})

    principal = service.issue_user_principal(
        {"sender": {"user_id": 123, "role": "OWNER"}},
        user_id    = 123,
        group_id   = 456,
        is_private = False,
    )
    spoofed = service.issue_user_principal(
        {"sender": {"user_id": 999, "role": "owner"}},
        user_id    = 123,
        group_id   = 456,
        is_private = False,
    )

    assert principal.is_bot_admin is True
    assert principal.group_role == "owner"
    assert spoofed.group_role == "unknown"
    assert service.owns(principal) is True
    assert service.owns(spoofed) is True


def test_principals_are_owned_by_exactly_one_service_instance() -> None:
    first  = AppIdentityService()
    second = AppIdentityService()
    principal = first.issue(kind="lifecycle")

    assert first.owns(principal) is True
    assert second.owns(principal) is False
