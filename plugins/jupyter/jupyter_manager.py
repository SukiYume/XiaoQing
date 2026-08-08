"""管理隔离 Jupyter 内核、输出预算、取消恢复和资源回收。"""

from __future__ import annotations

import asyncio
import base64
import binascii
import inspect
import logging
import math
import re
import shutil
import threading
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from queue import Empty as QueueEmpty
from typing import Any, ClassVar

from core.image_validation import (
    ImageValidationError,
    ImageValidationLimits,
    validate_image_bytes,
)

from .jupyter_audit import log_sensitive_audit, safe_audit_id
from .jupyter_config import (
    AUTO_SHUTDOWN_TIMEOUT,
    CHECK_INTERVAL,
    DEFAULT_TIMEOUT,
    MAX_EXECUTION_TIMEOUT,
    MAX_IMAGE_BYTES,
    MAX_IMAGE_PIXELS,
    MAX_IMAGES,
    MAX_KERNEL_INSTANCES,
    MAX_OUTPUT_BYTES,
    MAX_TOTAL_IMAGE_BYTES,
)
from .jupyter_models import ExecutionResult

logger = logging.getLogger(__name__)

_LEGACY_FIGURE_DIR_PATTERN = re.compile(r"[0-9a-f]{32}\Z")
_LEGACY_FIGURE_FILE_PATTERN = re.compile(r"output_[0-9]{10}_[0-9]+\.png\Z")
_MAX_LEGACY_FIGURE_ENTRIES = 4_096
_ANSI_ESCAPE_PATTERN = re.compile(r"(?:\x1b\][^\x07]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~])")
_PNG_FORMAT_EXTENSIONS = {"PNG": ".png"}

# 可选依赖允许安装后重试，因此失败状态不是永久缓存。
JUPYTER_AVAILABLE = False
IMPORT_ERROR: str | None = None
KernelManager: Any | None = None
_IMPORT_LOCK = threading.Lock()


def validate_png_bytes(image_data: object) -> bool:
    """通过共享图片边界完整解码一张有界、非动画 PNG。"""

    try:
        validate_image_bytes(
            image_data,
            limits=ImageValidationLimits(
                max_bytes=MAX_IMAGE_BYTES,
                max_pixels=MAX_IMAGE_PIXELS,
                max_frames=1,
            ),
            format_extensions=_PNG_FORMAT_EXTENSIONS,
            expected_format="PNG",
            allow_animation=False,
        )
    except ImageValidationError:
        return False
    return True


@dataclass
class KernelCleanupReport:
    """保留分阶段的尽力清理结果，供隔离决策、诊断和测试使用。"""

    context: str
    channels_stopped: bool | None = None
    shutdown_succeeded: bool | None = None
    resources_cleaned: bool | None = None
    fallback_methods: list[str] = field(default_factory=list)
    fallback_succeeded: bool | None = None
    alive_after: bool | None = None
    orphan_confirmed_absent: bool = True
    errors: list[str] = field(default_factory=list)

    def record_error(self, stage: str, exc: BaseException | None = None) -> None:
        """只保留阶段与异常类型，避免诊断报告携带路径或凭据正文。"""

        error_type = type(exc).__name__ if exc is not None else "Unavailable"
        self.errors.append(f"{stage}:{error_type}")

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


@dataclass(slots=True)
class _OutputBudget:
    """在 stdout、stderr、表达式结果和 traceback 之间共享字节预算。"""

    used_bytes: int = 0

    def append(self, current: str, value: object) -> tuple[str, bool]:
        text = value if isinstance(value, str) else str(value or "")
        text = _ANSI_ESCAPE_PATTERN.sub("", text)
        text = "".join(
            character
            for character in text
            if character in "\n\r\t"
            or (ord(character) >= 32 and not 0x7F <= ord(character) <= 0x9F)
        )
        encoded = text.encode("utf-8", errors="replace")
        remaining = MAX_OUTPUT_BYTES - self.used_bytes
        if remaining <= 0:
            return current, bool(encoded)
        kept = encoded[:remaining].decode("utf-8", errors="ignore")
        self.used_bytes += len(kept.encode("utf-8"))
        return current + kept, len(encoded) > remaining


