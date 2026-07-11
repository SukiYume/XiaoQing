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
from typing import Any, Optional

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
        
        self._km: Optional[Any] = None
        self._kc: Optional[Any] = None
        self._started_at: Optional[float] = None
        self._execution_count = 0
        self._execute_lock = asyncio.Lock()
        self._lifecycle_lock = threading.RLock()
        self._broken = False
        self._last_cleanup_report: KernelCleanupReport | None = None
        self._orphan_km: Any | None = None
        self._orphan_kc: Any | None = None
        
        # 自动关闭相关
        self._last_activity = 0.0
        self._shutdown_task: Optional[asyncio.Task] = None
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
            instances = list(
                dict.fromkeys([*cls._instances.values(), *cls._quarantined_instances])
            )
            cls._instances.clear()
            cls._quarantined_instances.clear()
        for instance in instances:
            try:
                instance.shutdown_kernel()
            except Exception:
                logger.exception("Failed to confirm Jupyter kernel shutdown")

    @classmethod
    async def shutdown_all_async(cls) -> None:
        with cls._instances_lock:
            instances = list(
                dict.fromkeys([*cls._instances.values(), *cls._quarantined_instances])
            )
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
                parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in parameters
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
        logger.error("Jupyter manager isolated after unconfirmed cleanup: %s", report.summary())
    
    async def _check_idleness_loop(self):
        """后台任务：检查空闲时间并自动关闭"""
        while self.is_running:
            await asyncio.sleep(CHECK_INTERVAL)
            if not self.is_running:
                break
            
            idle_time = time.time() - self._last_activity
            if idle_time > AUTO_SHUTDOWN_TIMEOUT:
                # 这里使用 print，实际应该通过回调或者 event 通知日志系统
                # 但为了不引入复杂的 context 传递，暂时简化
                print(f"[Jupyter] 内核空闲超时 ({idle_time:.0f}s)，正在自动关闭...")
                await asyncio.to_thread(self.shutdown_kernel, False)
                break

    def ensure_idle_monitor(self) -> None:
        self._loop = asyncio.get_running_loop()
        if self._shutdown_task is None or self._shutdown_task.done():
            self._shutdown_task = asyncio.create_task(self._check_idleness_loop())

    def get_status(self) -> dict[str, Any]:
        """获取内核状态"""
        if not self.is_running:
            return {
                "running": False,
                "message": "内核未启动"
            }
        
        uptime = time.time() - self._started_at if self._started_at else 0
        return {
            "running": True,
            "kernel_name": self._km.kernel_name,
            "uptime": uptime,
            "execution_count": self._execution_count,
            "message": f"内核运行中 (已执行 {self._execution_count} 次, 运行 {uptime:.0f}s)"
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
                        "旧 Jupyter kernel 无法确认退出；实例已隔离。"
                        f"{stale_report.summary()}"
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
                raise RuntimeError(
                    f"启动内核失败: {exc}; cleanup: {report.summary()}"
                ) from exc

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
    
    async def execute(self, code: str, timeout: float = DEFAULT_TIMEOUT) -> ExecutionResult:
        """执行代码"""
        async with self._execute_lock:
            if not self.is_running:
                await asyncio.to_thread(self.start_kernel)

            # 更新活动时间
            self._last_activity = time.time()

            # 在 asyncio 事件循环上下文中启动空闲检查任务（不能在 start_kernel 中做，因为它可能在线程池中运行）
            self.ensure_idle_monitor()

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

            try:
                msg_id = await asyncio.to_thread(self._kc.execute, code)

                while True:
                    remaining = deadline - time.time()
                    if remaining <= 0:
                        result.error = f"执行超时 ({timeout}s)"
                        result.success = False
                        await asyncio.to_thread(self.interrupt_kernel)
                        await self._recover_after_interrupt(msg_id)
                        break

                    try:
                        msg = await asyncio.to_thread(
                            self._kc.get_iopub_msg,
                            min(remaining, 0.25),
                        )
                    except TimeoutError:
                        continue

                    parent_id = msg.get("parent_header", {}).get("msg_id")
                    if parent_id != msg_id:
                        continue

                    msg_type = msg["msg_type"]
                    content = msg.get("content", {})

                    if msg_type == "stream":
                        if content.get("name") == "stdout":
                            result.stdout, exceeded = append_text(result.stdout, content.get("text", ""))
                        elif content.get("name") == "stderr":
                            result.stderr, exceeded = append_text(result.stderr, content.get("text", ""))
                        else:
                            exceeded = False
                        if exceeded:
                            result.error = f"输出超过 {MAX_OUTPUT_BYTES} 字节安全上限，已中断内核"
                            result.success = False
                            await asyncio.to_thread(self.interrupt_kernel)
                            await self._recover_after_interrupt(msg_id)
                            break

                    elif msg_type == "execute_result":
                        data = content.get("data", {})
                        if "image/png" in data and image_count < MAX_IMAGES:
                            img_path = self._save_image(data["image/png"], image_count, execution_dir)
                            if img_path:
                                result.images.append(img_path)
                                image_count += 1

                        if "text/plain" in data:
                            result.result, exceeded = append_text("", data["text/plain"])
                            if exceeded:
                                result.error = f"输出超过 {MAX_OUTPUT_BYTES} 字节安全上限，已中断内核"
                                result.success = False
                                await asyncio.to_thread(self.interrupt_kernel)
                                await self._recover_after_interrupt(msg_id)
                                break

                    elif msg_type == "display_data":
                        data = content.get("data", {})
                        if "image/png" in data and image_count < MAX_IMAGES:
                            img_path = self._save_image(data["image/png"], image_count, execution_dir)
                            if img_path:
                                result.images.append(img_path)
                                image_count += 1

                    elif msg_type == "error":
                        traceback = content.get("traceback", [])
                        cleaned = [re.sub(r'\x1b\[[0-9;]*m', '', line) for line in traceback]
                        result.error, _exceeded = append_text("", "\n".join(cleaned))
                        result.success = False

                    elif msg_type == "status" and content.get("execution_state") == "idle":
                        break

                self._execution_count += 1

            except Exception as e:
                result.error = f"执行异常: {e}"
                result.success = False

            self._last_activity = time.time()
            result.execution_time = time.time() - start_time
            return result
    
    async def _recover_after_interrupt(self, msg_id: str) -> None:
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
                return
        try:
            await asyncio.to_thread(self.restart_kernel)
        except Exception:
            try:
                await asyncio.to_thread(self.shutdown_kernel)
            except Exception:
                self._km = None
                self._kc = None

    def _save_image(self, base64_data: str, index: int, execution_dir: Path) -> Optional[Path]:
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
