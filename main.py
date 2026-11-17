import argparse
import asyncio
import logging
import signal
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

logger = logging.getLogger(__name__)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog        = "xiaoqing",
        description = "XiaoQing QQ Bot framework",
    )
    parser.add_argument(
        "--root",
        type    = Path,
        default = None,
        help    = (
            "运行目录：配置读 ROOT/config/，数据和日志写到 ROOT/data/、ROOT/logs/。"
            "默认在源码目录里运行时是源码目录，装成包以后是当前目录。"
        ),
    )
    return parser


def _default_root() -> Path:
    """源码目录里运行就用源码目录；装成 wheel 后 main.py 在 site-packages 里，改用当前目录。"""

    here = Path(__file__).resolve().parent
    return here if (here / "pyproject.toml").is_file() else Path.cwd()


def _plugins_dir() -> Path:
    """插件代码所在的目录：源码目录下的 plugins/，或安装环境里的 plugins 包。"""

    import plugins

    return Path(plugins.__file__).resolve().parent


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


async def main(root: Path | None = None) -> None:
    _prepare_runtime()
    from core.app import XiaoQingApp

    app = XiaoQingApp((root or _default_root()).resolve(), plugins_dir=_plugins_dir())
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
    """进程入口：读配置之前先装好只写控制台的日志，启动或运行失败时记一条事件、以退出码 1 结束。

    失败只按日志规则记异常类型、位置和安全回溯，不把异常消息（可能带配置里的地址、凭据）
    印到标准输出或标准错误。
    """

    args = _build_parser().parse_args(argv)
    from core.log import LogEvent, bootstrap_logging

    bootstrap_logging()
    try:
        asyncio.run(main(args.root))
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        logger.critical(LogEvent("process.failed", error=exc), exc_info=exc)
        raise SystemExit(1) from None


if __name__ == "__main__":
    cli()
