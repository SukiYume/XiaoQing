"""QingSSH 的服务器配置、连接、命令执行与 SFTP 管理。"""

import asyncio
import codecs
import fnmatch
import json
import logging
import math
import os
import re
import shlex
import uuid
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from typing import Any, TypedDict, TypeVar, cast

from core.args import parse_int
from core.plugin_base import atomic_write_text
from core.public_errors import public_error_message
from core.sensitive_audit import log_sensitive_operation

from .audit import audit_error_type
from .config import (
    COMMAND_TIMEOUT,
    CONNECT_TIMEOUT,
    EXIT_CODE_ERROR,
    EXIT_CODE_INTERRUPTED,
    EXIT_CODE_TIMEOUT,
    MAX_OUTPUT_LENGTH,
)

# Paramiko 是可选依赖；未安装时仍允许框架发现并加载插件。
try:
    import paramiko
    from paramiko.config import SSHConfig

    PARAMIKO_AVAILABLE = True
except ImportError:
    PARAMIKO_AVAILABLE = False
    paramiko = cast(Any, None)
    SSHConfig = cast(Any, None)

logger = logging.getLogger(__name__)
_CURRENT_REQUEST_CONTEXT: ContextVar[Any | None] = ContextVar(
    "qingssh_current_request_context",
    default=None,
)

_SSH_OPTIONS_WITH_ARG = {
    "-B",
    "-b",
    "-c",
    "-D",
    "-E",
    "-e",
    "-F",
    "-I",
    "-i",
    "-J",
    "-L",
    "-l",
    "-m",
    "-O",
    "-o",
    "-p",
    "-Q",
    "-R",
    "-S",
    "-W",
    "-w",
}


@dataclass(frozen=True)
class CommandTerminationResult:
    """一次远端命令终止及本地资源回收的结果。"""

    found: bool
    local_cleaned: bool
    remote_confirmed: bool
    signal_attempted: bool = False
    error: str | None = None

    @property
    def remote_unknown(self) -> bool:
        return self.found and not self.remote_confirmed


class UnsupportedProxyCommand(ValueError):
    """ProxyCommand 不是受支持的安全 ``ssh -W`` 形式。"""


class SSHConfigurationError(ValueError):
    """服务器配置缺失或字段不合法。"""


@dataclass
class _ConnectionResources:
    """连接建立完成前由当前协程独占的资源。"""

    client: Any = None
    jump_client: Any = None
    proxy_sock: Any = None
    installed: bool = False


class _ActiveCommand(TypedDict):
    """活跃命令通道及其远端进程组首进程。"""

    channel: Any
    remote_pid: int | None


OutputCallback = Callable[[str], Awaitable[Any]]
_T = TypeVar("_T")


async def _finish_blocking_call(
    function: Callable[..., _T],
    /,
    *args: Any,
    **kwargs: Any,
) -> tuple[_T, bool]:
    """即使调用方被取消也等阻塞写操作结束，并返回是否收到过取消请求。"""

    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    cancellation_requested = False
    while True:
        try:
            return await asyncio.shield(task), cancellation_requested
        except asyncio.CancelledError:
            cancellation_requested = True


def _retain_keyed_lock(
    locks: dict[str, asyncio.Lock],
    users: dict[str, int],
    key: str,
) -> asyncio.Lock:
    """登记一次锁使用；等待中的协程也计入，防止锁被提前移除。"""

    lock = locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        locks[key] = lock
    users[key] = users.get(key, 0) + 1
    return lock


def _release_keyed_lock(
    locks: dict[str, asyncio.Lock],
    users: dict[str, int],
    key: str,
    lock: asyncio.Lock,
) -> None:
    """注销一次锁使用；最后一个持有者或等待者离开后再清理缓存。"""

    remaining = users.get(key, 1) - 1
    if remaining > 0:
        users[key] = remaining
        return
    users.pop(key, None)
    if locks.get(key) is lock:
        locks.pop(key, None)


def _expand_proxycommand(proxycommand: str, server: dict[str, Any]) -> str:
    return (
        proxycommand.replace("%h", server["host"])
        .replace("%p", str(server["port"]))
        .replace("%r", server["username"])
    )


def _parse_proxyjump_command(proxycommand: str) -> dict[str, Any] | None:
    try:
        parts = shlex.split(proxycommand, posix=(os.name != "nt"))
    except ValueError:
        return None

    if not parts:
        return None

    executable = Path(parts[0]).name.lower()
    if executable not in {"ssh", "ssh.exe"}:
        return None

    jump_host = None
    jump_user = None
    jump_port = None
    saw_tunnel = False
    idx = 1

    while idx < len(parts):
        part = parts[idx]

        if part == "-W":
            if idx + 1 >= len(parts):
                return None
            saw_tunnel = True
            idx += 2
            continue
        if part.startswith("-W") and len(part) > 2:
            saw_tunnel = True
            idx += 1
            continue
        if part == "-l":
            if idx + 1 >= len(parts):
                return None
            jump_user = parts[idx + 1]
            idx += 2
            continue
        if part.startswith("-l") and len(part) > 2:
            jump_user = part[2:]
            idx += 1
            continue
        if part == "-p":
            if idx + 1 >= len(parts):
                return None
            try:
                jump_port = int(parts[idx + 1])
            except ValueError:
                return None
            idx += 2
            continue
        if part.startswith("-p") and len(part) > 2:
            try:
                jump_port = int(part[2:])
            except ValueError:
                return None
            idx += 1
            continue
        if part in _SSH_OPTIONS_WITH_ARG:
            if idx + 1 >= len(parts):
                return None
            idx += 2
            continue
        if part.startswith("-"):
            idx += 1
            continue

        jump_host = part
        idx += 1

    if not saw_tunnel or not jump_host:
        return None

    if "@" in jump_host and not jump_user:
        jump_user, jump_host = jump_host.split("@", 1)

    if not jump_host or jump_user == "" or (jump_port is not None and not 1 <= jump_port <= 65535):
        return None

    return {
        "jump_host": jump_host,
        "jump_user": jump_user,
        "jump_port": jump_port,
    }


def _parse_proxyjump_spec(proxyjump: str) -> dict[str, Any] | None:
    """解析单跳 ``[user@]host[:port]``，拒绝链式或含空白的目标。"""

    if (
        not proxyjump
        or proxyjump != proxyjump.strip()
        or proxyjump.casefold() == "none"
        or "," in proxyjump
        or any(character.isspace() for character in proxyjump)
    ):
        return None

    target = proxyjump
    jump_user = None
    if "@" in target:
        jump_user, target = target.split("@", 1)
        if not jump_user or "@" in target:
            return None

    jump_port = None
    if target.startswith("["):
        closing = target.find("]")
        if closing <= 1:
            return None
        jump_host = target[1:closing]
        suffix = target[closing + 1 :]
        if suffix:
            if not suffix.startswith(":"):
                return None
            jump_port = parse_int(suffix[1:], minimum=1, maximum=65535)
            if jump_port is None:
                return None
    else:
        if target.count(":") > 1:
            return None
        jump_host, separator, port_text = target.rpartition(":")
        if not separator:
            jump_host = target
        else:
            jump_port = parse_int(port_text, minimum=1, maximum=65535)
            if jump_port is None:
                return None

    if not jump_host:
        return None
    return {
        "jump_host": jump_host,
        "jump_user": jump_user,
        "jump_port": jump_port,
    }


