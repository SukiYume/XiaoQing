"""插件定时任务：按 manifest 发布到调度器，并按投递模式（广播/定向/静默）执行回调。"""

from __future__ import annotations

import asyncio
import functools
import logging
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from .app_support import _parse_positive_ids
from .delivery import (
    DeliveryReceipt,
    ScheduledDelivery,
    attach_receipt,
)
from .interfaces import (
    DeliveryTarget,
    PluginPrincipal,
    ScheduleDeliveryMode,
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

if TYPE_CHECKING:
    from .app_identity import AppIdentityService
    from .plugin_manager import PluginManager
    from .plugin_manager_support import LoadedPlugin
    from .scheduler import SchedulerManager

logger = logging.getLogger(__name__)


def resolve_schedule_targets(
    config: Mapping[str, Any],
    plugin_name: str,
    schedule_id: str,
    manifest_group_ids: Sequence[int] | None,
) -> tuple[tuple[int, ...], str]:
    """One schedule's target groups and the config field they came from.

    Most specific first: the deployment's
    ``config.plugins.<plugin>.schedules.<schedule id>.group_ids``, then the manifest's
    ``group_ids``, then ``default_group_ids``. Real group IDs therefore live only in the
    private config; manifests in the repository carry none. An explicit empty list in the
    deployment config is an override too: the schedule runs without group targets. Invalid
    IDs raise ``TypeError`` / ``ValueError``.
    """

    plugins  = config.get("plugins")
    section  = plugins.get(plugin_name) if isinstance(plugins, Mapping) else None
    schedule = section.get("schedules") if isinstance(section, Mapping) else None
    entry    = schedule.get(schedule_id) if schedule_id and isinstance(schedule, Mapping) else None
    if isinstance(entry, Mapping) and "group_ids" in entry:
        field_name = f"plugins.{plugin_name}.schedules.{schedule_id}.group_ids"
        raw_groups = entry["group_ids"]
    elif manifest_group_ids is not None:
        field_name = f"{plugin_name} plugin.json schedule {schedule_id} group_ids"
        raw_groups = list(manifest_group_ids)
    else:
        field_name = "default_group_ids"
        raw_groups = list(config.get("default_group_ids", []))
    return _parse_positive_ids(raw_groups, field_name=field_name), field_name


class AppSchedulingMixin:
    # 由组合类实际提供的成员；这里只为类型检查声明契约，运行时不存在。
    if TYPE_CHECKING:
        _defer_plugin_schedule_updates: bool

        async def _send_action(
            self,
            action: dict[str, Any],
            wait_ws_seconds: float = 0.0,
        ) -> bool | None:
            raise NotImplementedError

        _stopping: bool

        @staticmethod
        def _tag_action_source(action: dict[str, Any], plugin_name: str) -> dict[str, Any]:
            raise NotImplementedError

        @property
        def config(self) -> Mapping[str, Any]:
            raise NotImplementedError

        identity_service: AppIdentityService

        plugin_manager: PluginManager

        scheduler: SchedulerManager

    def _reschedule(self, plugin_name: str) -> None:
        """Validate and transactionally replace plugin schedules."""
        if self._stopping:
            logger.debug("Skipping schedule update for %s while stopping", plugin_name)
            return
        if self._defer_plugin_schedule_updates and plugin_name != "startup":
            logger.debug("Deferring schedule update for %s during startup", plugin_name)
            return
        if plugin_name == "startup":
            prefix         = "plugin."
            target_plugins = self.plugin_manager.schedule_definitions()
        else:
            prefix         = f"plugin.{plugin_name}."
            loaded         = self.plugin_manager.get(plugin_name)
            target_plugins = [loaded] if loaded else []
        if self.config.get("enable_plugin_schedules", True) is False:
            # 隔离运行（例如 UAT）关闭全部插件定时任务：不触达真实账号，也不向生产群投递。
            logger.info("Plugin schedules disabled by config: %s", plugin_name)
            target_plugins = []

        jobs: list[ScheduledJobSpec] = []
        try:
            for loaded in target_plugins:
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
                    group_ids   = tuple(entry.group_ids) if entry.group_ids is not None else None
                    schedule_id = entry.id or entry.handler
                    job_id      = f"plugin.{loaded.definition.name}.{schedule_id}"
                    jobs.append(
                        ScheduledJobSpec(
                            job_id = job_id,
                            func   = functools.partial(
                                self._run_job,
                                handler,
                                loaded.definition.name,
                                group_ids,
                                delivery      = entry.delivery,
                                loaded_plugin = loaded,
                                schedule_id   = schedule_id,
                            ),
                            cron        = entry.cron,
                            description = entry.description,
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
        delivery: ScheduleDeliveryMode = "broadcast",
        loaded_plugin: LoadedPlugin,
        schedule_id: str = "",
    ) -> None:
        """Execute one schedule under the Core-owned delivery contract.

        Target groups resolve at run time through :func:`resolve_schedule_targets`.
        """
        if self._stopping:
            logger.debug("Scheduled job skipped while stopping: %s", plugin_name)
            return
        parsed_target_groups: tuple[int, ...] = ()
        if delivery != "silent":
            try:
                parsed_target_groups, _source = resolve_schedule_targets(
                    self.config, plugin_name, schedule_id, group_ids
                )
            except (TypeError, ValueError) as exc:
                logger.error(
                    "Scheduled job skipped for %s because group IDs are invalid: %s",
                    plugin_name,
                    exc,
                )
                return
        delivery_targets = tuple(DeliveryTarget("group", value) for value in parsed_target_groups)
        principal        = self.identity_service.issue(
            kind              = "scheduled_system",
            is_private        = False,
            delivery_targets  = delivery_targets,
            schedule_delivery = delivery,
        )
        context = self.plugin_manager.build_context(plugin_name, principal=principal)
        try:
            result = await invoke_loaded_plugin(
                loaded_plugin, functools.partial(call_plugin_callback, handler, context)
            )

            if delivery == "targeted":
                await self._deliver_targeted_schedule_result(result, plugin_name, principal)
                return

            receipt = getattr(result, "delivery_receipt", None)
            receipt = receipt if isinstance(receipt, DeliveryReceipt) else None
            if delivery == "silent" and result not in (None, []):
                logger.warning(
                    "Silent schedule result ignored: plugin=%s result_type=%s",
                    plugin_name,
                    type(result).__name__,
                )

            # 静默任务不投递；广播任务向每个目标群各发一条
            actions: list[dict[str, Any]] = []
            segs                          = segments(result) if delivery != "silent" else []
            for target in principal.delivery_targets if segs else ():
                action = build_action(segs, target.user_id, target.group_id)
                if action:
                    actions.append(self._tag_action_source(action, plugin_name))

            # 没有可发送内容时回执按“未投递”结算
            if not actions:
                if receipt is not None:
                    await receipt.record(False)
                return

            if receipt is not None:
                receipt.add_expected_actions(len(actions) - 1)
                actions = [attach_receipt(action, receipt) for action in actions]
            for action in actions:
                await self._send_action(action)

        except asyncio.CancelledError:
            logger.info("Scheduled job cancelled during shutdown: %s", plugin_name)
        except (PluginExecutionClosed, PluginExecutionTimeout, PluginExecutionUnavailable):
            logger.debug("Scheduled job skipped during plugin unload: %s", plugin_name)
        except Exception as exc:
            logger.exception("Scheduled job failed: %s", exc)

    async def _deliver_targeted_schedule_result(
        self,
        result: Any,
        plugin_name: str,
        principal: PluginPrincipal,
    ) -> None:
        """Validate and send target-specific results produced by one schedule."""

        if result is None or result == []:
            return
        if isinstance(result, ScheduledDelivery):
            deliveries = (result,)
        elif isinstance(result, (list, tuple)) and all(
            isinstance(item, ScheduledDelivery) for item in result
        ):
            deliveries = tuple(result)
        else:
            logger.error(
                "Targeted schedule returned an invalid result: plugin=%s result_type=%s",
                plugin_name,
                type(result).__name__,
            )
            return

        allowed_groups = {
            target.target_id for target in principal.delivery_targets if target.kind == "group"
        }
        attempted = 0
        try:
            for delivery in deliveries:
                attempted += 1
                if (
                    delivery.target.kind == "group"
                    and delivery.target.target_id not in allowed_groups
                ):
                    logger.warning(
                        "Targeted schedule group rejected by manifest policy: plugin=%s",
                        plugin_name,
                    )
                    if delivery.receipt is not None:
                        await delivery.receipt.record(False)
                    continue
                action = build_action(
                    list(delivery.message),
                    delivery.target.user_id,
                    delivery.target.group_id,
                )
                if action is None:
                    if delivery.receipt is not None:
                        await delivery.receipt.record(False)
                    continue
                action = self._tag_action_source(action, plugin_name)
                if delivery.receipt is not None:
                    action = attach_receipt(action, delivery.receipt)
                await self._send_action(action)
        finally:
            # 当前目标发送异常或取消时，后续目标确定没有尝试：逐个失败结算它们的回执。
            for skipped in deliveries[attempted:]:
                if skipped.receipt is not None:
                    await asyncio.shield(skipped.receipt.record(False))
