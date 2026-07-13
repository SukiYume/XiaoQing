"""
Jupyter 内核管理器
"""

import asyncio
import base64
import inspect
import logging
import re
import struct
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.plugin_base import ensure_dir

from .jupyter_config import (
    AUTO_SHUTDOWN_TIMEOUT,
    CHECK_INTERVAL,
    DEFAULT_TIMEOUT,
    MAX_IMAGE_BYTES,
    MAX_IMAGE_PIXELS,
    MAX_IMAGES,
    MAX_OUTPUT_BYTES,
)
from .jupyter_models import ExecutionResult

logger = logging.getLogger(__name__)
AUDIT_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}\Z")
AUDIT_LABEL_RE = re.compile(r"[a-z][a-z0-9_.:-]{0,95}\Z")
ERROR_TYPE_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,95}\Z")


def _safe_audit_id(value: object) -> str:
    candidate = value if isinstance(value, str) else ""
    return candidate if AUDIT_ID_RE.fullmatch(candidate) else "-"


def _safe_audit_label(value: str) -> str:
    return value if AUDIT_LABEL_RE.fullmatch(value) else "unknown"


def _audit_error_type(exc: BaseException | None, fallback: str = "-") -> str:
    candidate = type(exc).__name__ if exc is not None else fallback
    return candidate if ERROR_TYPE_RE.fullmatch(candidate) else "Exception"


def _log_manager_audit(
    operation: str,
    *,
    status: str,
    audit_id: str | None = None,
    exc: BaseException | None = None,
    error_type: str = "-",
    level: int = logging.INFO,
) -> None:
    safe_error_type = _audit_error_type(exc, error_type)
    selected_level = logging.ERROR if exc is not None else level
    logger.log(
        selected_level,
        "sensitive_audit operation=%s job_id=%s status=%s error_type=%s "
        "payload_kind=none payload_length=0 payload_bytes=0 payload_fingerprint=-",
        _safe_audit_label(operation),
        _safe_audit_id(audit_id),
        _safe_audit_label(status),
        safe_error_type,
    )


# 全局变量保存导入状态
JUPYTER_AVAILABLE = False
IMPORT_ERROR = None
KernelManager = None
AsyncKernelManager = None


@dataclass
class KernelCleanupReport:
    """Best-effort kernel cleanup result retained for diagnostics and tests."""

    context: str
    channels_stopped: bool | None = None
    shutdown_succeeded: bool | None = None
    resources_cleaned: bool | None = None
    fallback_methods: list[str] = field(default_factory=list)
    fallback_succeeded: bool | None = None
    alive_after: bool | None = None
    orphan_confirmed_absent: bool = True
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        error_text = ", ".join(self.errors) if self.errors else "none"
        fallbacks = ",".join(self.fallback_methods) if self.fallback_methods else "none"
        return (
            f"context={self.context}; channels={self.channels_stopped}; "
            f"shutdown={self.shutdown_succeeded}; resources={self.resources_cleaned}; "
            f"fallback={self.fallback_succeeded}({fallbacks}); "
            f"alive_after={self.alive_after}; "
            f"orphan_absent={self.orphan_confirmed_absent}; errors={error_text}"
        )


def lazy_import_jupyter():
    """惰性导入 jupyter_client"""
    global JUPYTER_AVAILABLE, IMPORT_ERROR, KernelManager, AsyncKernelManager
    if JUPYTER_AVAILABLE:
        return

    try:
        from jupyter_client import KernelManager as KM

        # 尝试直接导入（适用于新版）
        try:
            from jupyter_client import AsyncKernelManager as AKM
        except ImportError:
            # 尝试从 asynchronous 子模块导入（适用于旧版）
            from jupyter_client.asynchronous import AsyncKernelManager as AKM

        global KernelManager, AsyncKernelManager
        KernelManager = KM
        AsyncKernelManager = AKM
        JUPYTER_AVAILABLE = True
    except ImportError as e:
        JUPYTER_AVAILABLE = False
        IMPORT_ERROR = str(e)
        KernelManager = None
        AsyncKernelManager = None


