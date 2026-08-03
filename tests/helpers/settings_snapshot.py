"""Test builders for the public atomic plugin-settings contract."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from core.interfaces import PluginSettingsSnapshot

T = TypeVar("T")


def settings_snapshot(
    *,
    config: Mapping[str, Any] | None = None,
    secrets: Mapping[str, Any] | None = None,
    revision: int = 0,
) -> PluginSettingsSnapshot:
    return PluginSettingsSnapshot(
        config={} if config is None else config,
        secrets={} if secrets is None else secrets,
        revision=revision,
    )


def with_settings_reader(context: T, *, revision: int = 0) -> T:
    """Attach a reader that reflects the fixture's current config and secrets."""

    def read_settings() -> PluginSettingsSnapshot:
        config = getattr(context, "config", {})
        secrets = getattr(context, "secrets", {})
        return settings_snapshot(
            config=config if isinstance(config, Mapping) else {},
            secrets=secrets if isinstance(secrets, Mapping) else {},
            revision=revision,
        )

    context.get_settings_snapshot = read_settings
    return context


__all__ = ["settings_snapshot", "with_settings_reader"]
