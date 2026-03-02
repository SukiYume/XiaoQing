"""
Jupyter 内核管理器
"""
import asyncio
import base64
import time
import re
import threading
from pathlib import Path
from typing import Any, Optional

from core.plugin_base import ensure_dir

from .jupyter_config import MAX_IMAGES, CHECK_INTERVAL, AUTO_SHUTDOWN_TIMEOUT, DEFAULT_TIMEOUT
from .jupyter_models import ExecutionResult

# 全局变量保存导入状态
JUPYTER_AVAILABLE = False
IMPORT_ERROR = None
KernelManager = None
AsyncKernelManager = None

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
    _instances_lock = threading.Lock()
    
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.figures_dir = data_dir / "figures"
        ensure_dir(self.figures_dir)
        
        self._km: Optional[Any] = None
        self._kc: Optional[Any] = None
        self._started_at: Optional[float] = None
        self._execution_count = 0
        
        # 自动关闭相关
        self._last_activity = 0.0
        self._shutdown_task: Optional[asyncio.Task] = None
    
    @classmethod
    def get_instance(cls, data_dir: Path, owner_key: str = "global") -> "JupyterKernelManager":
        key = str(owner_key or "global")
        with cls._instances_lock:
            instance = cls._instances.get(key)
            if instance is None:
                instance = cls(data_dir)
                cls._instances[key] = instance
            return instance

    @classmethod
    def shutdown_all(cls) -> None:
        with cls._instances_lock:
            instances = list(cls._instances.values())
            cls._instances.clear()
        for instance in instances:
            instance.shutdown_kernel()
    
    @property
    def is_running(self) -> bool:
        """检查内核是否运行中"""
        return self._km is not None and self._km.is_alive()
    
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
                await asyncio.to_thread(self.shutdown_kernel)
                break

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
        # 确保依赖已导入
        lazy_import_jupyter()
        if not JUPYTER_AVAILABLE:
            raise ImportError(f"Jupyter 依赖未加载: {IMPORT_ERROR}")
            
        if self.is_running:
            return True
        
        try:
            self._km = KernelManager(kernel_name=kernel_name)
            self._km.start_kernel()
            
            self._kc = self._km.client()
            self._kc.start_channels()
            self._kc.wait_for_ready(timeout=30)
            
            self._started_at = time.time()
            self._execution_count = 0
            
            # 记录活动时间（自动关闭任务将在 execute 方法中启动）
            self._last_activity = time.time()
            
            # 自动配置 matplotlib 内联后端
            self._init_matplotlib()
            
            return True
        except Exception as e:
            self._km = None
            self._kc = None
            raise RuntimeError(f"启动内核失败: {e}")
    
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
    
    def shutdown_kernel(self) -> None:
        """关闭内核"""
        if self._shutdown_task and not self._shutdown_task.done():
            self._shutdown_task.cancel()
            self._shutdown_task = None
            
        if self._kc:
            self._kc.stop_channels()
            self._kc = None
        
        if self._km:
            self._km.shutdown_kernel(now=True)
            self._km = None
        
        self._started_at = None
        self._execution_count = 0
    
    def restart_kernel(self) -> None:
        """重启内核"""
        if self._km and self.is_running:
            self._km.restart_kernel()
            self._kc.wait_for_ready(timeout=30)
            self._started_at = time.time()
            self._execution_count = 0
            self._last_activity = time.time()
            self._init_matplotlib()
        else:
            self.start_kernel()
    
    async def execute(self, code: str, timeout: float = DEFAULT_TIMEOUT) -> ExecutionResult:
        """执行代码"""
        need_start_idle_check = False
        if not self.is_running:
            await asyncio.to_thread(self.start_kernel)
            need_start_idle_check = True
        
        # 更新活动时间
        self._last_activity = time.time()
        
        # 在 asyncio 事件循环上下文中启动空闲检查任务（不能在 start_kernel 中做，因为它可能在线程池中运行）
        if need_start_idle_check:
            if self._shutdown_task and not self._shutdown_task.done():
                self._shutdown_task.cancel()
            self._shutdown_task = asyncio.create_task(self._check_idleness_loop())
        
        result = ExecutionResult()
        start_time = time.time()
        image_count = 0
        
        try:
            # 发送执行请求
            msg_id = self._kc.execute(code)
            
            # 收集输出
            while True:
                try:
                    msg = self._kc.get_iopub_msg(timeout=timeout)
                except TimeoutError:
                    result.error = f"执行超时 ({timeout}s)"
                    result.success = False
                    break
                
                msg_type = msg["msg_type"]
                content = msg.get("content", {})
                
                # 标准输出
                if msg_type == "stream":
                    if content.get("name") == "stdout":
                        result.stdout += content.get("text", "")
                    elif content.get("name") == "stderr":
                        result.stderr += content.get("text", "")
                
                # 执行结果
                elif msg_type == "execute_result":
                    data = content.get("data", {})
                    if "image/png" in data and image_count < MAX_IMAGES:
                        img_path = self._save_image(data["image/png"], image_count)
                        if img_path:
                            result.images.append(img_path)
                            image_count += 1
                    
                    if "text/plain" in data:
                        result.result = data["text/plain"]
                
                # 显示数据
                elif msg_type == "display_data":
                    data = content.get("data", {})
                    if "image/png" in data and image_count < MAX_IMAGES:
                        img_path = self._save_image(data["image/png"], image_count)
                        if img_path:
                            result.images.append(img_path)
                            image_count += 1
                
                # 错误
                elif msg_type == "error":
                    traceback = content.get("traceback", [])
                    cleaned = [re.sub(r'\x1b\[[0-9;]*m', '', line) for line in traceback]
                    result.error = "\n".join(cleaned)
                    result.success = False
                
                # 执行完成
                elif msg_type == "status":
                    if content.get("execution_state") == "idle":
                        parent_id = msg.get("parent_header", {}).get("msg_id")
                        if parent_id == msg_id:
                            break
            
            self._execution_count += 1
            
        except Exception as e:
            result.error = f"执行异常: {e}"
            result.success = False
        
        result.execution_time = time.time() - start_time
        return result
    
    def _save_image(self, base64_data: str, index: int) -> Optional[Path]:
        """保存 base64 图片到文件"""
        try:
            filename = f"output_{int(time.time())}_{index}.png"
            filepath = self.figures_dir / filename
            image_data = base64.b64decode(base64_data)
            filepath.write_bytes(image_data)
            return filepath
        except Exception:
            return None
