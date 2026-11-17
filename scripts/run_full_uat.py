"""XiaoQing 上线前全量 UAT 的单入口。

默认用调用者当前环境中的解释器启动真实 ``python main.py --root <报告目录>/runtime``：
运行目录里是按本机配置改写的测试配置和密钥副本，插件数据、日志都落在那里，仓库里的
``config/`` 不改动。依次运行 WebSocket/HTTP 命令矩阵、Core 压测和 CI 同款静态门禁，
最后优雅停机并删掉密钥副本。外部服务和会消耗模型额度的聊天质量测试必须显式开启。

相对路径参数（``--output``、``--scenario-fixtures``）按仓库根目录解析，从哪个目录调用都一样：

    python scripts/run_full_uat.py
    python scripts/run_full_uat.py --plan-only
    python scripts/run_full_uat.py --include-chat-quality
    python scripts/run_full_uat.py --include-external \
        --scenario-fixtures tests/command_scenario_fixtures.local.json

中途被强制终止时，报告目录 ``runtime/config/secrets.json`` 里的密钥副本可能留下，手动删掉即可。

本脚本不导入 ``core`` / ``plugins`` 的代码：被测代码出了问题，它仍要能停服务、写出报告。
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import math
import os
import shlex
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO
from urllib.parse import urlsplit, urlunsplit

PROJECT_ROOT          = Path(__file__).resolve().parents[1]
CONFIG_PATH           = PROJECT_ROOT / "config" / "config.json"
SECRETS_PATH          = PROJECT_ROOT / "config" / "secrets.json"
DEFAULT_REPORT_PARENT = PROJECT_ROOT / "test_reports" / "runs" / "project"
DEFAULT_ENDPOINT      = "http://127.0.0.1:12000/event"
PYTHON_COMMAND        = sys.executable
DEFAULT_PHASES        = (
    "ws-matrix",
    "http-matrix",
    "core-pressure",
    "compileall",
    "ruff",
    "format",
    "mypy",
    "pytest",
    "diff",
)
PHASE_CHOICES = frozenset((*DEFAULT_PHASES, "chat-quality"))
# 矩阵阶段名 -> 命令矩阵脚本的 --transport 取值
MATRIX_TRANSPORTS = {"ws-matrix": "websocket", "http-matrix": "http"}


class UATError(RuntimeError):
    """UAT 无法安全规划或执行。"""


@dataclass(frozen=True, slots=True)
class PhaseSpec:
    name            : str
    command         : tuple[str, ...]
    requires_service: bool
    description     : str


@dataclass(frozen=True, slots=True)
class PhaseResult:
    name            : str
    status          : str
    exit_code       : int | None
    duration_seconds: float
    log_path        : str | None
    command         : str
    detail          : str = ""


@dataclass(frozen=True, slots=True)
class LifecycleResult:
    generation      : int
    action          : str
    status          : str
    exit_code       : int | None
    duration_seconds: float
    stdout_log      : str
    stderr_log      : str
    detail          : str = ""


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_json_object(path: Path, *, label: str) -> dict[str, Any]:
    """读一个无重复键的 JSON 对象。"""

    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise UATError(f"{label}含重复 JSON 键 {key}: {path}")
            result[key] = value
        return result

    try:
        payload = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=reject_duplicate_keys
        )
    except (OSError, UnicodeError) as exc:
        raise UATError(f"无法读取{label}: {path}: {type(exc).__name__}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise UATError(f"{label}不是有效 JSON: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise UATError(f"{label}必须是 JSON 对象: {path}")
    return payload


def _load_inbound_token() -> str:
    payload = _read_json_object(SECRETS_PATH, label="运行密钥")
    token = payload.get("inbound_token")
    if not isinstance(token, str) or not token:
        raise UATError("config/secrets.json 缺少非空 inbound_token")
    return token


def _command_text(command: tuple[str, ...] | list[str]) -> str:
    if os.name == "nt":
        return subprocess.list2cmdline(list(command))
    return shlex.join(command)


def _sibling_endpoint(endpoint: str, name: str) -> str:
    """由 ``/event`` 入站地址推导同主机的 ``/<name>`` 地址（``/ws``、``/health``）。"""

    parsed = urlsplit(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise UATError(f"--endpoint 必须是 http(s) URL: {endpoint}")
    path = parsed.path
    if path.endswith("/event"):
        path = f"{path[: -len('/event')]}/{name}"
    elif not path or path == "/":
        path = f"/{name}"
    else:
        raise UATError(f"无法从 --endpoint 推导 /{name} 地址：路径必须是 /event: {endpoint}")
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _endpoint_host_port(endpoint: str) -> tuple[str, int]:
    parsed = urlsplit(endpoint)
    host   = parsed.hostname
    if host is None:
        raise UATError(f"URL 缺少主机: {endpoint}")
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise UATError(f"全量 UAT 只允许回环入站地址，当前为: {host}")
    port = parsed.port or (443 if parsed.scheme in {"https", "wss"} else 80)
    return host, port


def _port_is_open(host: str, port: int, *, timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _wait_for_port_release(host: str, port: int, *, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _port_is_open(host, port):
            return True
        time.sleep(0.1)
    return not _port_is_open(host, port)


def _selected_phases(raw: str | None, *, include_chat_quality: bool) -> tuple[str, ...]:
    selected = list(DEFAULT_PHASES if raw is None else (item.strip() for item in raw.split(",")))
    selected = [item for item in selected if item]
    if include_chat_quality and "chat-quality" not in selected:
        selected.insert(3, "chat-quality")
    unknown = set(selected) - PHASE_CHOICES
    if not selected or unknown:
        raise UATError(f"--phases 必须从 {sorted(PHASE_CHOICES)} 中选择，未知值: {sorted(unknown)}")
    if len(selected) != len(set(selected)):
        raise UATError("--phases 不能含重复阶段")
    order = (*DEFAULT_PHASES[:3], "chat-quality", *DEFAULT_PHASES[3:])
    return tuple(item for item in order if item in selected)


def _identity_block(output_dir: Path, index: int) -> tuple[int, int, int, int]:
    digest = hashlib.sha256(f"{output_dir}:{index}".encode()).digest()
    offset = int.from_bytes(digest[:3], "big") % 800_000
    return (
        980_000_000 + offset,
        981_000_000 + offset,
        982_000_000 + offset,
        983_000_000 + offset,
    )


def _matrix_phase(
    *,
    phase: str,
    args: argparse.Namespace,
    output_dir: Path,
    index: int,
) -> PhaseSpec:
    transport = MATRIX_TRANSPORTS[phase]
    user_id, group_id, scenario_user_id, scenario_group_id = _identity_block(output_dir, index)
    report_dir   = output_dir / "reports" / f"{transport}-command-matrix"
    dependencies = "local,external" if args.include_external else "local"
    command      = [
        PYTHON_COMMAND,
        str(PROJECT_ROOT / "scripts" / "run_command_matrix.py"),
        "--transport",
        transport,
        "--endpoint",
        args.endpoint,
        "--ws-endpoint",
        args.ws_endpoint,
        "--output",
        str(report_dir),
        "--risks",
        "read_only,isolated_state,privileged",
        "--dependencies",
        dependencies,
        "--allow-stateful",
        "--allow-privileged",
        "--user-id",
        str(user_id),
        "--group-id",
        str(group_id),
        "--scenario-user-id",
        str(scenario_user_id),
        "--scenario-group-id",
        str(scenario_group_id),
    ]
    if args.scenario_fixtures is not None:
        command.extend(("--scenario-fixtures", str(args.scenario_fixtures)))
    if args.matrix_plugins:
        command.extend(("--plugins", args.matrix_plugins))
    if args.matrix_codes:
        command.extend(("--codes", args.matrix_codes))
    if args.matrix_kinds:
        command.extend(("--kinds", args.matrix_kinds))
    label = "WebSocket" if transport == "websocket" else "HTTP"
    return PhaseSpec(
        name             = phase,
        command          = tuple(command),
        requires_service = True,
        description      = f"真实 {label} 命令目录、权限/场景拒绝与动态 CRUD/清理场景",
    )


def _build_phase_specs(args: argparse.Namespace, output_dir: Path) -> tuple[PhaseSpec, ...]:
    selected = _selected_phases(args.phases, include_chat_quality=args.include_chat_quality)
    specs: list[PhaseSpec] = []
    matrix_index           = 0
    for phase in selected:
        if phase in MATRIX_TRANSPORTS:
            matrix_index += 1
            specs.append(
                _matrix_phase(
                    phase      = phase,
                    args       = args,
                    output_dir = output_dir,
                    index      = matrix_index,
                )
            )
        elif phase == "core-pressure":
            specs.append(
                PhaseSpec(
                    "core-pressure",
                    (
                        PYTHON_COMMAND,
                        str(PROJECT_ROOT / "scripts" / "run_core_pressure.py"),
                        "--endpoint",
                        args.endpoint,
                        "--output",
                        str(output_dir / "reports" / "core-pressure.json"),
                    ),
                    True,
                    "真实 /event 顺序、并发、突发、同会话背压与恢复",
                )
            )
        elif phase == "chat-quality":
            specs.append(
                PhaseSpec(
                    "chat-quality",
                    (
                        PYTHON_COMMAND,
                        "-m",
                        "scripts.chat_eval",
                        "run",
                        "--driver",
                        "live",
                        "--set",
                        "quality",
                        "--no-judge",
                        "--calibrate",
                        "--gate",
                        "--out-dir",
                        str(output_dir / "reports" / "xiaoqing-chat-quality"),
                    ),
                    True,
                    "真实模型人设、参与度和长空档话题边界：另起一个隔离的 main.py（可能产生费用）",
                )
            )
        elif phase == "compileall":
            specs.append(
                PhaseSpec(
                    "compileall",
                    (
                        PYTHON_COMMAND,
                        "-m",
                        "compileall",
                        "-q",
                        "core",
                        "plugins",
                        "scripts",
                        "main.py",
                    ),
                    False,
                    "生产 Python 源码编译检查",
                )
            )
        elif phase == "format":
            specs.append(
                PhaseSpec(
                    "format",
                    (PYTHON_COMMAND, "scripts/format_code.py", "--check"),
                    False,
                    "项目格式门禁",
                )
            )
        elif phase == "ruff":
            specs.append(
                PhaseSpec(
                    "ruff",
                    (PYTHON_COMMAND, "-m", "ruff", "check", "."),
                    False,
                    "仓库 Ruff 门禁",
                )
            )
        elif phase == "mypy":
            specs.append(
                PhaseSpec(
                    "mypy",
                    (PYTHON_COMMAND, "-m", "mypy", "core", "plugins"),
                    False,
                    "Core/插件类型门禁",
                )
            )
        elif phase == "pytest":
            specs.append(
                PhaseSpec(
                    "pytest",
                    (
                        PYTHON_COMMAND,
                        "-m",
                        "pytest",
                        "-q",
                        "-n",
                        "auto",
                        "--cov=core",
                        "--cov=plugins",
                        "--cov-report=term-missing",
                    ),
                    False,
                    "CI 同款全量 pytest 与覆盖率门禁",
                )
            )
        elif phase == "diff":
            specs.extend(
                (
                    PhaseSpec(
                        "diff-unstaged",
                        ("git", "diff", "--check"),
                        False,
                        "未暂存改动 whitespace 门禁",
                    ),
                    PhaseSpec(
                        "diff-staged",
                        ("git", "diff", "--cached", "--check"),
                        False,
                        "已暂存改动 whitespace 门禁",
                    ),
                )
            )
    return tuple(specs)


class RuntimeRoot:
    """UAT 的运行目录：按本机配置写出测试配置，带一份密钥副本，结束时删掉副本。"""

    def __init__(self, output_dir: Path) -> None:
        self.path    = (output_dir / "runtime").resolve()
        self.secrets = self.path / "config" / "secrets.json"

    def __enter__(self) -> RuntimeRoot:
        config = _read_json_object(CONFIG_PATH, label="运行配置")
        config.update(
            {
                "data_root": str(self.path / "data"),
                # 关停是否干净靠读服务输出里 Core 的 shutdown.done 判断，所以控制台必须开着。
                "logging": {**(config.get("logging") or {}), "console": True, "file": False},
                # 命令矩阵需要本地入站端口；UAT 不连接生产 NapCat，也不在测试期间监听源码变化。
                "enable_inbound_server": True,
                "enable_ws_client"     : False,
                "enable_plugin_watcher": False,
                # 后台工作不经过命令矩阵：关闭插件定时任务、主动 HTTP 投递和默认通知群，
                # 跨过计划时间也不会调用真实账号或向生产群发送消息。
                "enable_plugin_schedules": False,
                "onebot_http_base"       : "",
                "default_group_ids"      : [],
            }
        )
        config_dir = self.path / "config"
        config_dir.mkdir(parents=True)
        (config_dir / "config.json").write_text(
            json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        try:
            self.secrets.write_bytes(SECRETS_PATH.read_bytes())
        except BaseException:
            self.secrets.unlink(missing_ok=True)
            raise
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.secrets.unlink(missing_ok=True)


# Windows 上主进程先退出后，taskkill /T 已找不到它的后代；用作业对象持有整棵进程树，
# 主进程退出与否都能一并回收继承了输出管道的子孙进程。
_WINDOWS_JOBS: dict[int, int] = {}


def _attach_windows_job(process: subprocess.Popen[Any]) -> None:
    if os.name != "nt":
        return
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.OpenProcess.restype      = wintypes.HANDLE
    job                               = kernel32.CreateJobObjectW(None, None)
    if not job:
        return
    process_all_access = 0x1F0FFF
    handle             = kernel32.OpenProcess(process_all_access, False, process.pid)
    if handle and kernel32.AssignProcessToJobObject(wintypes.HANDLE(job), wintypes.HANDLE(handle)):
        _WINDOWS_JOBS[process.pid] = job
    else:
        kernel32.CloseHandle(wintypes.HANDLE(job))
    if handle:
        kernel32.CloseHandle(wintypes.HANDLE(handle))


def _terminate_windows_job(process: subprocess.Popen[Any]) -> bool:
    job = _WINDOWS_JOBS.pop(process.pid, None)
    if job is None:
        return False
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    try:
        return bool(kernel32.TerminateJobObject(wintypes.HANDLE(job), 1))
    finally:
        kernel32.CloseHandle(wintypes.HANDLE(job))


def _terminate_process_tree(process: subprocess.Popen[Any]) -> None:
    if os.name == "nt" and _terminate_windows_job(process):
        return
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdin   = subprocess.DEVNULL,
            stdout  = subprocess.DEVNULL,
            stderr  = subprocess.DEVNULL,
            check   = False,
            timeout = 10,
        )
    else:
        # Windows 类型存根不暴露 POSIX 专属 API；运行时探测既保持跨平台，也避免
        # 为解释器或操作系统增加人为版本限制。
        kill_process_group = getattr(os, "killpg", None)
        sigkill            = getattr(signal, "SIGKILL", None)
        if callable(kill_process_group) and sigkill is not None:
            with contextlib.suppress(ProcessLookupError):
                kill_process_group(process.pid, sigkill)
    if process.poll() is None:
        process.kill()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired as exc:
        raise UATError(f"无法回收进程树: {process.pid}") from exc


def _popen_flags() -> dict[str, Any]:
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _run_logged_phase(spec: PhaseSpec, output_dir: Path, *, timeout: float) -> PhaseResult:
    log_path = output_dir / "logs" / f"{spec.name}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    started               = time.monotonic()
    exit_code: int | None = None
    detail                = ""
    environment           = os.environ.copy()
    environment.setdefault("PYTHONUTF8", "1")
    print(f"[RUN ] {spec.name}: {spec.description}", flush=True)
    with log_path.open("w", encoding="utf-8", errors="backslashreplace") as log:
        log.write(f"command: {_command_text(spec.command)}\n\n")
        log.flush()
        process = subprocess.Popen(
            list(spec.command),
            cwd    = PROJECT_ROOT,
            stdin  = subprocess.DEVNULL,
            stdout = log,
            stderr = subprocess.STDOUT,
            env    = environment,
            **_popen_flags(),
        )
        _attach_windows_job(process)
        try:
            exit_code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            detail = f"超过阶段超时 {timeout:.0f}s，已回收进程树"
            _terminate_process_tree(process)
            exit_code = process.returncode
        # 任何退出路径都先回收当前 runner 创建的子进程树，再把异常继续抛出。
        except BaseException:
            _terminate_process_tree(process)
            raise
    duration = round(time.monotonic() - started, 3)
    status   = "passed" if exit_code == 0 and not detail else "failed"
    print(f"[{'PASS' if status == 'passed' else 'FAIL'}] {spec.name} ({duration:.1f}s)")
    return PhaseResult(
        spec.name,
        status,
        exit_code,
        duration,
        str(log_path),
        _command_text(spec.command),
        detail,
    )


class ServiceController:
    """只管理本 runner 创建的 ``main.py`` 进程组。"""

    def __init__(
        self,
        output_dir: Path,
        *,
        runtime_root: Path,
        endpoint: str,
        token: str,
        expected_plugins: int,
        start_timeout: float,
        stop_timeout: float,
    ) -> None:
        self.output_dir                            = output_dir
        self.runtime_root                          = runtime_root
        self.endpoint                              = endpoint
        self.health_url                            = _sibling_endpoint(endpoint, "health")
        self.token                                 = token
        self.expected_plugins                      = expected_plugins
        self.start_timeout                         = start_timeout
        self.stop_timeout                          = stop_timeout
        self.generation                            = 0
        self.process: subprocess.Popen[Any] | None = None
        self.stdout_handle: BinaryIO | None        = None
        self.stderr_handle: BinaryIO | None        = None
        self.stdout_path: Path | None              = None
        self.stderr_path: Path | None              = None

    def _health(self) -> tuple[bool, str]:
        request = urllib.request.Request(
            self.health_url,
            headers = {"Authorization": f"Bearer {self.token}"},
            method  = "GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=1.5) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (OSError, urllib.error.URLError, json.JSONDecodeError):
            return False, "health unavailable"
        if not isinstance(payload, dict):
            return False, "health payload is not an object"
        loaded = payload.get("plugins_loaded")
        if response.status != 200 or payload.get("status") != "ok":
            return False, f"health status={response.status}/{payload.get('status')}"
        if loaded != self.expected_plugins:
            return False, f"plugins_loaded={loaded}, expected={self.expected_plugins}"
        return True, f"plugins_loaded={loaded}"

    def start(self) -> LifecycleResult:
        if self.process is not None and self.process.poll() is None:
            raise UATError("服务已经由当前 runner 启动")
        self.generation += 1
        logs = self.output_dir / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        self.stdout_path   = logs / f"service-{self.generation}.stdout.log"
        self.stderr_path   = logs / f"service-{self.generation}.stderr.log"
        self.stdout_handle = self.stdout_path.open("wb")
        self.stderr_handle = self.stderr_path.open("wb")
        started            = time.monotonic()
        self.process       = subprocess.Popen(
            [PYTHON_COMMAND, str(PROJECT_ROOT / "main.py"), "--root", str(self.runtime_root)],
            cwd    = PROJECT_ROOT,
            stdin  = subprocess.DEVNULL,
            stdout = self.stdout_handle,
            stderr = self.stderr_handle,
            env    = {**os.environ, "PYTHONUTF8": "1"},
            **_popen_flags(),
        )
        _attach_windows_job(self.process)
        deadline = time.monotonic() + self.start_timeout
        detail   = "health timeout"
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                detail = f"main.py 提前退出，exit_code={self.process.returncode}"
                break
            healthy, detail = self._health()
            if healthy:
                duration = round(time.monotonic() - started, 3)
                print(f"[PASS] service-start-{self.generation} ({duration:.1f}s, {detail})")
                return LifecycleResult(
                    self.generation,
                    "start",
                    "passed",
                    None,
                    duration,
                    str(self.stdout_path),
                    str(self.stderr_path),
                    detail,
                )
            time.sleep(0.25)
        duration = round(time.monotonic() - started, 3)
        print(f"[FAIL] service-start-{self.generation}: {detail}")
        self._force_stop()
        return LifecycleResult(
            self.generation,
            "start",
            "failed",
            self.process.returncode if self.process else None,
            duration,
            str(self.stdout_path),
            str(self.stderr_path),
            detail,
        )

    def _close_logs(self) -> None:
        for handle in (self.stdout_handle, self.stderr_handle):
            if handle is not None:
                handle.close()
        self.stdout_handle = None
        self.stderr_handle = None

    def _force_stop(self) -> None:
        if self.process is not None:
            _terminate_process_tree(self.process)
        self._close_logs()

    def stop(self) -> LifecycleResult:
        if self.process is None or self.stdout_path is None or self.stderr_path is None:
            raise UATError("当前没有可停止的 UAT 服务")
        started      = time.monotonic()
        timed_out    = False
        signal_error = ""
        if self.process.poll() is None:
            try:
                if os.name == "nt":
                    self.process.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    self.process.send_signal(signal.SIGINT)
                self.process.wait(timeout=self.stop_timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                _terminate_process_tree(self.process)
            # 平台信号 API 的失败类型不统一；此边界必须无条件转入进程树强制回收。
            except Exception as exc:  # noqa: BLE001
                signal_error = f"{type(exc).__name__}: {exc}"
                _terminate_process_tree(self.process)
        exit_code = self.process.returncode
        self._close_logs()
        stdout_text = self.stdout_path.read_text(encoding="utf-8", errors="replace")
        stderr_text = self.stderr_path.read_text(encoding="utf-8", errors="replace")
        combined = f"{stdout_text}\n{stderr_text}"
        host, port = _endpoint_host_port(self.endpoint)
        released = _wait_for_port_release(host, port, timeout=10)
        # 只认 Core 生命周期这一行：插件关停时也会记自己的 shutdown.done。
        graceful = "core.app_lifecycle: shutdown.done" in combined
        passed   = not timed_out and not signal_error and exit_code == 0 and graceful and released
        detail   = (
            f"signal={'CTRL_BREAK_EVENT' if os.name == 'nt' else 'SIGINT'}, "
            f"shutdown_complete={graceful}, port_released={released}, timed_out={timed_out}, "
            f"signal_error={signal_error or 'none'}"
        )
        duration = round(time.monotonic() - started, 3)
        print(f"[{'PASS' if passed else 'FAIL'}] service-stop-{self.generation} ({detail})")
        result = LifecycleResult(
            self.generation,
            "stop",
            "passed" if passed else "failed",
            exit_code,
            duration,
            str(self.stdout_path),
            str(self.stderr_path),
            detail,
        )
        self.process = None
        return result


def _write_repo_snapshot(output_dir: Path) -> dict[str, Any]:
    status = subprocess.run(
        ["git", "status", "--porcelain=v1"],
        cwd            = PROJECT_ROOT,
        text           = True,
        encoding       = "utf-8",
        errors         = "replace",
        capture_output = True,
        check          = False,
    )
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd            = PROJECT_ROOT,
        text           = True,
        encoding       = "utf-8",
        errors         = "replace",
        capture_output = True,
        check          = False,
    )
    (output_dir / "git-status-before.txt").write_text(status.stdout, encoding="utf-8")
    return {
        "head"            : head.stdout.strip() if head.returncode == 0 else None,
        "dirty_entries"   : len(status.stdout.splitlines()),
        "status_exit_code": status.returncode,
    }


def _mark_skipped(spec: PhaseSpec, detail: str) -> PhaseResult:
    return PhaseResult(
        spec.name,
        "skipped",
        None,
        0.0,
        None,
        _command_text(spec.command),
        detail,
    )


def _mark_runner_failure(spec: PhaseSpec, detail: str) -> PhaseResult:
    return PhaseResult(
        spec.name,
        "failed",
        None,
        0.0,
        None,
        _command_text(spec.command),
        detail,
    )


def _run_static_phase(
    spec: PhaseSpec,
    output_dir: Path,
    *,
    timeout: float,
) -> tuple[PhaseResult, str | None]:
    """运行一个静态门禁，并把 runner 自身异常转成明确失败结果。"""

    try:
        return _run_logged_phase(spec, output_dir, timeout=timeout), None
    # 静态 runner 的职责是把任意内部失败写进最终报告，而不是让报告本身丢失。
    except Exception as exc:  # noqa: BLE001
        detail = f"{spec.name}: {type(exc).__name__}: {exc}"
        return _mark_runner_failure(spec, "阶段 runner 异常"), detail


def _execute_service_phases(
    specs: tuple[PhaseSpec, ...],
    controller: ServiceController,
    output_dir: Path,
    *,
    phase_timeout: float,
) -> tuple[list[PhaseResult], list[LifecycleResult]]:
    phases   : list[PhaseResult]     = []
    lifecycle: list[LifecycleResult] = []
    # 回收范围从创建服务进程开始：启动等待期间的中断或异常同样要先停服，
    # 外层再删掉密钥副本。
    try:
        started = controller.start()
        lifecycle.append(started)
        if started.status != "passed":
            phases.extend(_mark_skipped(spec, "服务启动失败") for spec in specs)
            return phases, lifecycle

        for index, spec in enumerate(specs):
            if controller.process is None or controller.process.poll() is not None:
                phases.extend(_mark_skipped(item, "服务在阶段前意外退出") for item in specs[index:])
                break
            try:
                phases.append(_run_logged_phase(spec, output_dir, timeout=phase_timeout))
            except KeyboardInterrupt:
                phases.append(_mark_runner_failure(spec, "用户中断"))
                phases.extend(_mark_skipped(item, "用户中断") for item in specs[index + 1 :])
                break
            # 单阶段 runner 失败应留痕，外层 finally 仍负责停止本次创建的服务。
            except Exception as exc:  # noqa: BLE001
                phases.append(_mark_runner_failure(spec, f"{type(exc).__name__}: {exc}"))
            has_more = index + 1 < len(specs)
            if spec.name in MATRIX_TRANSPORTS and has_more:
                stopped = controller.stop()
                lifecycle.append(stopped)
                if stopped.status != "passed":
                    phases.extend(
                        _mark_skipped(item, "矩阵后优雅重启失败") for item in specs[index + 1 :]
                    )
                    break
                restarted = controller.start()
                lifecycle.append(restarted)
                if restarted.status != "passed":
                    phases.extend(
                        _mark_skipped(item, "矩阵后服务重启失败") for item in specs[index + 1 :]
                    )
                    break
    finally:
        if controller.process is not None:
            lifecycle.append(controller.stop())
    return phases, lifecycle


def _report_markdown(report: dict[str, Any]) -> str:
    verdict     = "通过" if report["gate_passed"] else "未通过"
    phase_lines = [
        "| 阶段 | 状态 | 退出码 | 耗时(s) | 日志 |",
        "|---|---:|---:|---:|---|",
    ]
    for row in report["phases"]:
        log = f"`{row['log_path']}`" if row["log_path"] else "-"
        phase_lines.append(
            f"| {row['name']} | {row['status']} | {row['exit_code']} | "
            f"{row['duration_seconds']} | {log} |"
        )
    lifecycle_lines = [
        f"- generation {row['generation']} {row['action']}: **{row['status']}**; {row['detail']}"
        for row in report["lifecycle"]
    ]
    notes = report["coverage_boundary"]
    return "\n".join(
        (
            "# XiaoQing 全量 UAT 报告",
            "",
            f"结论：**{verdict}**。",
            "",
            "## 执行上下文",
            "",
            f"- 开始/结束：`{report['started_at']}` / `{report['finished_at']}`",
            f"- Python：`{report['python']['command']}`",
            (
                f"- Git HEAD：`{report['repository']['head']}`；"
                f"运行前 dirty entries：{report['repository']['dirty_entries']}"
            ),
            "",
            "## 阶段",
            "",
            *phase_lines,
            "",
            "## 服务生命周期",
            "",
            *(lifecycle_lines or ["- 本次只运行静态阶段，未启动服务。"]),
            "",
            "## 隔离与收口",
            "",
            f"- 测试数据隔离：{report['integrity']['isolated_data']}。",
            f"- config 未改动：{report['integrity']['config_unchanged']}。",
            f"- secrets 哈希未变：{report['integrity']['secrets_unchanged']}。",
            f"- 入站端口释放：{report['integrity']['inbound_port_released']}。",
            "",
            "## 覆盖边界",
            "",
            f"- 外部依赖场景：{notes['external']}。",
            f"- 小青真实模型质量：{notes['chat_quality']}。",
            (
                "- 有状态/高权限命令只由带前置条件、动态 ID 和 cleanup 的业务场景执行；"
                "通用矩阵不会把静态样例冒充 CRUD 通过。"
            ),
            (
                "- WebSocket 与 HTTP 矩阵之间优雅重启，清空内存会话，"
                "避免 Jupyter/QingSSH 等交互态串扰。"
            ),
            "",
            "## 复跑",
            "",
            "```bash",
            "python scripts/run_full_uat.py",
            "# 只复测失败阶段示例",
            "python scripts/run_full_uat.py --phases ws-matrix,pytest,diff",
            "```",
            "",
        )
    )


def _write_final_report(output_dir: Path, report: dict[str, Any]) -> None:
    report_dir = output_dir / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "full-uat-summary.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (report_dir / "FULL_UAT_REPORT.md").write_text(
        _report_markdown(report),
        encoding="utf-8",
    )


def _plan_only(specs: tuple[PhaseSpec, ...], args: argparse.Namespace, output_dir: Path) -> int:
    plan = {
        "output"              : str(output_dir),
        "python"              : PYTHON_COMMAND,
        "isolated_data_root"  : str(output_dir / "runtime" / "data"),
        "include_external"    : args.include_external,
        "include_chat_quality": any(spec.name == "chat-quality" for spec in specs),
        "phases"              : [
            {
                "name"            : spec.name,
                "requires_service": spec.requires_service,
                "description"     : spec.description,
                "command"         : _command_text(spec.command),
            }
            for spec in specs
        ],
    }
    print(json.dumps(plan, ensure_ascii=False, indent=2))
    return 0


def _preflight_service_ports(endpoint: str, config: dict[str, Any]) -> None:
    host, port = _endpoint_host_port(endpoint)
    if _port_is_open(host, port):
        raise UATError(f"入站端口 {host}:{port} 已被占用；runner 不会接管非本次创建的服务")
    plugins = config.get("plugins")
    pendo   = plugins.get("pendo") if isinstance(plugins, dict) else None
    if not isinstance(pendo, dict) or pendo.get("web_enabled", True) is False:
        return
    pendo_host = str(pendo.get("web_host", "127.0.0.1"))
    probe_host = "127.0.0.1" if pendo_host in {"0.0.0.0", "::"} else pendo_host
    pendo_port = pendo.get("web_port", 12001)
    if type(pendo_port) is not int:
        raise UATError("plugins.pendo.web_port 必须是整数")
    if _port_is_open(probe_host, pendo_port):
        raise UATError(f"Pendo Web 端口 {probe_host}:{pendo_port} 已被占用")


def _execute(args: argparse.Namespace) -> int:
    stamp      = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    output_dir = (
        args.output.resolve()
        if args.output is not None
        else (DEFAULT_REPORT_PARENT / f"full-uat-{stamp}-{os.getpid()}").resolve()
    )
    if args.ws_endpoint is None:
        args.ws_endpoint = _sibling_endpoint(args.endpoint, "ws")
    if args.scenario_fixtures is None:
        candidate = PROJECT_ROOT / "tests" / "command_scenario_fixtures.local.json"
        if candidate.is_file():
            args.scenario_fixtures = candidate
    if args.include_external and args.scenario_fixtures is None:
        raise UATError("--include-external 需要 --scenario-fixtures")
    specs = _build_phase_specs(args, output_dir)
    if args.plan_only:
        return _plan_only(specs, args, output_dir)
    if output_dir.exists():
        raise UATError(f"输出目录已存在，拒绝覆盖: {output_dir}")
    output_dir.mkdir(parents=True)

    started_at = _now_iso()
    repository = _write_repo_snapshot(output_dir)
    secrets_before = _sha256_file(SECRETS_PATH)
    config_before = _sha256_file(CONFIG_PATH)
    phase_results    : list[PhaseResult] = []
    lifecycle_results: list[LifecycleResult] = []
    fatal_errors     : list[str] = []
    service_specs = tuple(spec for spec in specs if spec.requires_service)
    static_specs = tuple(spec for spec in specs if not spec.requires_service)

    if service_specs:
        config = _read_json_object(CONFIG_PATH, label="运行配置")
        try:
            _preflight_service_ports(args.endpoint, config)
            with RuntimeRoot(output_dir) as runtime:
                controller = ServiceController(
                    output_dir,
                    runtime_root     = runtime.path,
                    endpoint         = args.endpoint,
                    token            = _load_inbound_token(),
                    expected_plugins = len(tuple((PROJECT_ROOT / "plugins").glob("*/plugin.json"))),
                    start_timeout    = args.start_timeout,
                    stop_timeout     = args.stop_timeout,
                )
                service_phases, lifecycle = _execute_service_phases(
                    service_specs,
                    controller,
                    output_dir,
                    phase_timeout=args.phase_timeout,
                )
                phase_results.extend(service_phases)
                lifecycle_results.extend(lifecycle)
        except KeyboardInterrupt:
            fatal_errors.append("用户中断")
        # 服务编排的最外层故障必须进入报告；with/finally 已先完成进程回收、删掉密钥副本。
        except Exception as exc:  # noqa: BLE001
            fatal_errors.append(f"{type(exc).__name__}: {exc}")
            covered = {row.name for row in phase_results}
            phase_results.extend(
                _mark_skipped(spec, "服务阶段发生致命错误")
                for spec in service_specs
                if spec.name not in covered
            )

    config_unchanged = _sha256_file(CONFIG_PATH) == config_before
    if not config_unchanged:
        fatal_errors.append("config/config.json 在 UAT 期间被改动")

    completed_static = 0
    try:
        for spec in static_specs:
            result, runner_error = _run_static_phase(
                spec,
                output_dir,
                timeout=args.phase_timeout,
            )
            phase_results.append(result)
            completed_static += 1
            if runner_error:
                fatal_errors.append(runner_error)
    except KeyboardInterrupt:
        fatal_errors.append("用户在静态阶段中断")
        remaining = static_specs[completed_static:]
        phase_results.extend(_mark_skipped(item, "用户中断") for item in remaining)

    host, port = _endpoint_host_port(args.endpoint)
    integrity = {
        "isolated_data"        : bool(service_specs),
        "config_unchanged"     : config_unchanged,
        "secrets_unchanged"    : _sha256_file(SECRETS_PATH) == secrets_before,
        "inbound_port_released": not service_specs or not _port_is_open(host, port),
        "config_sha256_before" : config_before,
        "config_sha256_after"  : _sha256_file(CONFIG_PATH),
        "secrets_sha256_before": secrets_before,
        "secrets_sha256_after" : _sha256_file(SECRETS_PATH),
    }
    all_phases_passed    = all(row.status == "passed" for row in phase_results)
    all_lifecycle_passed = all(row.status == "passed" for row in lifecycle_results)
    gate_passed          = (
        not fatal_errors
        and all_phases_passed
        and all_lifecycle_passed
        and integrity["config_unchanged"]
        and integrity["secrets_unchanged"]
        and integrity["inbound_port_released"]
    )
    report = {
        "schema_version"   : 1,
        "started_at"       : started_at,
        "finished_at"      : _now_iso(),
        "gate_passed"      : gate_passed,
        "output_dir"       : str(output_dir),
        "python"           : {"command": PYTHON_COMMAND, "version": sys.version},
        "repository"       : repository,
        "phases"           : [asdict(row) for row in phase_results],
        "lifecycle"        : [asdict(row) for row in lifecycle_results],
        "integrity"        : integrity,
        "fatal_errors"     : fatal_errors,
        "coverage_boundary": {
            "external": "included" if args.include_external else "not executed (opt-in)",
            "chat_quality": (
                "included"
                if any(row.name == "chat-quality" for row in phase_results)
                else "not executed (opt-in)"
            ),
        },
    }
    _write_final_report(output_dir, report)
    print(f"报告目录: {output_dir}")
    print(f"最终门禁: {'PASS' if gate_passed else 'FAIL'}")
    return 0 if gate_passed else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description     = __doc__,
        formatter_class = argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--ws-endpoint")
    parser.add_argument(
        "--phases",
        help=f"逗号分隔的阶段；默认 {','.join(DEFAULT_PHASES)}",
    )
    parser.add_argument("--include-external", action="store_true")
    parser.add_argument("--include-chat-quality", action="store_true")
    parser.add_argument("--scenario-fixtures", type=Path)
    parser.add_argument("--matrix-plugins", help="只执行指定的逗号分隔插件，便于定点复测")
    parser.add_argument("--matrix-codes", help="只执行指定的逗号分隔稳定命令码前缀")
    parser.add_argument("--matrix-kinds", help="只执行指定的逗号分隔 matrix 用例类型")
    parser.add_argument("--plan-only", action="store_true")
    parser.add_argument("--start-timeout", type=float, default=120.0)
    parser.add_argument("--stop-timeout", type=float, default=60.0)
    parser.add_argument("--phase-timeout", type=float, default=7_200.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser   = build_parser()
    args     = parser.parse_args(argv)
    timeouts = (args.start_timeout, args.stop_timeout, args.phase_timeout)
    if any(not math.isfinite(value) or value <= 0 for value in timeouts):
        parser.error("超时参数必须为正数")
    try:
        return _execute(args)
    except UATError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    os.chdir(PROJECT_ROOT)
    raise SystemExit(main())
