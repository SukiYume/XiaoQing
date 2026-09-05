import argparse
import asyncio
import signal
import sys
from collections.abc import Callable, Sequence
from pathlib import Path


def _build_parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(
        prog        = "xiaoqing",
        description = "XiaoQing QQ Bot framework",
    )


def _prepare_runtime() -> None:
    """Load optional runtime dependencies without changing operator environment."""
    # Some optional plugins require torch. Importing it early preserves the
    # established DLL-load behavior, but OpenMP compatibility settings remain
    # an explicit deployment decision rather than a process-wide workaround.
    try:
        import torch  # noqa: F401
    except ImportError:
        pass


def _install_stop_signal_handlers(
    loop: asyncio.AbstractEventLoop,
    request_stop: Callable[[], None],
) -> None:
    if sys.platform != "win32":
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, request_stop)
        return

    def schedule_stop(*_: object) -> None:
        loop.call_soon_threadsafe(request_stop)

    signal.signal(signal.SIGINT, schedule_stop)
    sigbreak = getattr(signal, "SIGBREAK", None)
    if sigbreak is not None:
        signal.signal(sigbreak, schedule_stop)


async def main() -> None:
    _prepare_runtime()
    from core.app import XiaoQingApp

    app        = XiaoQingApp(Path(__file__).resolve().parent)
    stop_event = asyncio.Event()

    def request_stop() -> None:
        if stop_event.is_set():
            return
        print("\n收到退出信号，正在优雅关闭...")
        stop_event.set()

    loop = asyncio.get_running_loop()
    _install_stop_signal_handlers(loop, request_stop)

    try:
        await app.start()
        await stop_event.wait()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            await asyncio.shield(app.stop())
            if app.shutdown_errors:
                # 资源仍未收敛时让进程返回失败，运行器可据此发现关闭异常。
                raise RuntimeError("XiaoQing shutdown incomplete; see runtime log")
        except asyncio.CancelledError:
            pass


def cli(argv: Sequence[str] | None = None) -> None:
    _build_parser().parse_args(argv)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    cli()
