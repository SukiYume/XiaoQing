# mypy: disable-error-code=attr-defined
"""Plugin schedule publication and callback execution."""

from __future__ import annotations

import asyncio
import functools
import logging
from typing import Any

from .app_support import _parse_group_ids
from .delivery import (
    DeliveryReceipt,
    attach_receipt,
)
from .interfaces import (
    DeliveryTarget,
)
from .models import PluginScheduleManifest
from .plugin_base import build_action, segments
from .plugin_execution import (
    PluginExecutionClosed,
    PluginExecutionTimeout,
    PluginExecutionUnavailable,
    call_plugin_callback,
    invoke_loaded_plugin,
)
from .scheduler import ScheduledJobSpec

logger = logging.getLogger(__name__)


class AppSchedulingMixin:
    def _reschedule(self, plugin_name: str) -> None:
        """Validate and transactionally replace plugin schedules."""
        if self._stopping:
            logger.debug("Skipping schedule update for %s while stopping", plugin_name)
            return
        if self._defer_plugin_schedule_updates and plugin_name != "startup":
            logger.debug("Deferring schedule update for %s during startup", plugin_name)
            return
        if plugin_name == "startup":
            prefix = "plugin."
            target_plugins = self.plugin_manager.schedule_definitions()
        else:
            prefix = f"plugin.{plugin_name}."
            loaded = self.plugin_manager.get(plugin_name)
            target_plugins = [loaded] if loaded else []

        jobs: list[ScheduledJobSpec] = []
        try:
            for loaded in target_plugins:
                if not loaded:
                    continue
                for raw_entry in loaded.definition.schedule:
                    entry = PluginScheduleManifest.model_validate(raw_entry)
                    if not entry.enabled:
                        logger.info(
                            "Manifest-disabled schedule skipped: plugin=%s id=%s",
                            loaded.definition.name,
                            entry.id or entry.handler,
                        )
                        continue
                    handler = getattr(loaded.module, entry.handler, None)
                    if not callable(handler):
                        raise ValueError(
                            f"plugin {loaded.definition.name!r} schedule handler "
                            f"{entry.handler!r} is missing or not callable"
                        )
                    group_ids = tuple(entry.group_ids) if entry.group_ids is not None else None
                    job_id = f"plugin.{loaded.definition.name}.{entry.id or entry.handler}"
                    jobs.append(
                        ScheduledJobSpec(
                            job_id=job_id,
                            func=functools.partial(
                                self._run_job,
                                handler,
                                loaded.definition.name,
                                group_ids,
                                loaded_plugin=loaded,
                            ),
                            cron=entry.cron,
                            description=entry.description,
                        )
                    )
        except Exception:
            if plugin_name != "startup":
                self.scheduler.replace_prefix(prefix, [])
            raise

        self.scheduler.replace_prefix(prefix, jobs)

    async def _run_job(
        self,
        handler,
        plugin_name: str,
        group_ids: tuple[int, ...] | list[int] | None = None,
        *,
        loaded_plugin: Any | None = None,
    ) -> None:
        """执行定时任务"""
        if self._stopping:
            logger.debug("Scheduled job skipped while stopping: %s", plugin_name)
            return
        raw_target_groups = (
            group_ids if group_ids is not None else list(self.config.get("default_group_ids", []))
        )
        try:
            parsed_target_groups = _parse_group_ids(raw_target_groups)
        except (TypeError, ValueError) as exc:
            logger.error(
                "Scheduled job skipped for %s because group IDs are invalid: %s",
                plugin_name,
                exc,
            )
            return
        delivery_targets = tuple(DeliveryTarget("group", value) for value in parsed_target_groups)
        principal = self.identity_service.issue(
            kind="scheduled_system",
            is_private=False,
            delivery_targets=delivery_targets,
        )
        context = self.plugin_manager.build_context(
            plugin_name,
            principal=principal,
        )
        try:
            loaded = (
                loaded_plugin if loaded_plugin is not None else self.plugin_manager.get(plugin_name)
            )

            async def run_job_handler() -> Any:
                return await call_plugin_callback(handler, context)

            result = await invoke_loaded_plugin(loaded, run_job_handler)

            receipt = getattr(result, "delivery_receipt", None)
            segs = segments(result)
            if not segs:
                if isinstance(receipt, DeliveryReceipt):
                    await receipt.record(False)
                return

            actions: list[dict[str, Any]] = []
            for target in principal.delivery_targets:
                action = build_action(segs, target.user_id, target.group_id)
                if action:
                    actions.append(self._tag_action_source(action, plugin_name))

            if not actions:
                if isinstance(receipt, DeliveryReceipt):
                    await receipt.record(False)
                return

            if isinstance(receipt, DeliveryReceipt):
                receipt.add_expected_actions(len(actions) - 1)
            for action in actions:
                if isinstance(receipt, DeliveryReceipt):
                    action = attach_receipt(action, receipt)
                # 使用统一的 _send_action 方法（优先 WS，备选 HTTP）
                await self._send_action(action)

        except asyncio.CancelledError:
            logger.info("Scheduled job cancelled during shutdown: %s", plugin_name)
        except (PluginExecutionClosed, PluginExecutionTimeout, PluginExecutionUnavailable):
            logger.debug("Scheduled job skipped during plugin unload: %s", plugin_name)
        except Exception as exc:
            logger.exception("Scheduled job failed: %s", exc)
