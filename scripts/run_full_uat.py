"""XiaoQing 上线前全量 UAT 的单入口。

默认用调用者当前环境中的解释器启动真实 ``python main.py``，把插件数据重定向到本次报告目录，
依次运行 WebSocket/HTTP 命令矩阵、Core 压测和 CI 同款静态门禁，最后优雅停机并
逐字节恢复 ``config/config.json``。外部服务和会消耗模型额度的聊天质量测试必须
显式开启。

推荐从 Git Bash、macOS 或 Linux 的统一 Bash 入口调用：

    bash scripts/run_full_uat.sh
    bash scripts/run_full_uat.sh --plan-only
    bash scripts/run_full_uat.sh --include-chat-quality
    bash scripts/run_full_uat.sh --include-external \
        --scenario-fixtures tests/command_scenario_fixtures.local.json

中途被强制终止且配置未恢复时，先确认没有 XiaoQing 服务仍在运行，再执行：

    bash scripts/run_full_uat.sh --recover
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
import stat
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
LOCK_PATH             = DEFAULT_REPORT_PARENT / ".full-uat.lock"
DEFAULT_ENDPOINT      = "http://127.0.0.1:12000/event"
PYTHON_COMMAND        = "python"
DEFAULT_PHASES        = (
    "ws-matrix",
    "http-matrix",
    "core-pressure",
    "compileall",
    "ruff",
    "mypy",
    "pytest",
    "diff",
)
PHASE_CHOICES = frozenset((*DEFAULT_PHASES, "chat-quality"))
MATRIX_PHASES = frozenset({"ws-matrix", "http-matrix"})


class UATError(RuntimeError):
    """UAT 无法安全规划或执行。"""


@dataclass(frozen=True, slots=True)
class PhaseSpec:
    name: str
    command: tuple[str, ...]
    requires_service: bool
    description: str


@dataclass(frozen=True, slots=True)
class PhaseResult:
    name: str
    status: str
    exit_code: int | None
    duration_seconds: float
    log_path: str | None
    command: str
    detail: str = ""


@dataclass(frozen=True, slots=True)
class LifecycleResult:
    generation: int
    action: str
    status: str
    exit_code: int | None
    duration_seconds: float
    stdout_log: str
    stderr_log: str
    detail: str = ""


@dataclass(frozen=True, slots=True)
class _LegacyDataMove:
    """UAT 期间临时隐藏的一份源码树旧数据目录。"""

    source: Path
    hidden: Path


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _decode_json_object(text: str, *, label: str, source: Path) -> dict[str, Any]:
    """解析无重复键的 JSON 对象，供配置、密钥和恢复锁共用。"""

    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise UATError(f"{label}含重复 JSON 键 {key}: {source}")
            result[key] = value
        return result

    try:
        payload = json.loads(text, object_pairs_hook=reject_duplicate_keys)
    except json.JSONDecodeError as exc:
        raise UATError(f"{label}不是有效 JSON: {source}: {exc}") from exc
    if not isinstance(payload, dict):
        raise UATError(f"{label}必须是 JSON 对象: {source}")
    return payload


def _read_json_object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise UATError(f"无法读取{label}: {path}: {type(exc).__name__}: {exc}") from exc
    return _decode_json_object(text, label=label, source=path)


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


def _derive_ws_endpoint(endpoint: str) -> str:
    parsed = urlsplit(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise UATError(f"--endpoint 必须是 http(s) URL: {endpoint}")
    path = parsed.path
    if path.endswith("/event"):
        path = f"{path[: -len('/event')]}/ws"
    elif not path or path == "/":
        path = "/ws"
    else:
        raise UATError("无法从 --endpoint 推导 WebSocket 路径；请显式提供 --ws-endpoint")
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _health_endpoint(endpoint: str) -> str:
    parsed = urlsplit(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise UATError(f"--endpoint 必须是 http(s) URL: {endpoint}")
    path = parsed.path
    if path.endswith("/event"):
        path = f"{path[: -len('/event')]}/health"
    elif not path or path == "/":
        path = "/health"
    else:
        raise UATError("--endpoint 路径必须是 /event")
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
    transport: str,
    args: argparse.Namespace,
    output_dir: Path,
    index: int,
) -> PhaseSpec:
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
        name             = f"{transport}-matrix",
        command          = tuple(command),
        requires_service = True,
        description      = f"真实 {label} 命令目录、权限/场景拒绝与动态 CRUD/清理场景",
    )


def _build_phase_specs(args: argparse.Namespace, output_dir: Path) -> tuple[PhaseSpec, ...]:
    selected = _selected_phases(args.phases, include_chat_quality=args.include_chat_quality)
    specs: list[PhaseSpec] = []
    matrix_index           = 0
    for phase in selected:
        if phase in MATRIX_PHASES:
            matrix_index += 1
            transport = "websocket" if phase == "ws-matrix" else "http"
            specs.append(
                _matrix_phase(
                    transport  = transport,
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
                        str(PROJECT_ROOT / "scripts" / "run_xiaoqing_chat_quality.py"),
                        "--endpoint",
                        args.endpoint,
                        "--chat-data-dir",
                        str(output_dir / "data" / "xiaoqing_chat"),
                        "--output",
                        str(output_dir / "reports" / "xiaoqing-chat-quality.json"),
                    ),
                    True,
                    "真实模型人设、参与度和长空档话题边界（可能产生费用）",
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


def _is_link_like(metadata: os.stat_result) -> bool:
    """识别符号链接和 Windows 重解析点，避免重命名不透明目标。"""

    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    attributes   = int(getattr(metadata, "st_file_attributes", 0))
    return stat.S_ISLNK(metadata.st_mode) or bool(attributes & reparse_flag)


def _discover_legacy_data_moves(token: str) -> tuple[_LegacyDataMove, ...]:
    """列出 UAT 启动前必须临时隐藏的旧版插件数据目录。"""

    plugins_dir = PROJECT_ROOT / "plugins"
    try:
        plugin_entries = sorted(plugins_dir.iterdir(), key=lambda path: path.name)
    except OSError as exc:
        raise UATError(f"无法枚举插件目录: {type(exc).__name__}: {exc}") from exc

    moves: list[_LegacyDataMove] = []
    for plugin_dir in plugin_entries:
        try:
            plugin_metadata = plugin_dir.lstat()
        except OSError as exc:
            raise UATError(f"无法检查插件目录 {plugin_dir}: {type(exc).__name__}") from exc
        if _is_link_like(plugin_metadata) or not stat.S_ISDIR(plugin_metadata.st_mode):
            continue
        try:
            manifest_metadata = (plugin_dir / "plugin.json").lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise UATError(f"无法检查插件清单 {plugin_dir}: {type(exc).__name__}") from exc
        if _is_link_like(manifest_metadata) or not stat.S_ISREG(manifest_metadata.st_mode):
            continue

        source = plugin_dir / "data"
        try:
            source_metadata = source.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise UATError(f"无法检查旧插件数据 {source}: {type(exc).__name__}") from exc
        if _is_link_like(source_metadata) or not stat.S_ISDIR(source_metadata.st_mode):
            raise UATError(f"旧插件数据不是普通目录，拒绝临时移动: {source}")

        hidden = plugin_dir / f".uat-hidden-data-{token}"
        try:
            hidden.lstat()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise UATError(f"无法检查 UAT 隐藏路径 {hidden}: {type(exc).__name__}") from exc
        else:
            raise UATError(f"UAT 隐藏路径已存在，拒绝覆盖: {hidden}")
        moves.append(_LegacyDataMove(source=source.resolve(), hidden=hidden.resolve()))
    return tuple(moves)


def _hide_legacy_data(moves: tuple[_LegacyDataMove, ...]) -> None:
    """用同目录原子重命名隐藏旧数据，避免隔离启动触发真实迁移。"""

    try:
        for move in moves:
            move.source.rename(move.hidden)
    except OSError as exc:
        raise UATError(
            f"无法临时隐藏旧插件数据 {move.source}: {type(exc).__name__}: {exc}"
        ) from exc


def _ordinary_directory_exists(path: Path) -> bool:
    """返回目录是否存在；链接、文件和无法检查的路径一律拒绝。"""

    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise UATError(f"无法检查旧插件数据路径 {path}: {type(exc).__name__}") from exc
    if _is_link_like(metadata) or not stat.S_ISDIR(metadata.st_mode):
        raise UATError(f"旧插件数据路径不是普通目录: {path}")
    return True


def _restore_legacy_data(moves: tuple[_LegacyDataMove, ...]) -> None:
    """尽最大努力逐项恢复 UAT 隐藏目录；任何冲突都保留锁并失败关闭。"""

    errors: list[str] = []
    for move in reversed(moves):
        # 每项隔离才能在一个目录损坏时继续恢复其余目录，避免扩大中断面。
        try:
            source_exists = _ordinary_directory_exists(move.source)
            hidden_exists = _ordinary_directory_exists(move.hidden)
            if source_exists and hidden_exists:
                errors.append(f"源与隐藏目录同时存在: {move.source}")
            elif hidden_exists:
                move.hidden.rename(move.source)
            elif not source_exists:
                errors.append(f"源与隐藏目录均不存在: {move.source}")
        except (OSError, UATError) as exc:  # noqa: PERF203
            errors.append(f"{move.source}: {type(exc).__name__}: {exc}")
    if errors:
        raise UATError("恢复旧插件数据失败: " + "; ".join(errors))


def _legacy_moves_from_lock(lock: dict[str, Any]) -> tuple[_LegacyDataMove, ...]:
    """从恢复锁读取并约束移动目标，禁止锁文件指向仓库外路径。"""

    raw_moves = lock.get("legacy_data_moves", [])
    if not isinstance(raw_moves, list):
        raise UATError("UAT 恢复锁的 legacy_data_moves 必须是数组")
    try:
        plugins_dir = (PROJECT_ROOT / "plugins").resolve(strict=True)
    except OSError as exc:
        raise UATError("无法解析当前插件目录") from exc

    moves: list[_LegacyDataMove] = []
    seen_sources: set[Path]      = set()
    for raw in raw_moves:
        if not isinstance(raw, dict):
            raise UATError("UAT 恢复锁含无效旧数据移动记录")
        source_text = raw.get("source")
        hidden_text = raw.get("hidden")
        if not isinstance(source_text, str) or not isinstance(hidden_text, str):
            raise UATError("UAT 恢复锁的旧数据路径必须是字符串")
        source = Path(source_text)
        hidden = Path(hidden_text)
        try:
            plugin_dir = source.parent.resolve(strict=True)
            hidden_parent = hidden.parent.resolve(strict=True)
        except OSError as exc:
            raise UATError("UAT 恢复锁指向不存在的插件目录") from exc
        if (
            source.name != "data"
            or plugin_dir.parent != plugins_dir
            or hidden_parent != plugin_dir
            or not hidden.name.startswith(".uat-hidden-data-")
        ):
            raise UATError(f"UAT 恢复锁指向意外旧数据路径: {source}")
        normalized_source = plugin_dir / "data"
        normalized_hidden = plugin_dir / hidden.name
        if normalized_source in seen_sources:
            raise UATError(f"UAT 恢复锁重复登记旧数据路径: {normalized_source}")
        seen_sources.add(normalized_source)
        moves.append(_LegacyDataMove(source=normalized_source, hidden=normalized_hidden))
    return tuple(moves)


class RuntimeIsolation:
    """隔离数据与运行入口，并在退出时逐字节恢复生产配置。"""

    def __init__(self, output_dir: Path) -> None:
        self.output_dir                                     = output_dir
        self.backup_path                                    = output_dir / "config.original.json"
        self.original_bytes                                 = b""
        self.original_hash                                  = ""
        self.legacy_data_moves: tuple[_LegacyDataMove, ...] = ()
        self.restored                                       = False

    def __enter__(self) -> RuntimeIsolation:
        if LOCK_PATH.exists():
            raise UATError(f"发现未收口的 UAT 锁: {LOCK_PATH}；确认服务已停止后运行 --recover")
        self.original_bytes = CONFIG_PATH.read_bytes()
        self.original_hash  = _sha256_bytes(self.original_bytes)
        _atomic_write(self.backup_path, self.original_bytes)
        move_token             = f"{os.getpid()}-{time.time_ns()}"
        self.legacy_data_moves = _discover_legacy_data_moves(move_token)
        lock_payload           = {
            "schema_version": 2,
            "pid": os.getpid(),
            "created_at": _now_iso(),
            "project_root": str(PROJECT_ROOT),
            "config_path": str(CONFIG_PATH),
            "backup_path": str(self.backup_path),
            "original_sha256": self.original_hash,
            "legacy_data_moves": [
                {"source": str(move.source), "hidden": str(move.hidden)}
                for move in self.legacy_data_moves
            ],
        }
        LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(LOCK_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        except FileExistsError as exc:
            raise UATError(f"另一个全量 UAT 已持有锁: {LOCK_PATH}") from exc
        with os.fdopen(descriptor, "wb") as handle:
            handle.write((json.dumps(lock_payload, ensure_ascii=False, indent=2) + "\n").encode())
            handle.flush()
            os.fsync(handle.fileno())

        try:
            _hide_legacy_data(self.legacy_data_moves)
            try:
                config_text = self.original_bytes.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise UATError("config/config.json 不是有效 UTF-8") from exc
            config = _decode_json_object(
                config_text,
                label  = "config/config.json",
                source = CONFIG_PATH,
            )
            data_root = (self.output_dir / "data").resolve()
            data_root.mkdir(parents=True, exist_ok=True)
            config["data_root"]   = str(data_root)
            config["log_to_file"] = False
            # 命令矩阵需要本地入站端口；UAT 不连接生产 NapCat，也不在测试
            # 期间监听源码变化。三项设置都只存在于恢复锁保护的临时配置中。
            config["enable_inbound_server"] = True
            config["enable_ws_client"]      = False
            config["enable_plugin_watcher"] = False
            temporary = (json.dumps(config, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
            _atomic_write(CONFIG_PATH, temporary)
        # Ctrl+C/SystemExit 也不能跳过真实数据和配置回位。
        except BaseException:
            self._restore()
            raise
        return self

    def _restore(self) -> None:
        if self.restored:
            return
        # 先恢复数据目录，再发布原配置；这样恢复失败时，服务仍指向隔离目录，
        # 不会在真实数据暂时隐藏的窗口被误启动。
        _restore_legacy_data(self.legacy_data_moves)
        if self.original_bytes:
            _atomic_write(CONFIG_PATH, self.original_bytes)
            if _sha256_file(CONFIG_PATH) != self.original_hash:
                raise UATError("config/config.json 恢复后哈希不一致")
        self.restored = True
        LOCK_PATH.unlink(missing_ok=True)

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self._restore()


def _recover_interrupted_run(endpoint: str) -> int:
    if not LOCK_PATH.exists():
        print(f"没有待恢复的 UAT 锁: {LOCK_PATH}")
        return 0
    host, port = _endpoint_host_port(endpoint)
    if _port_is_open(host, port):
        raise UATError(f"{host}:{port} 仍有服务监听；先停止服务，再恢复配置")
    lock = _read_json_object(LOCK_PATH, label="UAT 恢复锁")
    root_text   = lock.get("project_root")
    config_text = lock.get("config_path")
    backup_text = lock.get("backup_path")
    if (
        not isinstance(root_text, str)
        or not root_text
        or not isinstance(config_text, str)
        or not config_text
        or not isinstance(backup_text, str)
        or not backup_text
    ):
        raise UATError("UAT 恢复锁缺少项目、配置或备份路径")
    locked_root = Path(root_text).resolve()
    if locked_root != PROJECT_ROOT.resolve():
        raise UATError(f"恢复锁指向意外项目目录: {locked_root}")
    expected_config = CONFIG_PATH.resolve()
    locked_config   = Path(config_text).resolve()
    if locked_config != expected_config:
        raise UATError(f"恢复锁指向意外配置文件: {locked_config}")
    backup        = Path(backup_text).resolve()
    original_hash = str(lock.get("original_sha256", ""))
    if not backup.is_file():
        raise UATError(f"恢复备份不存在: {backup}")
    payload = backup.read_bytes()
    if not original_hash or _sha256_bytes(payload) != original_hash:
        raise UATError("恢复备份哈希与锁记录不一致，拒绝覆盖")
    legacy_data_moves = _legacy_moves_from_lock(lock)
    # 与正常退出保持相同顺序：真实数据全部回位后，才恢复会指向真实目录的配置。
    _restore_legacy_data(legacy_data_moves)
    _atomic_write(CONFIG_PATH, payload)
    if _sha256_file(CONFIG_PATH) != original_hash:
        raise UATError("恢复后的 config/config.json 哈希不一致")
    LOCK_PATH.unlink()
    print(f"已逐字节恢复配置: {CONFIG_PATH}")
    if legacy_data_moves:
        print(f"已恢复 {len(legacy_data_moves)} 个旧插件数据目录")
    return 0


def _terminate_process_tree(process: subprocess.Popen[Any]) -> None:
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
        endpoint: str,
        token: str,
        expected_plugins: int,
        start_timeout: float,
        stop_timeout: float,
    ) -> None:
        self.output_dir                            = output_dir
        self.endpoint                              = endpoint
        self.health_url                            = _health_endpoint(endpoint)
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
            [PYTHON_COMMAND, str(PROJECT_ROOT / "main.py")],
            cwd    = PROJECT_ROOT,
            stdin  = subprocess.DEVNULL,
            stdout = self.stdout_handle,
            stderr = self.stderr_handle,
            env    = {**os.environ, "PYTHONUTF8": "1"},
            **_popen_flags(),
        )
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
        graceful = "XiaoQing shutdown complete" in combined
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
        "head": head.stdout.strip() if head.returncode == 0 else None,
        "dirty_entries": len(status.stdout.splitlines()),
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
    phases: list[PhaseResult]        = []
    lifecycle: list[LifecycleResult] = []
    started                          = controller.start()
    lifecycle.append(started)
    if started.status != "passed":
        phases.extend(_mark_skipped(spec, "服务启动失败") for spec in specs)
        return phases, lifecycle

    try:
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
            if spec.name in {"websocket-matrix", "http-matrix"} and has_more:
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
            f"- Python：`{report['python']['command']}`（仅记录版本，不按版本卡口）",
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
            f"- config 逐字节恢复：{report['integrity']['config_restored']}。",
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
            "bash scripts/run_full_uat.sh",
            "# 只复测失败阶段示例",
            "bash scripts/run_full_uat.sh --phases ws-matrix,pytest,diff",
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
        "output": str(output_dir),
        "python": PYTHON_COMMAND,
        "environment_policy": "use-current-python",
        "python_version_gate": False,
        "isolated_data_root": str(output_dir / "data"),
        "include_external": args.include_external,
        "include_chat_quality": any(spec.name == "chat-quality" for spec in specs),
        "phases": [
            {
                "name": spec.name,
                "requires_service": spec.requires_service,
                "description": spec.description,
                "command": _command_text(spec.command),
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
        args.ws_endpoint = _derive_ws_endpoint(args.endpoint)
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
    phase_results: list[PhaseResult] = []
    lifecycle_results: list[LifecycleResult] = []
    fatal_errors: list[str] = []
    service_specs = tuple(spec for spec in specs if spec.requires_service)
    static_specs = tuple(spec for spec in specs if not spec.requires_service)
    isolation: RuntimeIsolation | None = None

    if service_specs:
        config = _read_json_object(CONFIG_PATH, label="运行配置")
        try:
            _preflight_service_ports(args.endpoint, config)
            isolation = RuntimeIsolation(output_dir)
            with isolation:
                controller = ServiceController(
                    output_dir,
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
        # 服务编排的最外层故障必须进入报告；with/finally 已先完成数据和进程回收。
        except Exception as exc:  # noqa: BLE001
            fatal_errors.append(f"{type(exc).__name__}: {exc}")
            covered = {row.name for row in phase_results}
            phase_results.extend(
                _mark_skipped(spec, "服务阶段发生致命错误")
                for spec in service_specs
                if spec.name not in covered
            )

    config_restored = _sha256_file(CONFIG_PATH) == config_before
    if not config_restored:
        fatal_errors.append("config/config.json 未恢复到运行前哈希")

    if config_restored:
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
    else:
        phase_results.extend(
            _mark_skipped(spec, "配置未恢复，停止后续门禁") for spec in static_specs
        )

    host, port = _endpoint_host_port(args.endpoint)
    integrity = {
        "isolated_data": bool(service_specs),
        "config_restored": config_restored,
        "secrets_unchanged": _sha256_file(SECRETS_PATH) == secrets_before,
        "inbound_port_released": not service_specs or not _port_is_open(host, port),
        "config_sha256_before": config_before,
        "config_sha256_after": _sha256_file(CONFIG_PATH),
        "secrets_sha256_before": secrets_before,
        "secrets_sha256_after": _sha256_file(SECRETS_PATH),
    }
    all_phases_passed    = all(row.status == "passed" for row in phase_results)
    all_lifecycle_passed = all(row.status == "passed" for row in lifecycle_results)
    gate_passed          = (
        not fatal_errors
        and all_phases_passed
        and all_lifecycle_passed
        and integrity["config_restored"]
        and integrity["secrets_unchanged"]
        and integrity["inbound_port_released"]
    )
    report = {
        "schema_version": 1,
        "started_at": started_at,
        "finished_at": _now_iso(),
        "gate_passed": gate_passed,
        "output_dir": str(output_dir),
        "python": {
            "command": PYTHON_COMMAND,
            "version": sys.version,
            "version_gate": False,
        },
        "repository": repository,
        "phases": [asdict(row) for row in phase_results],
        "lifecycle": [asdict(row) for row in lifecycle_results],
        "integrity": integrity,
        "fatal_errors": fatal_errors,
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
    parser.add_argument("--recover", action="store_true")
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
        if args.recover:
            if args.plan_only or args.output or args.phases:
                raise UATError("--recover 不能与执行/计划参数组合")
            return _recover_interrupted_run(args.endpoint)
        return _execute(args)
    except UATError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