@dataclass(slots=True)
class _ExecutionState:
    """记录可能仍在线程中运行的 I/O，保证取消时可以等待其收敛。"""

    kernel_was_running: bool
    pending_task: asyncio.Task[Any] | None = None
    pending_kind: str | None = None
    recovery_task: asyncio.Task[str] | None = None
    msg_id: str | None = None


def lazy_import_jupyter() -> None:
    """线程安全地惰性导入唯一实际使用的同步 KernelManager。"""

    global IMPORT_ERROR, JUPYTER_AVAILABLE, KernelManager
    with _IMPORT_LOCK:
        if JUPYTER_AVAILABLE and KernelManager is not None:
            return
        try:
            from jupyter_client import KernelManager as imported_manager
        except ImportError as exc:
            JUPYTER_AVAILABLE = False
            IMPORT_ERROR = str(exc)
            KernelManager = None
            return
        KernelManager = imported_manager
        IMPORT_ERROR = None
        JUPYTER_AVAILABLE = True


class JupyterKernelManager:
    _instances: ClassVar[dict[str, JupyterKernelManager]] = {}
    _quarantined_instances: ClassVar[set[JupyterKernelManager]] = set()
    _instances_lock = threading.Lock()

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.figures_dir = self.data_dir / "figures"
        self._cleanup_legacy_figures()

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
        self._shutdown_task: asyncio.Task[None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def _cleanup_legacy_figures(self) -> None:
        """有界删除两代旧输出布局，同时保留链接和无关用户文件。"""

        try:
            # ``Path.iterdir()`` 延迟到首次迭代才访问目录，异常边界必须包住循环。
            for index, child in enumerate(self.figures_dir.iterdir()):
                if index >= _MAX_LEGACY_FIGURE_ENTRIES:
                    log_sensitive_audit(
                        logger,
                        "jupyter.figures.migration",
                        status="bounded",
                        error_type="EntryLimit",
                        level=logging.WARNING,
                    )
                    break
                self._remove_legacy_figure(child)
        except (FileNotFoundError, NotADirectoryError, OSError):
            return
        with suppress(OSError):
            self.figures_dir.rmdir()

    @staticmethod
    def _remove_legacy_figure(child: Path) -> None:
        """只删除名称和类型都符合旧插件布局的真实文件。"""

        try:
            if child.is_symlink():
                return
            if child.is_dir() and _LEGACY_FIGURE_DIR_PATTERN.fullmatch(child.name):
                shutil.rmtree(child)
                return
            if child.is_file() and _LEGACY_FIGURE_FILE_PATTERN.fullmatch(child.name):
                child.unlink()
        except OSError:
            log_sensitive_audit(
                logger,
                "jupyter.figures.migration",
                status="error",
                error_type="LegacyCleanupError",
                level=logging.WARNING,
            )

    @staticmethod
    def _instance_key(data_dir: Path, owner_key: str) -> str:
        if (
            type(owner_key) is not str
            or not owner_key
            or len(owner_key) > 128
            or any(ord(character) < 33 or ord(character) > 126 for character in owner_key)
        ):
            raise ValueError("Jupyter owner key is invalid")
        resolved_dir = str(Path(data_dir).resolve())
        return f"{owner_key}::{resolved_dir}"

    @classmethod
    def get_instance(cls, data_dir: Path, owner_key: str) -> JupyterKernelManager:
        key = cls._instance_key(data_dir, owner_key)
        with cls._instances_lock:
            instance = cls._instances.get(key)
            if instance is None or instance._broken:
                registered = set(cls._instances.values()) | cls._quarantined_instances
                if key not in cls._instances and len(registered) >= MAX_KERNEL_INSTANCES:
                    raise RuntimeError("Jupyter kernel instance limit reached")
                instance = cls(data_dir)
                cls._instances[key] = instance
            return instance

    @classmethod
    def _isolate_instance(cls, instance: JupyterKernelManager) -> None:
        with cls._instances_lock:
            stale_keys = [key for key, value in cls._instances.items() if value is instance]
            for key in stale_keys:
                cls._instances.pop(key, None)
            cls._quarantined_instances.add(instance)

    @classmethod
    def _forget_instance(cls, instance: JupyterKernelManager) -> None:
        with cls._instances_lock:
            stale_keys = [key for key, value in cls._instances.items() if value is instance]
            for key in stale_keys:
                cls._instances.pop(key, None)
            cls._quarantined_instances.discard(instance)

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
        results = await asyncio.gather(
            *(
                asyncio.to_thread(
                    instance.shutdown_kernel,
                    False,
                    evict_instance=False,
                )
                for instance in instances
            ),
            return_exceptions=True,
        )
        failures = [result for result in results if isinstance(result, BaseException)]
        if failures:
            for failure in failures:
                log_sensitive_audit(
                    logger,
                    "jupyter.kernel.shutdown_all",
                    status="error",
                    exc=failure,
                )
            raise RuntimeError(f"{len(failures)} Jupyter kernel cleanup operation(s) failed")

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
        deadline = max(0.1, timeout)
        result: list[Any] = []
        errors: list[BaseException] = []

        async def await_value() -> Any:
            return await asyncio.wait_for(value, timeout=deadline)

        def run() -> None:
            try:
                result.append(asyncio.run(await_value()))
            except BaseException as exc:  # noqa: BLE001 - 把辅助线程异常原样交还调用方。
                errors.append(exc)

        worker = threading.Thread(target=run, name="jupyter-cleanup-awaitable", daemon=True)
        worker.start()
        worker.join(timeout=deadline + 0.5)
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
        except BaseException as exc:  # noqa: BLE001 - 单步失败后仍需继续后续清理。
            if report is not None:
                report.record_error(f"{stage}.is_alive", exc)
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
        process = getattr(provisioner, "process", None)
        if process is None:
            process = getattr(km, "process", None)
        process_kill = getattr(process, "kill", None)
        if callable(process_kill):
            candidates.append(("process.kill", process_kill, {}))

        for name, callback, kwargs in candidates:
            report.fallback_methods.append(name)
            try:
                cls._invoke_cleanup(callback, **kwargs)
                report.fallback_succeeded = True
            except BaseException as exc:  # noqa: BLE001 - 失败后继续尝试下一个兜底。
                report.record_error(name, exc)
                continue
            alive = cls._wait_for_kernel_exit(km, report, stage=name)
            if alive is False:
                report.alive_after = False
                return True
        return False

    @classmethod
    def _stop_client_channels(
        cls,
        kc: Any,
        report: KernelCleanupReport,
    ) -> None:
        if kc is None:
            return
        stop_channels = getattr(kc, "stop_channels", None)
        if not callable(stop_channels):
            return
        try:
            cls._invoke_cleanup(stop_channels)
            report.channels_stopped = True
        except BaseException as exc:  # noqa: BLE001 - 关闭 channels 失败后仍需回收内核。
            report.channels_stopped = False
            report.record_error("stop_channels", exc)

    @classmethod
    def _request_kernel_shutdown(cls, km: Any, report: KernelCleanupReport) -> None:
        shutdown = getattr(km, "shutdown_kernel", None)
        if not callable(shutdown):
            report.shutdown_succeeded = False
            report.record_error("shutdown_kernel")
            return
        try:
            cls._invoke_cleanup(shutdown, now=True)
            report.shutdown_succeeded = True
        except BaseException as exc:  # noqa: BLE001 - 正常关闭失败后仍要进入强杀兜底。
            report.shutdown_succeeded = False
            report.record_error("shutdown_kernel", exc)

    @classmethod
    def _request_resource_cleanup(
        cls,
        km: Any,
        report: KernelCleanupReport,
        *,
        after_kill: bool = False,
    ) -> Any | None:
        cleanup_resources = getattr(km, "cleanup_resources", None)
        if not callable(cleanup_resources):
            return None
        stage = "cleanup_resources_after_kill" if after_kill else "cleanup_resources"
        try:
            cls._invoke_cleanup(cleanup_resources, restart=False)
            report.resources_cleaned = True
        except BaseException as exc:  # noqa: BLE001 - 资源清理和退出确认分别记录。
            report.resources_cleaned = False
            report.record_error(stage, exc)
        return cleanup_resources

    @classmethod
    def _cleanup_kernel_resources(
        cls,
        km: Any,
        kc: Any,
        *,
        context: str,
    ) -> KernelCleanupReport:
        report = KernelCleanupReport(context=context)
        cls._stop_client_channels(kc, report)
        if km is None:
            report.alive_after = False
            report.orphan_confirmed_absent = True
            return report

        cls._request_kernel_shutdown(km, report)
        cleanup_resources = cls._request_resource_cleanup(km, report)

        alive = cls._kernel_alive(km, report=report, stage="post_shutdown")
        report.alive_after = alive
        if alive is False or (alive is None and report.shutdown_succeeded is True):
            report.orphan_confirmed_absent = True
            return report

        report.fallback_succeeded = False
        killed = cls._force_kill_kernel(km, report)
        if killed:
            if callable(cleanup_resources) and report.resources_cleaned is False:
                cls._request_resource_cleanup(km, report, after_kill=True)
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
        log_sensitive_audit(
            logger,
            "jupyter.kernel.cleanup",
            status="quarantined",
            error_type="KernelCleanupIncomplete",
            level=logging.ERROR,
        )

    async def _shutdown_if_expired(self) -> bool:
        """在没有执行占用时重新确认空闲状态，并按需关闭内核。"""

        async with self._execute_lock:
            if not self.is_running:
                return True
            idle_time = time.monotonic() - self._last_activity
            if idle_time <= AUTO_SHUTDOWN_TIMEOUT:
                return False
            log_sensitive_audit(
                logger,
                "jupyter.kernel.idle_shutdown",
                status="started",
            )
            await asyncio.to_thread(self.shutdown_kernel, False)
            return True

    async def _check_idleness_loop(self) -> None:
        """后台任务：检查空闲时间并自动关闭。"""

        while self.is_running:
            await asyncio.sleep(CHECK_INTERVAL)
            if await self._shutdown_if_expired():
                break

    def ensure_idle_monitor(self) -> None:
        self._loop = asyncio.get_running_loop()
        if self._shutdown_task is None or self._shutdown_task.done():
            self._shutdown_task = asyncio.create_task(self._check_idleness_loop())

    def get_status(self) -> dict[str, Any]:
        """获取内核状态"""
        with self._lifecycle_lock:
            if not self.is_running or self._km is None:
                return {"running": False, "message": "内核未启动"}
            uptime = time.monotonic() - self._started_at if self._started_at else 0.0
            return {
                "running": True,
                "kernel_name": str(getattr(self._km, "kernel_name", "unknown")),
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
            factory = KernelManager
            if not JUPYTER_AVAILABLE or not callable(factory):
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
                km = factory(kernel_name=kernel_name)
                km.start_kernel()
                kc = km.client()
                kc.start_channels()
                kc.wait_for_ready(timeout=30)
                self._initialize_matplotlib(kc)
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

            # ready 与内联后端初始化全部成功后，才发布可复用实例。
            self._km = km
            self._kc = kc
            self._orphan_km = None
            self._orphan_kc = None
            self._started_at = time.monotonic()
            self._execution_count = 0
            self._last_activity = time.monotonic()
            return True

    @staticmethod
    def _initialize_matplotlib(kc: Any) -> None:
        """在发布内核前等待可选 matplotlib 初始化消息完整收敛。"""

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
    except Exception:
        pass
except ImportError:
    pass
"""
        msg_id = str(kc.execute(init_code))
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            try:
                message = kc.get_iopub_msg(timeout=0.2)
            except (TimeoutError, QueueEmpty):
                continue
            if JupyterKernelManager._is_matching_idle(message, msg_id):
                return
        raise TimeoutError("Jupyter kernel initialization did not become idle")

    def shutdown_kernel(
        self,
        cancel_idle_task: bool = True,
        *,
        evict_instance: bool = True,
    ) -> None:
        """关闭内核"""
        with self._lifecycle_lock:
            if cancel_idle_task and self._shutdown_task and not self._shutdown_task.done():
                task = self._shutdown_task
                if self._loop and self._loop.is_running():
                    self._loop.call_soon_threadsafe(task.cancel)
                else:
                    task.cancel()
            self._shutdown_task = None
            self._loop = None

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
                if evict_instance:
                    self._forget_instance(self)
                else:
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
                self.shutdown_kernel(cancel_idle_task=False, evict_instance=False)
            self.start_kernel(kernel_name)

    def interrupt_kernel(self) -> None:
        """中断当前执行中的代码，避免超时后继续占用内核状态。"""
        with self._lifecycle_lock:
            if self._km is not None and self.is_running:
                self._km.interrupt_kernel()

    @staticmethod
    def _is_matching_idle(message: Any, msg_id: str | None) -> bool:
        if not msg_id or not isinstance(message, dict) or message.get("msg_type") != "status":
            return False
        content = message.get("content")
        parent = message.get("parent_header")
        return bool(
            isinstance(content, dict)
            and isinstance(parent, dict)
            and content.get("execution_state") == "idle"
            and parent.get("msg_id") == msg_id
        )

    def _require_client(self) -> Any:
        client = self._kc
        if client is None:
            raise RuntimeError("Jupyter kernel client is unavailable")
        return client

    @staticmethod
    def _normalize_timeout(value: object) -> float:
        try:
            timeout = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError, OverflowError):
            return float(DEFAULT_TIMEOUT)
        if not math.isfinite(timeout) or timeout <= 0:
            return float(DEFAULT_TIMEOUT)
        return min(timeout, float(MAX_EXECUTION_TIMEOUT))

    @staticmethod
    async def _wait_task_despite_cancellation(task: asyncio.Task[Any]) -> Any:
        while not task.done():
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
        return task.result()

    @staticmethod
    async def _run_tracked_thread(
        state: _ExecutionState,
        kind: str,
        callback: Any,
        *args: Any,
    ) -> Any:
        """在线程调用完成前保留任务引用，供取消清理路径接管。"""

        task = asyncio.create_task(
            asyncio.to_thread(callback, *args),
            name=f"jupyter-io-{kind}",
        )
        state.pending_task = task
        state.pending_kind = kind
        try:
            value = await asyncio.shield(task)
        except asyncio.CancelledError:
            raise
        except BaseException:
            state.pending_task = None
            state.pending_kind = None
            raise
        state.pending_task = None
        state.pending_kind = None
        return value

    async def _await_cancel_pending(
        self,
        task: asyncio.Task[Any] | None,
        audit_id: str,
    ) -> tuple[Any, bool]:
        if task is None:
            return None, False
        try:
            return await self._wait_task_despite_cancellation(task), False
        except Exception as exc:
            log_sensitive_audit(
                logger,
                "jupyter.cancel.pending_operation",
                status="error",
                job_id=audit_id,
                exc=exc,
            )
            return None, True

    async def _shutdown_new_kernel_after_cancel(
        self,
        kernel_was_running: bool,
        audit_id: str,
        operation: str,
    ) -> None:
        if kernel_was_running or not self.is_running:
            return
        try:
            await asyncio.to_thread(self.shutdown_kernel)
        except Exception as exc:
            log_sensitive_audit(
                logger,
                operation,
                status="quarantined",
                job_id=audit_id,
                exc=exc,
            )

    async def _cancel_execution_cleanup(
        self,
        *,
        state: _ExecutionState,
        audit_id: str,
    ) -> None:
        if state.recovery_task is not None:
            try:
                await self._wait_task_despite_cancellation(state.recovery_task)
            except Exception as exc:
                log_sensitive_audit(
                    logger,
                    "jupyter.cancel.recovery",
                    status="error",
                    job_id=audit_id,
                    exc=exc,
                )
            return

        pending_value, pending_failed = await self._await_cancel_pending(
            state.pending_task,
            audit_id,
        )
        if state.pending_kind == "start":
            await self._shutdown_new_kernel_after_cancel(
                state.kernel_was_running,
                audit_id,
                "jupyter.cancel.new_kernel_shutdown",
            )
            return
        if state.pending_kind == "submit" and not pending_failed:
            state.msg_id = str(pending_value)
        if (
            state.pending_kind == "read"
            and not pending_failed
            and self._is_matching_idle(pending_value, state.msg_id)
        ):
            return
        if not state.msg_id:
            await self._shutdown_new_kernel_after_cancel(
                state.kernel_was_running,
                audit_id,
                "jupyter.cancel.new_kernel_cleanup",
            )
            return

        try:
            await self._interrupt_and_recover(state.msg_id, audit_id)
        except Exception as exc:
            log_sensitive_audit(
                logger,
                "jupyter.cancel.cleanup",
                status="error",
                job_id=audit_id,
                exc=exc,
            )

    @staticmethod
    def _set_output_limit(result: ExecutionResult, message: str) -> None:
        result.error = message
        result.success = False

    def _append_image(
        self,
        result: ExecutionResult,
        encoded_image: object,
        total_image_bytes: int,
    ) -> tuple[int, bool]:
        if len(result.images) >= MAX_IMAGES:
            return total_image_bytes, False
        image = self._decode_image(encoded_image)
        if image is None:
            return total_image_bytes, False
        if total_image_bytes + len(image) > MAX_TOTAL_IMAGE_BYTES:
            self._set_output_limit(
                result,
                f"图片输出超过 {MAX_TOTAL_IMAGE_BYTES} 字节安全上限，已中断内核",
            )
            return total_image_bytes, True
        result.images.append(image)
        return total_image_bytes + len(image), False

    def _process_data_message(
        self,
        data: object,
        result: ExecutionResult,
        budget: _OutputBudget,
        total_image_bytes: int,
        *,
        include_text: bool,
    ) -> tuple[int, bool]:
        if not isinstance(data, dict):
            return total_image_bytes, False
        total_image_bytes, exceeded = self._append_image(
            result,
            data.get("image/png"),
            total_image_bytes,
        )
        if exceeded or not include_text or "text/plain" not in data:
            return total_image_bytes, exceeded
        separator = "\n" if result.result else ""
        result.result, exceeded = budget.append(
            result.result + separator,
            data.get("text/plain"),
        )
        return total_image_bytes, exceeded

    @staticmethod
    def _matching_message_content(
        message: object,
        msg_id: str,
    ) -> tuple[object, dict[str, Any]] | None:
        if not isinstance(message, dict):
            return None
        parent = message.get("parent_header")
        content = message.get("content")
        if not isinstance(parent, dict) or parent.get("msg_id") != msg_id:
            return None
        if not isinstance(content, dict):
            return None
        return message.get("msg_type"), content

    @staticmethod
    def _process_stream_message(
        content: dict[str, Any],
        result: ExecutionResult,
        budget: _OutputBudget,
    ) -> bool:
        name = content.get("name")
        if name == "stdout":
            result.stdout, exceeded = budget.append(result.stdout, content.get("text"))
            return exceeded
        if name == "stderr":
            result.stderr, exceeded = budget.append(result.stderr, content.get("text"))
            return exceeded
        return False

    @staticmethod
    def _process_error_message(
        content: dict[str, Any],
        result: ExecutionResult,
        budget: _OutputBudget,
    ) -> bool:
        traceback = content.get("traceback")
        lines = traceback if isinstance(traceback, list) else []
        result.error, exceeded = budget.append("", "\n".join(map(str, lines)))
        result.success = False
        return exceeded

    def _process_iopub_message(
        self,
        message: object,
        msg_id: str,
        result: ExecutionResult,
        budget: _OutputBudget,
        total_image_bytes: int,
    ) -> tuple[str, int]:
        """处理一条属于当前执行的 IOPub 消息，并返回下一步动作。"""

        payload = self._matching_message_content(message, msg_id)
        if payload is None:
            return "continue", total_image_bytes
        msg_type, content = payload
        exceeded = False
        if msg_type == "stream":
            exceeded = self._process_stream_message(content, result, budget)
        elif msg_type in {"execute_result", "display_data"}:
            total_image_bytes, exceeded = self._process_data_message(
                content.get("data"),
                result,
                budget,
                total_image_bytes,
                include_text=msg_type == "execute_result",
            )
        elif msg_type == "error":
            exceeded = self._process_error_message(content, result, budget)
        elif msg_type == "status" and content.get("execution_state") == "idle":
            return "idle", total_image_bytes

        if exceeded:
            self._set_output_limit(
                result,
                f"输出超过 {MAX_OUTPUT_BYTES} 字节安全上限，已中断内核",
            )
            return "limit", total_image_bytes
        return "continue", total_image_bytes

    async def _interrupt_and_recover(self, msg_id: str, audit_id: str) -> str:
        try:
            await asyncio.to_thread(self.interrupt_kernel)
        except Exception as exc:
            log_sensitive_audit(
                logger,
                "jupyter.interrupt",
                status="error",
                job_id=audit_id,
                exc=exc,
            )
        return await self._recover_after_interrupt(msg_id, audit_id=audit_id)

    async def _run_recovery(self, state: _ExecutionState, audit_id: str) -> None:
        if state.msg_id is None:
            raise RuntimeError("Jupyter execution message id is unavailable")
        task = asyncio.create_task(
            self._interrupt_and_recover(state.msg_id, audit_id),
            name="jupyter-interrupt-recovery",
        )
        state.recovery_task = task
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            raise
        except BaseException:
            state.recovery_task = None
            raise
        state.recovery_task = None

    async def _contain_execution_failure(
        self,
        state: _ExecutionState,
        audit_id: str,
    ) -> None:
        """提交状态不确定时恢复已知执行；没有消息 ID 时直接关闭内核。"""

        try:
            if state.msg_id is not None:
                await self._run_recovery(state, audit_id)
            elif self.is_running:
                await asyncio.to_thread(self.shutdown_kernel)
        except Exception as exc:
            log_sensitive_audit(
                logger,
                "jupyter.execute.containment",
                status="quarantined",
                job_id=audit_id,
                exc=exc,
            )

    async def _collect_execution_messages(
        self,
        state: _ExecutionState,
        client: Any,
        result: ExecutionResult,
        budget: _OutputBudget,
        timeout: float,
        started_at: float,
        audit_id: str,
    ) -> None:
        total_image_bytes = 0
        deadline = started_at + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                result.error = f"执行超时 ({timeout}s)"
                result.success = False
                await self._run_recovery(state, audit_id)
                return
            try:
                message = await self._run_tracked_thread(
                    state,
                    "read",
                    client.get_iopub_msg,
                    min(remaining, 0.25),
                )
            except (TimeoutError, QueueEmpty):
                continue
            action, total_image_bytes = self._process_iopub_message(
                message,
                str(state.msg_id),
                result,
                budget,
                total_image_bytes,
            )
            if action == "continue":
                continue
            if action == "limit":
                await self._run_recovery(state, audit_id)
            return

    async def execute(
        self,
        code: str,
        timeout: float = DEFAULT_TIMEOUT,
        *,
        audit_id: str | None = None,
    ) -> ExecutionResult:
        """执行代码，并在外部取消返回前收敛底层 kernel 工作。"""
        async with self._execute_lock:
            timeout = self._normalize_timeout(timeout)
            audit_id = safe_audit_id(audit_id)
            if audit_id == "-":
                audit_id = uuid.uuid4().hex
            state = _ExecutionState(kernel_was_running=self.is_running)
            result = ExecutionResult()
            budget = _OutputBudget()
            started_at = time.monotonic()

            try:
                if not self.is_running:
                    await self._run_tracked_thread(state, "start", self.start_kernel)
                self._last_activity = time.monotonic()
                self.ensure_idle_monitor()
                started_at = time.monotonic()
                client = self._require_client()
                submitted_id = await self._run_tracked_thread(
                    state,
                    "submit",
                    client.execute,
                    code,
                )
                if not isinstance(submitted_id, str) or not submitted_id:
                    raise RuntimeError("Jupyter kernel returned an invalid message id")
                state.msg_id = submitted_id
                await self._collect_execution_messages(
                    state,
                    client,
                    result,
                    budget,
                    timeout,
                    started_at,
                    audit_id,
                )
                self._execution_count += 1
            except asyncio.CancelledError:
                cleanup_task = asyncio.create_task(
                    self._cancel_execution_cleanup(
                        state=state,
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
                    log_sensitive_audit(
                        logger,
                        "jupyter.cancel.cleanup_task",
                        status="error",
                        job_id=audit_id,
                        exc=exc,
                    )
                self._last_activity = time.monotonic()
                raise
            except Exception as exc:
                await self._contain_execution_failure(state, audit_id)
                log_sensitive_audit(
                    logger,
                    "jupyter.execute",
                    status="error",
                    job_id=audit_id,
                    payload=code,
                    exc=exc,
                )
                result.error = "执行失败，请稍后重试"
                result.success = False

            self._last_activity = time.monotonic()
            result.execution_time = time.monotonic() - started_at
            return result

    async def _recover_after_interrupt(
        self,
        msg_id: str,
        *,
        audit_id: str | None = None,
    ) -> str:
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            try:
                msg = await asyncio.to_thread(self._require_client().get_iopub_msg, 0.2)
            except (TimeoutError, QueueEmpty):
                continue
            except Exception:
                break
            if self._is_matching_idle(msg, msg_id):
                return "idle"
        try:
            await asyncio.to_thread(self.restart_kernel)
            return "restarted"
        except Exception as exc:
            log_sensitive_audit(
                logger,
                "jupyter.interrupt.restart",
                status="error",
                job_id=audit_id,
                exc=exc,
            )
            try:
                await asyncio.to_thread(self.shutdown_kernel)
                return "shutdown"
            except Exception as shutdown_exc:
                log_sensitive_audit(
                    logger,
                    "jupyter.interrupt.shutdown",
                    status="quarantined",
                    job_id=audit_id,
                    exc=shutdown_exc,
                )
                return "quarantined"

    @staticmethod
    def _decode_image(base64_data: object) -> bytes | None:
        """在内存中严格解码一个有界 PNG。"""

        try:
            if type(base64_data) is not str:
                return None
            if len(base64_data) > ((MAX_IMAGE_BYTES + 2) // 3) * 4:
                return None
            image_data = base64.b64decode(base64_data, validate=True)
            return image_data if validate_png_bytes(image_data) else None
        except (ValueError, binascii.Error):
            return None