class JupyterKernelManager:
    _instances: dict[str, "JupyterKernelManager"] = {}
    _quarantined_instances: set["JupyterKernelManager"] = set()
    _instances_lock = threading.Lock()

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.figures_dir = data_dir / "figures"
        ensure_dir(self.figures_dir)

        self._km: Any | None = None
        self._kc: Any | None = None
        self._started_at: float | None = None
        self._execution_count = 0
        self._execute_lock = asyncio.Lock()
        self._lifecycle_lock = threading.RLock()
        self._broken = False
        self._last_cleanup_report: KernelCleanupReport | None = None
        self._orphan_km: Any | None = None
        self._orphan_kc: Any | None = None

        # 自动关闭相关
        self._last_activity = 0.0
        self._shutdown_task: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    @staticmethod
    def _instance_key(data_dir: Path, owner_key: str) -> str:
        resolved_dir = str(Path(data_dir).resolve())
        return f"{owner_key or 'global'}::{resolved_dir}"

    @classmethod
    def get_instance(cls, data_dir: Path, owner_key: str = "global") -> "JupyterKernelManager":
        key = cls._instance_key(data_dir, str(owner_key or "global"))
        with cls._instances_lock:
            instance = cls._instances.get(key)
            if instance is None or instance._broken:
                instance = cls(data_dir)
                cls._instances[key] = instance
            return instance

    @classmethod
    def _isolate_instance(cls, instance: "JupyterKernelManager") -> None:
        with cls._instances_lock:
            stale_keys = [key for key, value in cls._instances.items() if value is instance]
            for key in stale_keys:
                cls._instances.pop(key, None)
            cls._quarantined_instances.add(instance)

    @classmethod
    def shutdown_all(cls) -> None:
        with cls._instances_lock:
            instances = list(dict.fromkeys([*cls._instances.values(), *cls._quarantined_instances]))
            cls._instances.clear()
            cls._quarantined_instances.clear()
        for instance in instances:
            try:
                instance.shutdown_kernel()
            except Exception as exc:
                _log_manager_audit(
                    "jupyter.kernel.shutdown_all",
                    status="error",
                    exc=exc,
                )

    @classmethod
    async def shutdown_all_async(cls) -> None:
        with cls._instances_lock:
            instances = list(dict.fromkeys([*cls._instances.values(), *cls._quarantined_instances]))
            cls._instances.clear()
            cls._quarantined_instances.clear()
        tasks = [instance._shutdown_task for instance in instances if instance._shutdown_task]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(
            *(asyncio.to_thread(instance.shutdown_kernel, False) for instance in instances),
            return_exceptions=True,
        )

    @property
    def is_running(self) -> bool:
        """检查内核是否运行中"""
        with self._lifecycle_lock:
            if self._broken or self._km is None:
                return False
            alive = self._kernel_alive(self._km, report=None, stage="status")
            return alive is True

    @property
    def broken(self) -> bool:
        return self._broken

    @property
    def last_cleanup_report(self) -> KernelCleanupReport | None:
        return self._last_cleanup_report

    @staticmethod
    def _resolve_awaitable(value: Any, *, timeout: float = 5.0) -> Any:
        if not inspect.isawaitable(value):
            return value
        result: list[Any] = []
        errors: list[BaseException] = []

        def run() -> None:
            try:
                result.append(asyncio.run(value))
            except BaseException as exc:  # noqa: BLE001 - propagate from helper thread.
                errors.append(exc)

        worker = threading.Thread(target=run, name="jupyter-cleanup-awaitable", daemon=True)
        worker.start()
        worker.join(timeout=max(0.1, timeout))
        if worker.is_alive():
            raise TimeoutError("async cleanup step did not finish before its deadline")
        if errors:
            raise errors[0]
        return result[0] if result else None

    @classmethod
    def _invoke_cleanup(cls, callback: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            parameters = inspect.signature(callback).parameters.values()
            accepts_kwargs = any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters
            )
            accepted_names = {parameter.name for parameter in parameters}
            call_kwargs = (
                kwargs
                if accepts_kwargs
                else {key: value for key, value in kwargs.items() if key in accepted_names}
            )
        except (TypeError, ValueError):
            call_kwargs = kwargs
        value = callback(*args, **call_kwargs)
        return cls._resolve_awaitable(value)

    @classmethod
    def _kernel_alive(
        cls,
        km: Any,
        *,
        report: KernelCleanupReport | None,
        stage: str,
    ) -> bool | None:
        if km is None:
            return False
        is_alive = getattr(km, "is_alive", None)
        if not callable(is_alive):
            return None
        try:
            return bool(cls._resolve_awaitable(is_alive()))
        except BaseException as exc:  # noqa: BLE001 - cleanup must continue.
            if report is not None:
                report.errors.append(f"{stage}.is_alive: {exc}")
            return None

    @classmethod
    def _wait_for_kernel_exit(
        cls,
        km: Any,
        report: KernelCleanupReport,
        *,
        stage: str,
        timeout: float = 2.0,
    ) -> bool | None:
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            alive = cls._kernel_alive(km, report=report, stage=stage)
            if alive is not True:
                return alive
            if time.monotonic() >= deadline:
                return True
            time.sleep(0.05)

    @classmethod
    def _force_kill_kernel(cls, km: Any, report: KernelCleanupReport) -> bool:
        candidates: list[tuple[str, Any, dict[str, Any]]] = []
        provisioner = getattr(km, "provisioner", None)
        provisioner_kill = getattr(provisioner, "kill", None)
        if callable(provisioner_kill):
            candidates.append(("provisioner.kill", provisioner_kill, {"restart": False}))
        private_kill = getattr(km, "_kill_kernel", None)
        if callable(private_kill):
            candidates.append(("_kill_kernel", private_kill, {}))
        process = getattr(provisioner, "process", None) or getattr(km, "process", None)
        process_kill = getattr(process, "kill", None)
        if callable(process_kill):
            candidates.append(("process.kill", process_kill, {}))

        for name, callback, kwargs in candidates:
            report.fallback_methods.append(name)
            try:
                cls._invoke_cleanup(callback, **kwargs)
                report.fallback_succeeded = True
            except BaseException as exc:  # noqa: BLE001 - try the next fallback.
                report.errors.append(f"{name}: {exc}")
                continue
            alive = cls._wait_for_kernel_exit(km, report, stage=name)
            if alive is False:
                report.alive_after = False
                return True
        return False

    @classmethod
    def _cleanup_kernel_resources(
        cls,
        km: Any,
        kc: Any,
        *,
        context: str,
    ) -> KernelCleanupReport:
        report = KernelCleanupReport(context=context)
        if kc is not None:
            stop_channels = getattr(kc, "stop_channels", None)
            if callable(stop_channels):
                try:
                    cls._invoke_cleanup(stop_channels)
                    report.channels_stopped = True
                except BaseException as exc:  # noqa: BLE001 - continue to kernel kill.
                    report.channels_stopped = False
                    report.errors.append(f"stop_channels: {exc}")

        if km is None:
            report.alive_after = False
            report.orphan_confirmed_absent = True
            return report

        shutdown = getattr(km, "shutdown_kernel", None)
        if callable(shutdown):
            try:
                cls._invoke_cleanup(shutdown, now=True)
                report.shutdown_succeeded = True
            except BaseException as exc:  # noqa: BLE001 - cleanup must continue.
                report.shutdown_succeeded = False
                report.errors.append(f"shutdown_kernel: {exc}")
        else:
            report.shutdown_succeeded = False
            report.errors.append("shutdown_kernel: unavailable")

        cleanup_resources = getattr(km, "cleanup_resources", None)
        if callable(cleanup_resources):
            try:
                cls._invoke_cleanup(cleanup_resources, restart=False)
                report.resources_cleaned = True
            except BaseException as exc:  # noqa: BLE001 - fallback kill still runs.
                report.resources_cleaned = False
                report.errors.append(f"cleanup_resources: {exc}")

        alive = cls._kernel_alive(km, report=report, stage="post_shutdown")
        report.alive_after = alive
        if alive is False or (alive is None and report.shutdown_succeeded is True):
            report.orphan_confirmed_absent = True
            return report

        report.fallback_succeeded = False
        killed = cls._force_kill_kernel(km, report)
        if killed:
            if callable(cleanup_resources) and report.resources_cleaned is False:
                try:
                    cls._invoke_cleanup(cleanup_resources, restart=False)
                    report.resources_cleaned = True
                except BaseException as exc:  # noqa: BLE001 - termination is confirmed.
                    report.errors.append(f"cleanup_resources_after_kill: {exc}")
            report.orphan_confirmed_absent = True
            return report

        alive = cls._kernel_alive(km, report=report, stage="post_fallback")
        report.alive_after = alive
        report.orphan_confirmed_absent = alive is False
        return report

    def _mark_broken(self, km: Any, kc: Any, report: KernelCleanupReport) -> None:
        self._broken = True
        self._orphan_km = km
        self._orphan_kc = kc
        self._last_cleanup_report = report
        self._isolate_instance(self)
        _log_manager_audit(
            "jupyter.kernel.cleanup",
            status="quarantined",
            error_type="KernelCleanupIncomplete",
            level=logging.ERROR,
        )

    async def _check_idleness_loop(self):
        """后台任务：检查空闲时间并自动关闭"""
        while self.is_running:
            await asyncio.sleep(CHECK_INTERVAL)
            if not self.is_running:
                break

            idle_time = time.time() - self._last_activity
            if idle_time > AUTO_SHUTDOWN_TIMEOUT:
                _log_manager_audit(
                    "jupyter.kernel.idle_shutdown",
                    status="started",
                )
                await asyncio.to_thread(self.shutdown_kernel, False)
                break

    def ensure_idle_monitor(self) -> None:
        self._loop = asyncio.get_running_loop()
        if self._shutdown_task is None or self._shutdown_task.done():
            self._shutdown_task = asyncio.create_task(self._check_idleness_loop())

    def get_status(self) -> dict[str, Any]:
        """获取内核状态"""
        if not self.is_running:
            return {"running": False, "message": "内核未启动"}

        uptime = time.time() - self._started_at if self._started_at else 0
        return {
            "running": True,
            "kernel_name": self._km.kernel_name,
            "uptime": uptime,
            "execution_count": self._execution_count,
            "message": f"内核运行中 (已执行 {self._execution_count} 次, 运行 {uptime:.0f}s)",
        }

    def start_kernel(self, kernel_name: str = "python3") -> bool:
        """启动内核"""
        with self._lifecycle_lock:
            if self._broken:
                raise RuntimeError("Jupyter manager 已隔离，必须重新获取实例")

            lazy_import_jupyter()
            if not JUPYTER_AVAILABLE:
                raise ImportError(f"Jupyter 依赖未加载: {IMPORT_ERROR}")
            if self.is_running:
                return True

            if self._km is not None or self._kc is not None:
                stale_km, stale_kc = self._km, self._kc
                self._km = None
                self._kc = None
                stale_report = self._cleanup_kernel_resources(
                    stale_km,
                    stale_kc,
                    context="stale-before-start",
                )
                self._last_cleanup_report = stale_report
                if not stale_report.orphan_confirmed_absent:
                    self._mark_broken(stale_km, stale_kc, stale_report)
                    raise RuntimeError(
                        f"旧 Jupyter kernel 无法确认退出；实例已隔离。{stale_report.summary()}"
                    )

            km: Any | None = None
            kc: Any | None = None
            try:
                km = KernelManager(kernel_name=kernel_name)
                km.start_kernel()
                kc = km.client()
                kc.start_channels()
                kc.wait_for_ready(timeout=30)
            except Exception as exc:
                report = self._cleanup_kernel_resources(
                    km,
                    kc,
                    context="start-failure",
                )
                self._last_cleanup_report = report
                self._km = None
                self._kc = None
                self._started_at = None
                self._execution_count = 0
                if not report.orphan_confirmed_absent:
                    self._mark_broken(km, kc, report)
                raise RuntimeError(f"启动内核失败: {exc}; cleanup: {report.summary()}") from exc

            # Publish the manager/client pair only after the ready handshake.
            self._km = km
            self._kc = kc
            self._orphan_km = None
            self._orphan_kc = None
            self._started_at = time.time()
            self._execution_count = 0
            self._last_activity = time.time()
            self._init_matplotlib()
            return True

    def _init_matplotlib(self) -> None:
        """初始化 matplotlib 内联后端"""
        init_code = """
import warnings
warnings.filterwarnings('ignore')

try:
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    plt.ioff()

    try:
        from IPython import get_ipython
        ipython = get_ipython()
        if ipython:
            ipython.run_line_magic('matplotlib', 'inline')
    except:
        pass
except ImportError:
    pass
"""
        try:
            msg_id = self._kc.execute(init_code)
            deadline = time.time() + 1.0
            while time.time() < deadline:
                try:
                    msg = self._kc.get_iopub_msg(timeout=0.2)
                except TimeoutError:
                    continue
                except Exception:
                    break
                if msg.get("msg_type") != "status":
                    continue
                content = msg.get("content", {})
                parent_id = msg.get("parent_header", {}).get("msg_id")
                if content.get("execution_state") == "idle" and parent_id == msg_id:
                    break
        except Exception:
            pass

    def shutdown_kernel(self, cancel_idle_task: bool = True) -> None:
        """关闭内核"""
        with self._lifecycle_lock:
            if cancel_idle_task and self._shutdown_task and not self._shutdown_task.done():
                task = self._shutdown_task
                if self._loop and self._loop.is_running():
                    self._loop.call_soon_threadsafe(task.cancel)
                else:
                    task.cancel()
            self._shutdown_task = None

            km = self._km if self._km is not None else self._orphan_km
            kc = self._kc if self._kc is not None else self._orphan_kc
            self._km = None
            self._kc = None
            report = self._cleanup_kernel_resources(km, kc, context="shutdown")
            self._last_cleanup_report = report
            self._started_at = None
            self._execution_count = 0
            if report.orphan_confirmed_absent:
                self._orphan_km = None
                self._orphan_kc = None
                with self._instances_lock:
                    self._quarantined_instances.discard(self)
                return
            self._mark_broken(km, kc, report)
            raise RuntimeError(f"无法确认 Jupyter kernel 已退出: {report.summary()}")

    def restart_kernel(self) -> None:
        """重启内核"""
        with self._lifecycle_lock:
            kernel_name = str(getattr(self._km, "kernel_name", "python3") or "python3")
            if self._km is not None or self._orphan_km is not None:
                self.shutdown_kernel(cancel_idle_task=False)
            self.start_kernel(kernel_name)

    def interrupt_kernel(self) -> None:
        """中断当前执行中的代码，避免超时后继续占用内核状态。"""
        with self._lifecycle_lock:
            if self._km and self.is_running:
                self._km.interrupt_kernel()

    @staticmethod
    def _is_matching_idle(message: Any, msg_id: str | None) -> bool:
        return bool(
            msg_id
            and isinstance(message, dict)
            and message.get("msg_type") == "status"
            and message.get("content", {}).get("execution_state") == "idle"
            and message.get("parent_header", {}).get("msg_id") == msg_id
        )

    @staticmethod
    async def _wait_task_despite_cancellation(task: asyncio.Task[Any]) -> Any:
        while not task.done():
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
        return task.result()

    async def _cancel_execution_cleanup(
        self,
        *,
        pending_task: asyncio.Task[Any] | None,
        pending_kind: str | None,
        msg_id: str | None,
        recovery_task: asyncio.Task[Any] | None,
        kernel_was_running: bool,
        audit_id: str,
    ) -> None:
        if recovery_task is not None:
            try:
                await self._wait_task_despite_cancellation(recovery_task)
            except Exception as exc:
                _log_manager_audit(
                    "jupyter.cancel.recovery",
                    status="error",
                    audit_id=audit_id,
                    exc=exc,
                )
            return

        pending_value: Any = None
        pending_failed = False
        if pending_task is not None:
            try:
                pending_value = await self._wait_task_despite_cancellation(pending_task)
            except Exception as exc:
                pending_failed = True
                _log_manager_audit(
                    "jupyter.cancel.pending_operation",
                    status="error",
                    audit_id=audit_id,
                    exc=exc,
                )

        if pending_kind == "start":
            if not kernel_was_running and self.is_running:
                try:
                    await asyncio.to_thread(self.shutdown_kernel)
                except Exception as exc:
                    _log_manager_audit(
                        "jupyter.cancel.new_kernel_shutdown",
                        status="quarantined",
                        audit_id=audit_id,
                        exc=exc,
                    )
            return
        if pending_kind == "submit" and not pending_failed:
            msg_id = str(pending_value)
        if (
            pending_kind == "read"
            and not pending_failed
            and self._is_matching_idle(
                pending_value,
                msg_id,
            )
        ):
            return
        if not msg_id:
            if not kernel_was_running and self.is_running:
                try:
                    await asyncio.to_thread(self.shutdown_kernel)
                except Exception as exc:
                    _log_manager_audit(
                        "jupyter.cancel.new_kernel_cleanup",
                        status="quarantined",
                        audit_id=audit_id,
                        exc=exc,
                    )
            return

        try:
            if pending_kind != "interrupt":
                await asyncio.to_thread(self.interrupt_kernel)
            await self._recover_after_interrupt(msg_id, audit_id=audit_id)
        except Exception as exc:
            _log_manager_audit(
                "jupyter.cancel.cleanup",
                status="error",
                audit_id=audit_id,
                exc=exc,
            )

    async def execute(
        self,
        code: str,
        timeout: float = DEFAULT_TIMEOUT,
        *,
        audit_id: str | None = None,
    ) -> ExecutionResult:
        """执行代码，并在外部取消返回前收敛底层 kernel 工作。"""
        async with self._execute_lock:
            audit_id = _safe_audit_id(audit_id)
            if audit_id == "-":
                audit_id = uuid.uuid4().hex
            kernel_was_running = self.is_running
            pending_task: asyncio.Task[Any] | None = None
            pending_kind: str | None = None
            recovery_task: asyncio.Task[Any] | None = None
            msg_id: str | None = None
            result = ExecutionResult()
            start_time = time.time()
            deadline = start_time + timeout
            image_count = 0
            text_bytes = 0
            execution_dir = self.figures_dir / uuid.uuid4().hex

            def append_text(current: str, value: Any) -> tuple[str, bool]:
                nonlocal text_bytes
                encoded = str(value or "").encode("utf-8", errors="replace")
                remaining = MAX_OUTPUT_BYTES - text_bytes
                if remaining <= 0:
                    return current, True
                kept = encoded[:remaining].decode("utf-8", errors="ignore")
                text_bytes += len(kept.encode("utf-8"))
                return current + kept, len(encoded) > remaining

            async def interrupt_and_recover() -> None:
                nonlocal pending_task, pending_kind, recovery_task
                pending_kind = "interrupt"
                pending_task = asyncio.create_task(
                    asyncio.to_thread(self.interrupt_kernel),
                    name="jupyter-io-interrupt",
                )
                await asyncio.shield(pending_task)
                pending_task = None
                pending_kind = None
                recovery_task = asyncio.create_task(
                    self._recover_after_interrupt(str(msg_id), audit_id=audit_id),
                    name="jupyter-cancel-recovery",
                )
                await asyncio.shield(recovery_task)
                recovery_task = None

            try:
                if not self.is_running:
                    pending_kind = "start"
                    pending_task = asyncio.create_task(
                        asyncio.to_thread(self.start_kernel),
                        name="jupyter-io-start",
                    )
                    await asyncio.shield(pending_task)
                    pending_task = None
                    pending_kind = None

                self._last_activity = time.time()
                self.ensure_idle_monitor()
                start_time = time.time()
                deadline = start_time + timeout

                pending_kind = "submit"
                pending_task = asyncio.create_task(
                    asyncio.to_thread(self._kc.execute, code),
                    name="jupyter-io-submit",
                )
                msg_id = str(await asyncio.shield(pending_task))
                pending_task = None
                pending_kind = None

                while True:
                    remaining = deadline - time.time()
                    if remaining <= 0:
                        result.error = f"执行超时 ({timeout}s)"
                        result.success = False
                        await interrupt_and_recover()
                        break

                    pending_kind = "read"
                    pending_task = asyncio.create_task(
                        asyncio.to_thread(
                            self._kc.get_iopub_msg,
                            min(remaining, 0.25),
                        ),
                        name="jupyter-io-read",
                    )
                    try:
                        msg = await asyncio.shield(pending_task)
                    except TimeoutError:
                        pending_task = None
                        pending_kind = None
                        continue
                    pending_task = None
                    pending_kind = None

                    parent_id = msg.get("parent_header", {}).get("msg_id")
                    if parent_id != msg_id:
                        continue

                    msg_type = msg["msg_type"]
                    content = msg.get("content", {})

                    if msg_type == "stream":
                        if content.get("name") == "stdout":
                            result.stdout, exceeded = append_text(
                                result.stdout,
                                content.get("text", ""),
                            )
                        elif content.get("name") == "stderr":
                            result.stderr, exceeded = append_text(
                                result.stderr,
                                content.get("text", ""),
                            )
                        else:
                            exceeded = False
                        if exceeded:
                            result.error = f"输出超过 {MAX_OUTPUT_BYTES} 字节安全上限，已中断内核"
                            result.success = False
                            await interrupt_and_recover()
                            break

                    elif msg_type == "execute_result":
                        data = content.get("data", {})
                        if "image/png" in data and image_count < MAX_IMAGES:
                            img_path = self._save_image(
                                data["image/png"], image_count, execution_dir
                            )
                            if img_path:
                                result.images.append(img_path)
                                image_count += 1
                        if "text/plain" in data:
                            result.result, exceeded = append_text("", data["text/plain"])
                            if exceeded:
                                result.error = (
                                    f"输出超过 {MAX_OUTPUT_BYTES} 字节安全上限，已中断内核"
                                )
                                result.success = False
                                await interrupt_and_recover()
                                break

                    elif msg_type == "display_data":
                        data = content.get("data", {})
                        if "image/png" in data and image_count < MAX_IMAGES:
                            img_path = self._save_image(
                                data["image/png"], image_count, execution_dir
                            )
                            if img_path:
                                result.images.append(img_path)
                                image_count += 1

                    elif msg_type == "error":
                        traceback = content.get("traceback", [])
                        cleaned = [re.sub(r"\x1b\[[0-9;]*m", "", line) for line in traceback]
                        result.error, _exceeded = append_text("", "\n".join(cleaned))
                        result.success = False

                    elif msg_type == "status" and content.get("execution_state") == "idle":
                        break

                self._execution_count += 1
            except asyncio.CancelledError:
                cleanup_task = asyncio.create_task(
                    self._cancel_execution_cleanup(
                        pending_task=pending_task,
                        pending_kind=pending_kind,
                        msg_id=msg_id,
                        recovery_task=recovery_task,
                        kernel_was_running=kernel_was_running,
                        audit_id=audit_id,
                    ),
                    name="jupyter-cancel-recovery",
                )
                while not cleanup_task.done():
                    try:
                        await asyncio.shield(cleanup_task)
                    except asyncio.CancelledError:
                        continue
                try:
                    cleanup_task.result()
                except Exception as exc:
                    _log_manager_audit(
                        "jupyter.cancel.cleanup_task",
                        status="error",
                        audit_id=audit_id,
                        exc=exc,
                    )
                self._last_activity = time.time()
                raise
            except Exception as exc:
                _log_manager_audit(
                    "jupyter.execute",
                    status="error",
                    audit_id=audit_id,
                    exc=exc,
                )
                result.error = f"执行异常: {exc}"
                result.success = False

            self._last_activity = time.time()
            result.execution_time = time.time() - start_time
            return result

    async def _recover_after_interrupt(
        self,
        msg_id: str,
        *,
        audit_id: str | None = None,
    ) -> str:
        deadline = time.time() + 2.0
        while time.time() < deadline:
            try:
                msg = await asyncio.to_thread(self._kc.get_iopub_msg, 0.2)
            except TimeoutError:
                continue
            except Exception:
                break
            if (
                msg.get("msg_type") == "status"
                and msg.get("content", {}).get("execution_state") == "idle"
                and msg.get("parent_header", {}).get("msg_id") == msg_id
            ):
                return "idle"
        try:
            await asyncio.to_thread(self.restart_kernel)
            return "restarted"
        except Exception as exc:
            _log_manager_audit(
                "jupyter.interrupt.restart",
                status="error",
                audit_id=audit_id,
                exc=exc,
            )
            try:
                await asyncio.to_thread(self.shutdown_kernel)
                return "shutdown"
            except Exception as exc:
                _log_manager_audit(
                    "jupyter.interrupt.shutdown",
                    status="quarantined",
                    audit_id=audit_id,
                    exc=exc,
                )
                return "quarantined"

    def _save_image(self, base64_data: str, index: int, execution_dir: Path) -> Path | None:
        """保存 base64 图片到文件"""
        try:
            if len(base64_data) > ((MAX_IMAGE_BYTES + 2) // 3) * 4:
                return None
            image_data = base64.b64decode(base64_data, validate=True)
            if len(image_data) > MAX_IMAGE_BYTES:
                return None
            if len(image_data) < 24 or image_data[:8] != b"\x89PNG\r\n\x1a\n":
                return None
            width, height = struct.unpack(">II", image_data[16:24])
            if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
                return None
            ensure_dir(execution_dir)
            filename = f"output_{index}.png"
            filepath = execution_dir / filename
            temp_path = execution_dir / f".{filename}.tmp"
            temp_path.write_bytes(image_data)
            temp_path.replace(filepath)
            return filepath
        except Exception:
            return None
