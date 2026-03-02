import asyncio
import signal
import sys, os
# Workaround for Intel MKL library conflict when torch and numpy coexist
os.environ['KMP_DUPLICATE_LIB_OK'] = 'TRUE'

# 尽早导入 torch 以避免 DLL 加载冲突 (WinError 127)
try:
    import torch
except ImportError:
    pass

from pathlib import Path

# 将当前目录添加到 sys.path，使项目不依赖文件夹名称
sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.app import XiaoQingApp


async def main() -> None:
    app = XiaoQingApp(Path(__file__).resolve().parent)
    stop_event = asyncio.Event()

    def request_stop() -> None:
        if stop_event.is_set():
            return
        print("\n收到退出信号，正在优雅关闭...")
        stop_event.set()

    loop = asyncio.get_running_loop()
    if sys.platform != "win32":
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, request_stop)
    else:
        signal.signal(signal.SIGINT, lambda *_: loop.call_soon_threadsafe(request_stop))

    try:
        await app.start()
    except Exception:
        try:
            await asyncio.shield(app.stop())
        except Exception:
            pass
        raise
    try:
        await stop_event.wait()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            await asyncio.shield(app.stop())
        except asyncio.CancelledError:
            pass


def cli() -> None:
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    cli()