class SSHManager:
    """按“用户、群、服务器”隔离 SSH 客户端与活跃命令。"""

    def __init__(self, data_dir: Path, context: Any | None = None) -> None:
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.servers_file = data_dir / "servers.json"
        self._base_context = context
        self.servers: dict[str, dict[str, Any]] = {}
        self.connections: dict[str, Any] = {}
        self.active_channels: dict[str, object] = {}
        self._ssh_config: Any | None = None
        self._config_lock = asyncio.Lock()
        self._initialize_lock = asyncio.Lock()
        self._connection_locks: dict[str, asyncio.Lock] = {}
        self._connection_lock_users: dict[str, int] = {}
        self._termination_locks: dict[str, asyncio.Lock] = {}
        self._termination_lock_users: dict[str, int] = {}
        self._connection_registry_lock = asyncio.Lock()
        self._pending_connection_keys: set[str] = set()
        self._initialized = False

    @property
    def context(self) -> Any | None:
        """返回当前任务绑定的请求上下文，否则回退到生命周期上下文。"""

        return _CURRENT_REQUEST_CONTEXT.get() or self._base_context

    def _log(self, level: str, message: str) -> None:
        """
        辅助方法：记录日志（如果上下文有 logger）

        Args:
            level: 日志级别 (debug, info, warning, error)
            message: 日志消息
        """
        if self.context and hasattr(self.context, "logger"):
            logger = self.context.logger
            log_method = getattr(logger, level, None)
            if log_method and callable(log_method):
                log_method(message)

    def _log_sensitive(
        self,
        level: str,
        *,
        operation: str,
        status: str,
        value: str,
        exc: BaseException | None = None,
    ) -> None:
        context_logger = getattr(self.context, "logger", None)
        target_logger = (
            cast(logging.Logger, context_logger)
            if callable(getattr(context_logger, "log", None))
            else logger
        )
        log_sensitive_operation(
            target_logger,
            operation,
            request_id=getattr(self.context, "request_id", None),
            status=status,
            payload=value,
            exc=exc,
            level=getattr(logging, level.upper(), logging.INFO),
        )

    def _unexpected_error_message(self, exc: BaseException, *, component: str) -> str:
        """记录可关联诊断，只向会话返回稳定的公开错误。"""

        context_logger = getattr(self.context, "logger", None)
        selected_logger = (
            context_logger if callable(getattr(context_logger, "error", None)) else logger
        )
        return cast(
            str,
            public_error_message(
                self.context,
                exc,
                logger=selected_logger,
                component=component,
            ),
        )

    def _load_host_keys(self, client: "paramiko.SSHClient", known_hosts_path: Path) -> None:
        try:
            client.load_system_host_keys()
        except Exception as exc:
            self._log(
                "warning",
                f"SSH host key load status=failed error_type={audit_error_type(exc)}",
            )

        if known_hosts_path.exists():
            try:
                client.load_host_keys(str(known_hosts_path))
            except Exception as exc:
                self._log(
                    "warning",
                    f"SSH known_hosts load status=failed error_type={audit_error_type(exc)}",
                )

    # ──────────────────── 初始化与持久化配置 ────────────────────

    async def initialize(self) -> None:
        """仅初始化一次，避免并发入口重复加载并覆盖配置。"""

        if self._initialized:
            return
        async with self._initialize_lock:
            if self._initialized:
                return
            await self._load_servers()
            await self.reload_ssh_config()
            self._initialized = True

    @staticmethod
    def _validate_server_snapshot(payload: object) -> dict[str, dict[str, Any]]:
        """验证持久化文件的外层结构，并断开与 JSON 对象的可变引用。"""

        if not isinstance(payload, dict):
            raise ValueError("SSH server config root must be an object")
        snapshot: dict[str, dict[str, Any]] = {}
        for name, server in payload.items():
            if not isinstance(name, str) or not name:
                raise ValueError("SSH server names must be non-empty strings")
            if not isinstance(server, dict):
                raise ValueError(f"SSH server config must be an object: {name}")
            snapshot[name] = dict(server)
        return snapshot

    async def _save_server_snapshot(self, snapshot: Mapping[str, Mapping[str, Any]]) -> bool:
        """原子写入快照，返回写入期间是否收到过取消请求。"""

        payload = json.dumps(snapshot, ensure_ascii=False, indent=4)
        _, cancellation_requested = await _finish_blocking_call(
            atomic_write_text,
            self.servers_file,
            payload,
        )
        return cancellation_requested

    async def _rollback_secret_refs(self, refs: list[str]) -> None:
        """尽力删除事务失败前已经创建的密码引用。"""

        if not refs:
            return
        delete_secret = getattr(self.context, "delete_secret", None)
        if not callable(delete_secret):
            self._log("error", "SSH secret rollback unavailable")
            return
        for password_ref in refs:
            try:
                await _finish_blocking_call(delete_secret, password_ref)
            except Exception as exc:
                self._log(
                    "error",
                    f"SSH secret rollback status=failed error_type={audit_error_type(exc)}",
                )

    async def _load_servers(self) -> None:
        """加载配置，并把旧版明文密码事务式迁移到插件密钥存储。"""

        async with self._config_lock:
            if not self.servers_file.exists():
                self.servers = {}
                return

            created_refs: list[str] = []
            committed = False
            try:
                text = await asyncio.to_thread(self.servers_file.read_text, encoding="utf-8")
                candidate = self._validate_server_snapshot(json.loads(text))
                migrated = False
                for server in candidate.values():
                    if "password" not in server:
                        continue
                    password = server.pop("password")
                    migrated = True
                    if password is None or password == "" or server.get("password_ref"):
                        continue
                    if not isinstance(password, str):
                        raise ValueError("legacy SSH password must be a string")
                    set_secret = getattr(self.context, "set_secret", None)
                    if not callable(set_secret):
                        raise RuntimeError(
                            "legacy SSH plaintext password requires plugin secret store"
                        )
                    password_ref = f"passwords.{uuid.uuid4().hex}"
                    created_refs.append(password_ref)
                    _, cancellation_requested = await _finish_blocking_call(
                        set_secret,
                        password_ref,
                        password,
                    )
                    if cancellation_requested:
                        raise asyncio.CancelledError
                    server["password_ref"] = password_ref

                cancellation_requested = False
                if migrated:
                    cancellation_requested = await self._save_server_snapshot(candidate)
                self.servers = candidate
                committed = True
                if cancellation_requested:
                    raise asyncio.CancelledError
            except asyncio.CancelledError:
                if not committed:
                    await self._rollback_secret_refs(created_refs)
                    self.servers = {}
                raise
            except Exception as exc:
                await self._rollback_secret_refs(created_refs)
                self._log(
                    "error",
                    f"SSH server config load status=failed error_type={audit_error_type(exc)}",
                )
                self.servers = {}

    async def reload_ssh_config(self) -> None:
        """重新读取 ``~/.ssh/config``；文件消失或解析失败时清空旧缓存。"""

        self._ssh_config = None
        if not PARAMIKO_AVAILABLE or SSHConfig is None:
            return
        ssh_config_path = Path.home() / ".ssh" / "config"
        if not ssh_config_path.is_file():
            return
        try:
            text = await asyncio.to_thread(ssh_config_path.read_text, encoding="utf-8")
            config = SSHConfig()
            config.parse(StringIO(text))
            self._ssh_config = config
        except Exception as exc:
            self._log(
                "warning",
                f"SSH config load status=failed error_type={audit_error_type(exc)}",
            )

    # ──────────────────── OpenSSH config 导入 ────────────────────

    def get_ssh_config_hosts(self) -> list[str]:
        """获取 ~/.ssh/config 中的所有 Host 名称"""
        if self._ssh_config is None:
            return []

        hosts: list[str] = []
        for host in self._ssh_config.get_hostnames():
            if isinstance(host, str) and "*" not in host and "?" not in host:
                hosts.append(host)
        return sorted(set(hosts))

    def get_ssh_config_for_host(self, host: str) -> dict[str, Any] | None:
        """获取 ~/.ssh/config 中特定 Host 的配置"""
        if self._ssh_config is None or host not in self.get_ssh_config_hosts():
            return None

        try:
            config = self._ssh_config.lookup(host)
            hostname = config.get("hostname", host)
            proxycommand = config.get("proxycommand")
            proxyjump = config.get("proxyjump")
            if not isinstance(hostname, str) or not hostname:
                return None
            if proxycommand is not None and not isinstance(proxycommand, str):
                return None
            if proxyjump is not None and not isinstance(proxyjump, str):
                return None
            if isinstance(proxyjump, str) and proxyjump.casefold() == "none":
                proxyjump = None
            port = int(config.get("port", 22))
            if not 1 <= port <= 65535:
                return None
            username = config.get("user", "root")
            if not isinstance(username, str) or not username:
                return None
            identityfile = config.get("identityfile", [])
            if isinstance(identityfile, str):
                identityfile = [identityfile]
            if not isinstance(identityfile, list):
                identityfile = []
            return {
                "hostname": hostname,
                "port": port,
                "user": username,
                "identityfile": [item for item in identityfile if isinstance(item, str)],
                "proxycommand": proxycommand,
                "proxyjump": proxyjump,
            }
        except Exception as exc:
            self._log_sensitive(
                "warning",
                operation="qingssh.config_lookup",
                status="failed",
                value=host,
                exc=exc,
            )
        return None

    def _build_imported_server(
        self,
        host_name: str,
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        """把一个 OpenSSH Host 条目转换为插件持久化格式。"""

        config = self.get_ssh_config_for_host(host_name)
        if not config:
            return None

        key_path = None
        if config.get("identityfile"):
            key_path = Path(os.path.expanduser(config["identityfile"][0])).as_posix()

        server_config: dict[str, Any] = {
            "host": config["hostname"],
            "port": config["port"],
            "username": config["user"],
            "auth_type": "key" if key_path else "agent",
            "from_ssh_config": True,
            "ssh_config_host": host_name,
        }
        if key_path:
            server_config["key_path"] = key_path
        if config.get("proxycommand"):
            server_config["proxycommand"] = config["proxycommand"]
        if config.get("proxyjump"):
            server_config["proxyjump"] = config["proxyjump"]
        return config, server_config

    async def import_from_ssh_config(
        self, host_name: str, alias: str | None = None
    ) -> tuple[bool, str]:
        """从 ~/.ssh/config 导入一个服务器配置。"""

        imported_server = self._build_imported_server(host_name)
        if imported_server is None:
            return False, f"在 ~/.ssh/config 中未找到 Host: {host_name}"
        config, server_config = imported_server
        name = alias or host_name

        async with self._config_lock:
            if name in self.servers:
                return False, f"服务器 '{name}' 已存在"
            candidate = deepcopy(self.servers)
            candidate[name] = server_config
            cancellation_requested = await self._save_server_snapshot(candidate)
            self.servers = candidate
            if cancellation_requested:
                raise asyncio.CancelledError

        if config.get("proxycommand") or config.get("proxyjump"):
            return True, f"✅ 已导入: {name}（使用跳板机）"
        return True, f"✅ 已导入: {name} ({config['hostname']}:{config['port']})"

    async def import_all_from_ssh_config(self) -> tuple[int, list[str]]:
        """在一次原子写盘中导入全部尚未保存的有效 Host。"""

        available: dict[str, dict[str, Any]] = {}
        for host in self.get_ssh_config_hosts():
            resolved = self._build_imported_server(host)
            if resolved is not None:
                available[host] = resolved[1]
        async with self._config_lock:
            imported_hosts = [host for host in available if host not in self.servers]
            if not imported_hosts:
                return 0, []
            candidate = deepcopy(self.servers)
            candidate.update((host, available[host]) for host in imported_hosts)
            cancellation_requested = await self._save_server_snapshot(candidate)
            self.servers = candidate
            if cancellation_requested:
                raise asyncio.CancelledError
        return len(imported_hosts), imported_hosts

    async def add_server(
        self,
        name: str,
        host: str,
        port: int = 22,
        username: str = "root",
        auth_type: str = "password",
        password: str | None = None,
        password_ref: str | None = None,
        key_path: str | None = None,
    ) -> bool:
        """新增服务器；密钥与配置文件以同一逻辑事务提交。"""

        if not all(isinstance(item, str) and item for item in (name, host, username)):
            raise ValueError("name, host and username must be non-empty strings")
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("SSH port must be between 1 and 65535")
        if auth_type not in {"password", "key", "agent"}:
            raise ValueError("unsupported SSH authentication type")
        if password is not None and password_ref is not None:
            raise ValueError("password and password_ref are mutually exclusive")
        if auth_type == "password" and password is None and not password_ref:
            raise ValueError("password authentication requires a credential")
        if auth_type == "key" and not key_path:
            raise ValueError("key authentication requires key_path")

        created_refs: list[str] = []
        cancellation_requested = False
        committed = False
        async with self._config_lock:
            if name in self.servers:
                return False
            try:
                if password is not None:
                    if not password:
                        raise ValueError("SSH password must not be empty")
                    set_secret = getattr(self.context, "set_secret", None)
                    if not callable(set_secret):
                        raise RuntimeError(
                            "plaintext SSH passwords cannot be stored in servers.json"
                        )
                    password_ref = f"passwords.{uuid.uuid4().hex}"
                    created_refs.append(password_ref)
                    _, secret_cancelled = await _finish_blocking_call(
                        set_secret,
                        password_ref,
                        password,
                    )
                    if secret_cancelled:
                        raise asyncio.CancelledError

                server: dict[str, Any] = {
                    "host": host,
                    "port": port,
                    "username": username,
                    "auth_type": auth_type,
                }
                if key_path:
                    server["key_path"] = key_path
                if password_ref:
                    server["password_ref"] = password_ref
                candidate = deepcopy(self.servers)
                candidate[name] = server
                cancellation_requested = await self._save_server_snapshot(candidate)
                self.servers = candidate
                committed = True
            except BaseException:
                if not committed:
                    await self._rollback_secret_refs(created_refs)
                raise

        self._log_sensitive(
            "info",
            operation="qingssh.server_add",
            status="success",
            value="\0".join((name, host, str(port))),
        )
        if cancellation_requested:
            raise asyncio.CancelledError
        return True

    async def remove_server(self, name: str) -> bool:
        """先提交配置删除，再回收密码引用和该名称的所有连接。"""

        async with self._config_lock:
            server = self.servers.get(name)
            if server is None:
                return False
            candidate = deepcopy(self.servers)
            candidate.pop(name)
            cancellation_requested = await self._save_server_snapshot(candidate)
            self.servers = candidate

        password_ref = server.get("password_ref")
        delete_secret = getattr(self.context, "delete_secret", None)
        if isinstance(password_ref, str) and callable(delete_secret):
            try:
                _, secret_cancelled = await _finish_blocking_call(delete_secret, password_ref)
                cancellation_requested = cancellation_requested or secret_cancelled
            except Exception as exc:
                self._log(
                    "warning",
                    f"SSH secret delete status=failed error_type={audit_error_type(exc)}",
                )
        for key in list(self.connections):
            if self.parse_connection_key(key)[2] == name:
                self._disconnect_key(key, send_interrupt=True)
        if cancellation_requested:
            raise asyncio.CancelledError
        return True

    def get_server(self, name: str) -> dict[str, Any] | None:
        """返回单个配置副本，防止调用方绕过持久化事务修改状态。"""

        server = self.servers.get(name)
        return deepcopy(server) if server is not None else None

    def list_servers(self) -> dict[str, dict[str, Any]]:
        """返回全部配置的防御性副本。"""

        return deepcopy(self.servers)

    # ──────────────────── 连接索引与资源回收 ────────────────────

    @staticmethod
    def build_connection_key(user_id: object, group_id: object | None, name: str) -> str:
        """生成 ``用户:群:服务器`` 隔离键。"""

        return f"{user_id}:{group_id}:{name}"

    @staticmethod
    def parse_connection_key(key: str) -> tuple[str, str | None, str]:
        user_id, group_id, name = key.split(":", 2)
        return user_id, None if group_id == "None" else group_id, name

    @staticmethod
    def _active_channel(active: object | None) -> Any:
        return active.get("channel") if isinstance(active, dict) else active

    def _close_channel(self, channel: Any, send_interrupt: bool = False) -> bool:
        if channel is None:
            return True

        if send_interrupt:
            with suppress(Exception):
                send_ready = getattr(channel, "send_ready", None)
                if callable(send_ready) and send_ready():
                    channel.send("\x03")

        try:
            channel.close()
        except Exception:
            return False
        return True

    def _close_jump_client(self, client: Any) -> None:
        jump_client = getattr(client, "_jump_client", None)
        if jump_client is None:
            return

        try:
            jump_client.close()
        except Exception as exc:
            self._log(
                "warning",
                f"SSH jump client close status=failed error_type={audit_error_type(exc)}",
            )

    def _disconnect_key(self, key: str, *, send_interrupt: bool) -> bool:
        active = self.active_channels.pop(key, None)
        channel = self._active_channel(active)
        if channel is not None:
            self._close_channel(channel, send_interrupt=send_interrupt)

        client = self.connections.get(key)
        if client is None:
            return channel is not None

        try:
            client.close()
        except Exception as exc:
            self._log_sensitive(
                "warning",
                operation="qingssh.disconnect",
                status="failed",
                value=key,
                exc=exc,
            )
        finally:
            self._close_jump_client(client)
            self.connections.pop(key, None)

        return True

    def _configured_max_connections(self) -> int:
        plugin_config = (
            {}
            if self.context is None
            else self.context.get_settings_snapshot().plugin_config("qingssh")
        )
        try:
            configured = int(plugin_config.get("max_connections", 32))
        except (TypeError, ValueError):
            configured = 32
        return max(1, configured)

    async def _reserve_connection_slot(self, key: str, max_connections: int) -> bool:
        """预留容量，防止并发拨号突破连接上限。"""

        async with self._connection_registry_lock:
            occupied_keys = set(self.connections) | self._pending_connection_keys
            if key not in occupied_keys and len(occupied_keys) >= max_connections:
                return False
            self._pending_connection_keys.add(key)
            return True

    async def _release_connection_slot(self, key: str) -> None:
        async with self._connection_registry_lock:
            self._pending_connection_keys.discard(key)

    def _resolve_connection_server(
        self,
        name: str,
        *,
        username_override: str | None,
    ) -> tuple[dict[str, Any] | None, str]:
        """解析已保存配置；找不到时再回退到 OpenSSH 配置。"""

        server = self.get_server(name)
        if server is None:
            ssh_config = self.get_ssh_config_for_host(name)
            if ssh_config:
                server = {
                    "host": ssh_config["hostname"],
                    "port": ssh_config["port"],
                    "username": ssh_config["user"],
                    "auth_type": "key" if ssh_config.get("identityfile") else "agent",
                    "key_path": (
                        os.path.expanduser(ssh_config["identityfile"][0])
                        if ssh_config.get("identityfile")
                        else None
                    ),
                }
                if ssh_config.get("proxycommand"):
                    server["proxycommand"] = ssh_config["proxycommand"]
                if ssh_config.get("proxyjump"):
                    server["proxyjump"] = ssh_config["proxyjump"]

        if not server:
            available_hosts = self.get_ssh_config_hosts()
            if not available_hosts:
                return None, f"❌ 服务器 '{name}' 不存在"
            hint = f"\n💡 ~/.ssh/config 中可用的 Host: {', '.join(available_hosts[:5])}"
            if len(available_hosts) > 5:
                hint += f" ... (共 {len(available_hosts)} 个)"
            return None, f"❌ 服务器 '{name}' 不存在{hint}"

        if username_override:
            server = server.copy()
            server["username"] = username_override
        return server, ""

    @staticmethod
    def _connection_audit_value(name: str, server: dict[str, Any]) -> str:
        return "\0".join((name, str(server["host"]), str(server["port"]), str(server["username"])))

    def _target_connect_kwargs(self, server: dict[str, Any]) -> dict[str, Any]:
        host = server.get("host")
        port = server.get("port")
        username = server.get("username")
        if not isinstance(host, str) or not host:
            raise SSHConfigurationError("缺少有效主机地址")
        if type(port) is not int or not 1 <= port <= 65535:
            raise SSHConfigurationError("SSH 端口不合法")
        if not isinstance(username, str) or not username:
            raise SSHConfigurationError("缺少 SSH 用户名")

        connect_kwargs: dict[str, Any] = {
            "hostname": host,
            "port": port,
            "username": username,
            "timeout": CONNECT_TIMEOUT,
            "banner_timeout": CONNECT_TIMEOUT,
            "auth_timeout": CONNECT_TIMEOUT,
            "channel_timeout": CONNECT_TIMEOUT,
        }
        auth_type = server.get("auth_type", "agent")
        if auth_type == "key":
            key_path = server.get("key_path")
            if not isinstance(key_path, str) or not key_path:
                raise SSHConfigurationError("密钥认证缺少 key_path")
            connect_kwargs["key_filename"] = key_path
        elif auth_type == "password":
            password_ref = server.get("password_ref")
            get_secret = getattr(self.context, "get_secret", None)
            if not isinstance(password_ref, str) or not callable(get_secret):
                raise SSHConfigurationError("密码认证缺少可用的密码引用")
            try:
                password = get_secret(password_ref)
            except Exception as exc:
                raise SSHConfigurationError("密码凭据读取失败") from exc
            if not isinstance(password, str) or not password:
                raise SSHConfigurationError("密码凭据不存在或为空")
            connect_kwargs.update(
                password=password,
                allow_agent=False,
                look_for_keys=False,
            )
        elif auth_type == "agent":
            connect_kwargs["allow_agent"] = True
            connect_kwargs["look_for_keys"] = True
        else:
            raise SSHConfigurationError("不支持的认证方式")
        return connect_kwargs

    async def _configure_proxy_jump(
        self,
        server: dict[str, Any],
        connect_kwargs: dict[str, Any],
        resources: _ConnectionResources,
        *,
        known_hosts_path: Path,
    ) -> None:
        proxycommand = server.get("proxycommand")
        proxyjump = server.get("proxyjump")
        if not proxycommand and not proxyjump:
            return
        if proxyjump:
            jump_target = _parse_proxyjump_spec(proxyjump) if isinstance(proxyjump, str) else None
        else:
            jump_target = (
                _parse_proxyjump_command(_expand_proxycommand(proxycommand, server))
                if isinstance(proxycommand, str)
                else None
            )
        if not jump_target:
            raise UnsupportedProxyCommand

        jump_host_name = jump_target["jump_host"]
        jump_conf = self.get_ssh_config_for_host(jump_host_name) or {
            "hostname": jump_host_name,
            "port": 22,
            "user": "root",
            "identityfile": [],
        }
        jump_kwargs: dict[str, Any] = {
            "hostname": jump_conf.get("hostname", jump_host_name),
            "port": jump_target["jump_port"] or jump_conf.get("port", 22),
            "username": jump_target["jump_user"] or jump_conf.get("user", "root"),
            "timeout": CONNECT_TIMEOUT,
            "allow_agent": True,
            "look_for_keys": True,
        }
        if jump_conf.get("identityfile"):
            jump_kwargs["key_filename"] = os.path.expanduser(jump_conf["identityfile"][0])

        resources.jump_client = paramiko.SSHClient()
        resources.jump_client.set_missing_host_key_policy(paramiko.RejectPolicy())
        self._load_host_keys(resources.jump_client, known_hosts_path)
        await asyncio.wait_for(
            asyncio.to_thread(resources.jump_client.connect, **jump_kwargs),
            timeout=CONNECT_TIMEOUT + 5,
        )
        transport = resources.jump_client.get_transport()
        if transport is None:
            raise SSHConfigurationError("跳板机没有可用的 SSH transport")
        resources.proxy_sock = await asyncio.wait_for(
            asyncio.to_thread(
                transport.open_channel,
                "direct-tcpip",
                (server["host"], server["port"]),
                ("0.0.0.0", 0),
            ),
            timeout=CONNECT_TIMEOUT + 5,
        )
        connect_kwargs["sock"] = resources.proxy_sock
        if resources.client is None:
            raise RuntimeError("target SSH client was not created")
        resources.client._jump_client = resources.jump_client
        self._log_sensitive(
            "info",
            operation="qingssh.jump_connect",
            status="success",
            value=jump_host_name,
        )

    def _cleanup_connection_resources(self, resources: _ConnectionResources) -> None:
        """关闭失败或取消的连接流程尚未移交给注册表的全部资源。"""

        if resources.client is not None:
            with suppress(Exception):
                resources.client.close()
        if resources.proxy_sock is not None:
            with suppress(Exception):
                resources.proxy_sock.close()
        if resources.jump_client is not None:
            try:
                resources.jump_client.close()
            except Exception as exc:
                self._log(
                    "warning",
                    f"SSH jump client close status=failed error_type={audit_error_type(exc)}",
                )

    async def _install_connection(self, key: str, client: Any) -> None:
        """原子发布新客户端，再回收同一隔离键下的旧客户端。"""

        async with self._connection_registry_lock:
            old_client = self.connections.get(key)
            self.connections[key] = client
        if old_client is None or old_client is client:
            return
        try:
            await asyncio.to_thread(old_client.close)
        except Exception as exc:
            self._log_sensitive(
                "warning",
                operation="qingssh.connect_replace",
                status="old_close_failed",
                value=key,
                exc=exc,
            )
        finally:
            self._close_jump_client(old_client)

    def _log_connection_failure(
        self,
        *,
        audit_value: str,
        exc: BaseException,
    ) -> None:
        self._log_sensitive(
            "error",
            operation="qingssh.connect",
            status="failed",
            value=audit_value,
            exc=exc,
        )

    @staticmethod
    def _authentication_failure_message(server: dict[str, Any]) -> str:
        return (
            "❌ 认证失败\n"
            f"服务器: {server['host']}:{server['port']}\n"
            f"用户: {server['username']}\n\n"
            "💡 请检查:\n"
            "  1. 用户名和密码是否正确\n"
            "  2. 如使用密钥，确保 ssh-agent 已运行\n"
            "  3. 如使用私钥，请确认私钥配置可用"
        )

    def _ssh_failure_message(self, exc: BaseException) -> str:
        if paramiko is not None and isinstance(exc, paramiko.BadHostKeyException):
            return (
                "❌ SSH Host Key 不匹配\n\n"
                "这通常发生在：\n"
                "  • 服务器重新安装了系统\n"
                "  • 服务器重新生成了密钥\n\n"
                "请核对主机指纹，并确认本机 known_hosts 中的记录"
            )
        return self._unexpected_error_message(exc, component="qingssh.connect")

    async def _connect_resolved_server(
        self,
        *,
        key: str,
        name: str,
        server: dict[str, Any],
    ) -> tuple[bool, str]:
        resources = _ConnectionResources()
        audit_value = self._connection_audit_value(name, server)
        try:
            resources.client = paramiko.SSHClient()
            resources.client.set_missing_host_key_policy(paramiko.RejectPolicy())
            known_hosts_path = Path.home() / ".ssh" / "known_hosts"
            self._load_host_keys(resources.client, known_hosts_path)
            connect_kwargs = self._target_connect_kwargs(server)
            await self._configure_proxy_jump(
                server,
                connect_kwargs,
                resources,
                known_hosts_path=known_hosts_path,
            )
            self._log_sensitive(
                "info",
                operation="qingssh.connect",
                status="started",
                value=audit_value,
            )
            await asyncio.wait_for(
                asyncio.to_thread(resources.client.connect, **connect_kwargs),
                timeout=CONNECT_TIMEOUT + 5,
            )
            await self._install_connection(key, resources.client)
            resources.installed = True
            self._log_sensitive(
                "info",
                operation="qingssh.connect",
                status="success",
                value=audit_value,
            )
            return True, f"✅ 成功连接到 {name} ({server['host']})"
        except UnsupportedProxyCommand:
            return (
                False,
                "❌ 仅支持单跳 ProxyJump 或安全的 ssh -W ProxyCommand，请检查 SSH 配置",
            )
        except SSHConfigurationError as exc:
            return False, f"❌ 服务器配置无效: {exc}"
        except paramiko.AuthenticationException as exc:
            self._log_connection_failure(audit_value=audit_value, exc=exc)
            return False, self._authentication_failure_message(server)
        except paramiko.SSHException as exc:
            self._log_connection_failure(audit_value=audit_value, exc=exc)
            return False, self._ssh_failure_message(exc)
        except asyncio.CancelledError as exc:
            self._log_connection_failure(audit_value=audit_value, exc=exc)
            raise
        except Exception as exc:
            self._log_connection_failure(audit_value=audit_value, exc=exc)
            return False, self._unexpected_error_message(exc, component="qingssh.connect")
        finally:
            if not resources.installed:
                self._cleanup_connection_resources(resources)

    async def connect(
        self,
        user_id: str,
        group_id: str | None,
        name: str,
        username_override: str | None = None,
    ) -> tuple[bool, str]:
        """解析配置、预留容量并原子安装一个隔离的 SSH 客户端。"""

        if not PARAMIKO_AVAILABLE:
            return False, "❌ 未安装 paramiko 库，请运行: pip install paramiko"

        key = self.build_connection_key(user_id, group_id, name)
        lock = _retain_keyed_lock(
            self._connection_locks,
            self._connection_lock_users,
            key,
        )
        try:
            async with lock:
                # 容量只统计真实存活的客户端，底层已断开的旧条目应先回收。
                self.get_active_connections()
                max_connections = self._configured_max_connections()
                reserved = await self._reserve_connection_slot(key, max_connections)
                if not reserved:
                    return False, f"❌ SSH 活跃连接已达到配置上限 ({max_connections})"
                try:
                    server, error_message = self._resolve_connection_server(
                        name,
                        username_override=username_override,
                    )
                    if server is None:
                        return False, error_message
                    return await self._connect_resolved_server(
                        key=key,
                        name=name,
                        server=server,
                    )
                finally:
                    await asyncio.shield(self._release_connection_slot(key))
        finally:
            _release_keyed_lock(
                self._connection_locks,
                self._connection_lock_users,
                key,
                lock,
            )

    def disconnect(self, user_id: str, group_id: str | None, name: str) -> bool:
        """断开连接（用户+群隔离）"""
        key = self.build_connection_key(user_id, group_id, name)
        return self._disconnect_key(key, send_interrupt=True)

    def is_connected(self, user_id: str, group_id: str | None, name: str) -> bool:
        """检查是否已连接（用户+群隔离）"""
        key = self.build_connection_key(user_id, group_id, name)
        client = self.connections.get(key)
        if client is None:
            return False
        try:
            transport = client.get_transport()
            active = transport is not None and transport.is_active()
        except Exception:
            active = False
        if not active:
            self._disconnect_key(key, send_interrupt=False)
        return active

    def get_active_connections(self) -> list[dict[str, str | None]]:
        """返回真实存活的连接，并顺手清除底层已断开的客户端。"""

        active_list: list[dict[str, str | None]] = []
        for key in list(self.connections):
            try:
                user_id, group_id, server_name = self.parse_connection_key(key)
            except ValueError:
                self._disconnect_key(key, send_interrupt=False)
                continue
            if not self.is_connected(user_id, group_id, server_name):
                continue
            active_list.append(
                {"user_id": user_id, "group_id": group_id, "server_name": server_name}
            )
        return active_list

    # ──────────────────── 远端命令执行与终止 ────────────────────

    async def stop_command(
        self,
        user_id: str,
        group_id: str | None,
        name: str,
    ) -> CommandTerminationResult:
        """
        停止指定服务器上正在运行的命令（用户+群隔离）

        先尝试向远端进程组发送 TERM/KILL，再无条件关闭本地通道。
        """
        key = self.build_connection_key(user_id, group_id, name)
        return await self._terminate_active_command(key)

    async def _terminate_active_command(self, key: str) -> CommandTerminationResult:
        """串行清理同一隔离键，避免停止、超时和取消重复操作同一通道。"""

        lock = _retain_keyed_lock(
            self._termination_locks,
            self._termination_lock_users,
            key,
        )
        try:
            async with lock:
                return await self._terminate_active_command_locked(key)
        finally:
            _release_keyed_lock(
                self._termination_locks,
                self._termination_lock_users,
                key,
                lock,
            )

    async def _terminate_active_command_locked(self, key: str) -> CommandTerminationResult:
        captured = self.active_channels.get(key)
        if captured is None:
            return CommandTerminationResult(
                found=False,
                local_cleaned=True,
                remote_confirmed=False,
            )
        channel = self._active_channel(captured)
        remote_pid: int | None = None
        if isinstance(captured, dict):
            candidate_pid = captured.get("remote_pid")
            if type(candidate_pid) is int and candidate_pid > 0:
                remote_pid = candidate_pid
        client = self.connections.get(key)
        remote_confirmed = False
        signal_attempted = False
        errors: list[str] = []

        async def wait_for_remote_exit() -> bool:
            if channel is None:
                return False
            for _ in range(20):
                try:
                    if channel.exit_status_ready() or not getattr(channel, "active", True):
                        return True
                except Exception as exc:
                    errors.append(f"channel status error_type={audit_error_type(exc)}")
                    return False
                await asyncio.sleep(0.1)
            return False

        try:
            if remote_pid is not None and client is not None:
                for signal_name in ("TERM", "KILL"):
                    signal_attempted = True
                    command = f"kill -{signal_name} -- -{remote_pid}"
                    try:
                        await asyncio.wait_for(
                            asyncio.to_thread(client.exec_command, command),
                            timeout=5,
                        )
                    except Exception as exc:
                        errors.append(f"{signal_name} error_type={audit_error_type(exc)}")
                        continue
                    if await wait_for_remote_exit():
                        remote_confirmed = True
                        break
            else:
                missing = "remote PID" if remote_pid is None else "SSH client"
                errors.append(f"missing {missing}")
        finally:
            local_cleaned = self._close_channel(channel, send_interrupt=False)
            if self.active_channels.get(key) is captured:
                self.active_channels.pop(key, None)

        error = "; ".join(errors) or None
        if not remote_confirmed:
            self._log_sensitive(
                "warning",
                operation="qingssh.terminate",
                status="remote_unknown",
                value=key,
            )
        return CommandTerminationResult(
            found=True,
            local_cleaned=local_cleaned,
            remote_confirmed=remote_confirmed,
            signal_attempted=signal_attempted,
            error=error,
        )

    async def execute_command_stream(
        self,
        user_id: str,
        group_id: str | None,
        name: str,
        command: str,
        output_callback: OutputCallback,
        use_pty: bool = False,
        *,
        timeout: float | None = None,
    ) -> int:
        """
        流式执行命令（用户+群隔离）

        通过回调函数实时推送命令输出。
        """
        effective_timeout = COMMAND_TIMEOUT if timeout is None else float(timeout)
        if not math.isfinite(effective_timeout) or effective_timeout < 0:
            raise ValueError("SSH command timeout must be finite and non-negative")
        operation = self._execute_command_stream_impl(
            user_id,
            group_id,
            name,
            command,
            output_callback,
            use_pty=use_pty,
        )
        try:
            if effective_timeout == 0:
                return await operation
            return await asyncio.wait_for(
                operation,
                timeout=effective_timeout,
            )
        except asyncio.TimeoutError:
            key = self.build_connection_key(user_id, group_id, name)
            termination = await self._terminate_active_command(key)
            if termination.remote_unknown:
                await output_callback("\n⚠️ 远端进程终止状态未知，请登录服务器确认")
            return int(EXIT_CODE_TIMEOUT)
        except asyncio.CancelledError:
            key = self.build_connection_key(user_id, group_id, name)
            cleanup = asyncio.create_task(
                self._terminate_active_command(key),
                name="qingssh-remote-command-cleanup",
            )
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    continue
            try:
                cleanup.result()
            except Exception as exc:
                self._log_sensitive(
                    "error",
                    operation="qingssh.cancel_cleanup",
                    status="failed",
                    value=key,
                    exc=exc,
                )
            raise

    async def _execute_command_stream_impl(
        self,
        user_id: str,
        group_id: str | None,
        name: str,
        command: str,
        output_callback: OutputCallback,
        use_pty: bool = False,
    ) -> int:
        """
        execute_command_stream 的实际执行逻辑。
        """
        if not self.is_connected(user_id, group_id, name):
            await output_callback("❌ 未连接到服务器")
            return int(EXIT_CODE_ERROR)

        key = self.build_connection_key(user_id, group_id, name)

        channel: Any | None = None
        keep_registered = False
        active_record: _ActiveCommand | None = None

        try:
            client = self.connections.get(key)
            if client is None:
                raise RuntimeError("SSH connection disappeared before command start")
            transport = client.get_transport()
            if transport is None:
                raise RuntimeError("SSH transport is unavailable")

            # Paramiko 的建通道、PTY 和 exec_command 都是同步调用，集中在线程池执行。
            def open_channel() -> Any:
                ch = transport.open_session()
                if use_pty:
                    ch.get_pty()
                ch.set_combine_stderr(True)
                wrapped = (
                    f"setsid sh -c {shlex.quote(command)} & pid=$!; "
                    'printf \'__XQ_PID__%s\\n\' "$pid"; wait "$pid"'
                )
                ch.exec_command(wrapped)
                return ch

            channel = await asyncio.to_thread(open_channel)
            if channel is None:
                raise RuntimeError("SSH channel creation returned no channel")

            active_record = {"channel": channel, "remote_pid": None}
            self.active_channels[key] = active_record

            decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

            marker_buffer = ""
            marker_seen = False

            async def emit_data(data: bytes) -> None:
                nonlocal marker_buffer, marker_seen
                text = decoder.decode(data)
                if marker_seen:
                    if text:
                        await output_callback(text)
                    return
                marker_buffer += text
                if "\n" not in marker_buffer:
                    return
                lines = marker_buffer.splitlines(keepends=True)
                marker_buffer = ""
                for line in lines:
                    match = re.fullmatch(r"__XQ_PID__(\d+)\r?\n?", line)
                    if match:
                        if self.active_channels.get(key) is active_record:
                            active_record["remote_pid"] = int(match.group(1))
                        marker_seen = True
                    elif line:
                        await output_callback(line)

            while not channel.exit_status_ready():
                if channel.recv_ready():
                    data = await asyncio.to_thread(channel.recv, 4096)
                    if data:
                        await emit_data(data)

                await asyncio.sleep(0.05)

                if self.active_channels.get(key) is not active_record:
                    return int(EXIT_CODE_INTERRUPTED)

            # recv_ready() 不能代表 EOF，命令结束后继续读取到空字节。
            while True:
                data = await asyncio.to_thread(channel.recv, 4096)
                if not data:
                    break
                await emit_data(data)

            remaining = decoder.decode(b"", final=True)
            if marker_buffer:
                remaining = marker_buffer + remaining
            if remaining:
                await output_callback(remaining)

            return int(channel.exit_status)

        except asyncio.CancelledError:
            # 已注册的通道交给外层取消处理发送 TERM/KILL；注册前则由 finally 回收。
            keep_registered = active_record is not None
            raise
        except Exception as exc:
            await output_callback(
                "\n❌ "
                + self._unexpected_error_message(
                    exc,
                    component="qingssh.execute_stream",
                )
            )
            self._log_sensitive(
                "error",
                operation="qingssh.execute_stream",
                status="failed",
                value=command,
                exc=exc,
            )
            return int(EXIT_CODE_ERROR)
        finally:
            if not keep_registered and self.active_channels.get(key) is active_record:
                self.active_channels.pop(key, None)
            if channel is not None and not keep_registered:
                try:
                    await asyncio.to_thread(channel.close)
                except Exception as exc:
                    self._log_sensitive(
                        "warning",
                        operation="qingssh.command_channel_close",
                        status="failed",
                        value=key,
                        exc=exc,
                    )

    async def execute_command(
        self, user_id: str, group_id: str | None, name: str, command: str
    ) -> tuple[bool, str]:
        """执行命令并返回受长度限制的完整输出。"""
        if not self.is_connected(user_id, group_id, name):
            return False, "❌ 未连接到服务器"

        output_buffer: list[str] = []

        async def collector(text: str) -> None:
            output_buffer.append(text)

        try:
            exit_code = await self.execute_command_stream(
                user_id, group_id, name, command, collector
            )

            result = "".join(output_buffer)
            if exit_code == EXIT_CODE_TIMEOUT:
                return False, f"❌ 命令执行超时 ({COMMAND_TIMEOUT}s)"
            if len(result) > MAX_OUTPUT_LENGTH:
                result = result[:MAX_OUTPUT_LENGTH] + "\n\n... (输出被截断)"
            output = result.strip() or "(无输出)"
            if exit_code != 0:
                return False, output
            return True, output
        except Exception as exc:
            return False, self._unexpected_error_message(
                exc,
                component="qingssh.execute",
            )

    # ──────────────────── SFTP 文件读取 ────────────────────

    async def download_file(
        self,
        user_id: str,
        group_id: str | None,
        name: str,
        remote_path: str,
        local_path: str,
        *,
        max_bytes: int = 10 * 1024 * 1024,
    ) -> tuple[bool, str]:
        """下载单个受大小限制的文件，成功后再原子替换目标路径。"""

        if type(max_bytes) is not int or max_bytes <= 0:
            return False, "❌ 下载大小上限必须是正整数"
        if not self.is_connected(user_id, group_id, name):
            return False, "❌ 未连接到服务器"

        key = self.build_connection_key(user_id, group_id, name)
        client = self.connections.get(key)
        if client is None:
            return False, f"❌ 服务器 '{name}' 未连接"

        target_path = Path(local_path)
        temporary_path = target_path.with_name(f".{target_path.name}.{uuid.uuid4().hex}.part")
        sftp: Any | None = None
        try:
            sftp = await asyncio.to_thread(client.open_sftp)
            if sftp is None:
                raise RuntimeError("SFTP session creation returned no session")
            stat_result = await asyncio.to_thread(sftp.stat, remote_path)
            if int(getattr(stat_result, "st_size", 0)) > max_bytes:
                return False, f"❌ 文件超过 {max_bytes} 字节安全上限"
            await asyncio.to_thread(sftp.get, remote_path, str(temporary_path))
            local_stat = await asyncio.to_thread(temporary_path.stat)
            local_size = local_stat.st_size
            if local_size > max_bytes:
                return False, f"❌ 文件超过 {max_bytes} 字节安全上限"
            await asyncio.to_thread(os.replace, temporary_path, target_path)
            return True, f"✅ 文件已下载: {remote_path}"
        except FileNotFoundError:
            return False, f"❌ 远程文件不存在: {remote_path}"
        except PermissionError:
            return False, f"❌ 权限不足，无法访问: {remote_path}"
        except Exception as exc:
            self._log_sensitive(
                "error",
                operation="qingssh.download",
                status="failed",
                value="\0".join((remote_path, local_path)),
                exc=exc,
            )
            return False, self._unexpected_error_message(
                exc,
                component="qingssh.download",
            )
        finally:
            with suppress(OSError):
                temporary_path.unlink(missing_ok=True)
            if sftp is not None:
                try:
                    await asyncio.to_thread(sftp.close)
                except Exception as exc:
                    self._log(
                        "warning",
                        f"SFTP close status=failed error_type={audit_error_type(exc)}",
                    )

    async def list_files(
        self,
        user_id: str,
        group_id: str | None,
        name: str,
        remote_dir: str,
        pattern: str = "*",
    ) -> tuple[bool, list[str]]:
        """列出远端目录中最多一百个、区分大小写的匹配文件名。"""

        if not self.is_connected(user_id, group_id, name):
            return False, []

        key = self.build_connection_key(user_id, group_id, name)
        client = self.connections.get(key)
        if client is None:
            return False, []

        sftp: Any | None = None
        try:
            sftp = await asyncio.to_thread(client.open_sftp)
            if sftp is None:
                raise RuntimeError("SFTP session creation returned no session")
            all_files = await asyncio.to_thread(sftp.listdir, remote_dir)
            files = sorted(
                filename
                for filename in all_files
                if isinstance(filename, str) and fnmatch.fnmatchcase(filename, pattern)
            )
            return True, files[:100]
        except Exception as exc:
            self._log_sensitive(
                "error",
                operation="qingssh.list_files",
                status="failed",
                value="\0".join((remote_dir, pattern)),
                exc=exc,
            )
            return False, []
        finally:
            if sftp is not None:
                try:
                    await asyncio.to_thread(sftp.close)
                except Exception as exc:
                    self._log(
                        "warning",
                        f"SFTP close status=failed error_type={audit_error_type(exc)}",
                    )

    # ──────────────────── 生命周期关闭 ────────────────────

    def close_all(self) -> None:
        """关闭全部连接和命令通道，供卸载或重启流程调用。"""

        if self.context and hasattr(self.context, "logger"):
            self.context.logger.info(
                "Closing all SSH connections (%d active)",
                len(self.connections),
            )

        for key in set(self.connections) | set(self.active_channels):
            self._disconnect_key(key, send_interrupt=False)
        self.active_channels.clear()

    async def shutdown(self) -> None:
        await asyncio.to_thread(self.close_all)


async def get_manager(context: Any) -> SSHManager:
    """获取上下文共享的管理器，并在并发初始化前先发布唯一实例。"""

    # ContextVar 跟随当前事件循环任务，因此长命令会持续保留其发起请求 ID；
    # 其他请求复用同一管理器时不会覆盖该审计上下文。
    _CURRENT_REQUEST_CONTEXT.set(context)

    state = getattr(context, "state", None)
    if not isinstance(state, dict):
        raise RuntimeError("QingSSH context.state must be a dictionary")

    existing: object | None = state.get("ssh_manager")
    if isinstance(existing, SSHManager):
        await existing.initialize()
        return existing
    if existing is not None and hasattr(context, "logger"):
        context.logger.warning("Replacing invalid QingSSH manager state")

    configured_data_dir = getattr(context, "data_dir", None)
    data_dir = (
        Path(configured_data_dir)
        if isinstance(configured_data_dir, (str, os.PathLike))
        else Path(context.plugin_dir) / "data"
    )
    manager = SSHManager(data_dir, context=context)
    state["ssh_manager"] = manager
    try:
        await manager.initialize()
    except BaseException:
        if state.get("ssh_manager") is manager:
            state.pop("ssh_manager", None)
        raise

    if hasattr(context, "logger"):
        context.logger.info("SSH manager initialized")
    return manager
