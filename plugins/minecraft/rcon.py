"""Minecraft 插件使用的严格异步 Source RCON 客户端。"""

from __future__ import annotations

import asyncio
import logging
import struct
from dataclasses import dataclass
from enum import Enum, IntEnum

from core.sensitive_audit import SensitiveAuditSummary, summarize_sensitive

from .audit import audit_error_type

logger = logging.getLogger(__name__)

# Minecraft 按最多 4096 个 Java 字符拆分响应；UTF-8 最坏需要四倍字节空间。
RCON_MAX_CHUNK_UNITS = 4096
RCON_MAX_PACKET_BODY_BYTES = RCON_MAX_CHUNK_UNITS * 4 + 10
RCON_MAX_OUTBOUND_PAYLOAD_BYTES = 4096


class PacketType(IntEnum):
    """Source RCON 包类型；认证响应与命令请求共用数值 2。"""

    RESPONSE = 0
    COMMAND = 2
    LOGIN = 3


class RconErrorKind(str, Enum):
    """可安全返回给命令层的失败分类。"""

    INPUT = "input"
    TRANSPORT = "transport"
    AUTH = "auth"
    TIMEOUT = "timeout"
    PROTOCOL = "protocol"
    RESPONSE_LIMIT = "response_limit"
    INTERNAL = "internal"


@dataclass(frozen=True, slots=True)
class RconConnectResult:
    success: bool
    error_kind: RconErrorKind | None = None
    error_message: str = ""


@dataclass(frozen=True, slots=True)
class RconCommandResult:
    success: bool
    response: str = ""
    error_kind: RconErrorKind | None = None
    error_message: str = ""
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class _CollectedResponse:
    """命令响应及其可选续包是否因短超时而结束。"""

    payload: str
    truncated: bool = False


class RconProtocolError(ValueError):
    """对端返回了畸形包或乱序包。"""


class RconAuthenticationError(PermissionError):
    """服务端明确拒绝了 RCON 密码。"""


class RconResponseLimitError(ValueError):
    """累计命令响应超过内存安全上限。"""


@dataclass(frozen=True, slots=True)
class RconPacket:
    request_id: int
    packet_type: int
    payload: str

    def encode(self) -> bytes:
        """编码一个完整包，并拒绝会破坏双 NUL 边界的载荷。"""

        if "\0" in self.payload:
            raise RconProtocolError("RCON payload contains an embedded NUL")
        payload_bytes = self.payload.encode("utf-8")
        length = 10 + len(payload_bytes)
        if length > RCON_MAX_PACKET_BODY_BYTES:
            raise RconProtocolError("RCON packet exceeds the safety limit")
        return (
            struct.pack("<iii", length, self.request_id, self.packet_type)
            + payload_bytes
            + b"\x00\x00"
        )

    @classmethod
    def decode(cls, data: bytes) -> tuple[RconPacket, bytes]:
        """从缓冲区解出一个包，并返回尚未消费的后续字节。"""

        if len(data) < 4:
            raise RconProtocolError("RCON packet header is incomplete")
        length = struct.unpack("<i", data[:4])[0]
        if length < 10 or length > RCON_MAX_PACKET_BODY_BYTES:
            raise RconProtocolError(f"invalid RCON packet length: {length}")
        packet_end = 4 + length
        if len(data) < packet_end:
            raise RconProtocolError("RCON packet body is incomplete")

        body = data[4:packet_end]
        if body[-2:] != b"\x00\x00":
            raise RconProtocolError("RCON packet is missing the double-NUL terminator")
        payload_bytes = body[8:-2]
        if b"\x00" in payload_bytes:
            raise RconProtocolError("RCON packet contains an embedded NUL")
        request_id, packet_type = struct.unpack("<ii", body[:8])
        try:
            payload = payload_bytes.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise RconProtocolError("RCON packet payload is not valid UTF-8") from exc
        return cls(request_id, packet_type, payload), data[packet_end:]


class RconClient:
    """单连接、串行执行并在失败后自动重建的 Source RCON 客户端。"""

    MAX_PACKET_BYTES = RCON_MAX_PACKET_BODY_BYTES
    MAX_RESPONSE_BYTES = 1024 * 1024
    RESPONSE_CHUNK_TIMEOUT = 0.5

    def __init__(self, host: str, port: int, password: str, timeout: float = 10.0) -> None:
        if (
            not isinstance(host, str)
            or not host
            or host != host.strip()
            or len(host) > 253
            or any(char.isspace() or ord(char) < 32 for char in host)
        ):
            raise ValueError("RCON host is invalid")
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError("RCON port is invalid")
        if (
            not isinstance(password, str)
            or not password
            or "\0" in password
            or len(password.encode("utf-8")) > RCON_MAX_OUTBOUND_PAYLOAD_BYTES
        ):
            raise ValueError("RCON password is invalid")
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not 0.01 <= float(timeout) <= 60.0
        ):
            raise ValueError("RCON timeout is invalid")

        self.host = host
        self.port = port
        self.password = password
        self.timeout = float(timeout)
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._request_id = 0
        # 建连、认证、命令和关闭共享一把锁，避免并发建连覆盖 reader/writer。
        self._operation_lock = asyncio.Lock()
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected and self._writer is not None and not self._writer.is_closing()

    @staticmethod
    def _classify_error(exc: BaseException) -> tuple[RconErrorKind, str]:
        if isinstance(exc, RconAuthenticationError):
            return RconErrorKind.AUTH, "RCON 认证失败，请检查密码"
        if isinstance(exc, RconResponseLimitError):
            return RconErrorKind.RESPONSE_LIMIT, "RCON 响应超过安全上限，连接已重置"
        if isinstance(exc, TimeoutError):
            return RconErrorKind.TIMEOUT, "RCON 操作超时，未收到完整响应"
        if isinstance(exc, RconProtocolError):
            return RconErrorKind.PROTOCOL, "RCON 协议响应无效，连接已重置"
        if isinstance(exc, (ConnectionError, OSError)):
            return RconErrorKind.TRANSPORT, "RCON 连接不可用或已断开"
        return RconErrorKind.INTERNAL, "RCON 内部错误，连接已重置"

    def _next_request_id(self) -> int:
        self._request_id = 1 if self._request_id >= 2_147_483_647 else self._request_id + 1
        return self._request_id

    async def connect(self) -> RconConnectResult:
        """串行建连和认证，并保留可公开的失败分类。"""

        async with self._operation_lock:
            return await self._connect_locked()

    async def _connect_locked(self) -> RconConnectResult:
        if self.connected:
            return RconConnectResult(success=True)
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port),
                timeout=self.timeout,
            )
            await self._authenticate()
            self._connected = True
            self._log_connect(status="success")
            return RconConnectResult(success=True)
        except asyncio.CancelledError:
            await self._disconnect_locked()
            raise
        except Exception as exc:
            kind, message = self._classify_error(exc)
            self._log_connect(status="failed", error_kind=kind, exc=exc)
            await self._disconnect_locked()
            return RconConnectResult(False, error_kind=kind, error_message=message)

    def _log_connect(
        self,
        *,
        status: str,
        error_kind: RconErrorKind | None = None,
        exc: BaseException | None = None,
    ) -> None:
        target_audit = summarize_sensitive(f"{self.host}\0{self.port}")
        logger.log(
            logging.INFO if status == "success" else logging.ERROR,
            "sensitive_audit operation=minecraft.rcon_connect status=%s error_kind=%s "
            "error_type=%s payload_kind=%s payload_length=%d payload_bytes=%d "
            "payload_fingerprint=%s",
            status,
            error_kind.value if error_kind is not None else "-",
            audit_error_type(exc),
            target_audit.kind,
            target_audit.length,
            target_audit.byte_length,
            target_audit.fingerprint,
        )

    async def disconnect(self) -> None:
        """串行关闭连接；重复调用不会重复操作旧 writer。"""

        async with self._operation_lock:
            await self._disconnect_locked()

    async def _disconnect_locked(self) -> None:
        was_open = self._connected or self._writer is not None
        self._connected = False
        writer, self._writer = self._writer, None
        self._reader = None
        if writer is not None:
            try:
                writer.close()
                await asyncio.wait_for(writer.wait_closed(), timeout=self.timeout)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "RCON close status=failed error_type=%s",
                    audit_error_type(exc),
                )
        if was_open:
            logger.info("RCON disconnected")

    async def command(self, cmd: str) -> RconCommandResult:
        """执行管理员命令；输入、响应和失败均受固定边界约束。"""

        input_error = self._validate_command(cmd)
        if input_error:
            return RconCommandResult(
                success=False,
                error_kind=RconErrorKind.INPUT,
                error_message=input_error,
            )

        command_audit = summarize_sensitive(cmd)
        async with self._operation_lock:
            if not self.connected:
                connection = await self._connect_locked()
                if not connection.success:
                    return RconCommandResult(
                        success=False,
                        error_kind=connection.error_kind,
                        error_message=connection.error_message,
                    )
            try:
                response = await self._send_command(cmd)
                self._log_command(command_audit, response=response.payload)
                return RconCommandResult(
                    success=True,
                    response=response.payload,
                    truncated=response.truncated,
                )
            except asyncio.CancelledError:
                await self._disconnect_locked()
                raise
            except Exception as exc:
                kind, message = self._classify_error(exc)
                self._log_command(command_audit, error_kind=kind, exc=exc)
                await self._disconnect_locked()
                return RconCommandResult(False, error_kind=kind, error_message=message)

    @staticmethod
    def _validate_command(cmd: object) -> str:
        if not isinstance(cmd, str) or not cmd.strip():
            return "RCON 命令不能为空"
        if "\0" in cmd:
            return "RCON 命令包含不允许的控制字符"
        if len(cmd.encode("utf-8")) > RCON_MAX_OUTBOUND_PAYLOAD_BYTES:
            return "RCON 命令超过 4096 字节安全上限"
        return ""

    @staticmethod
    def _log_command(
        command_audit: SensitiveAuditSummary,
        *,
        response: str | None = None,
        error_kind: RconErrorKind | None = None,
        exc: BaseException | None = None,
    ) -> None:
        command = command_audit
        if response is not None:
            response_audit = summarize_sensitive(response)
            logger.info(
                "sensitive_audit operation=minecraft.rcon_command status=success "
                "payload_kind=%s payload_length=%d payload_bytes=%d payload_fingerprint=%s "
                "response_kind=%s response_length=%d response_bytes=%d "
                "response_fingerprint=%s",
                command.kind,
                command.length,
                command.byte_length,
                command.fingerprint,
                response_audit.kind,
                response_audit.length,
                response_audit.byte_length,
                response_audit.fingerprint,
            )
            return
        logger.error(
            "sensitive_audit operation=minecraft.rcon_command status=failed "
            "error_kind=%s error_type=%s payload_kind=%s payload_length=%d "
            "payload_bytes=%d payload_fingerprint=%s",
            error_kind.value if error_kind is not None else "internal",
            audit_error_type(exc),
            command.kind,
            command.length,
            command.byte_length,
            command.fingerprint,
        )

    async def _write_packet(self, packet: RconPacket) -> None:
        if self._writer is None or self._reader is None:
            raise ConnectionError("RCON connection is not open")
        self._writer.write(packet.encode())
        await asyncio.wait_for(self._writer.drain(), timeout=self.timeout)

    async def _read_packet(self, timeout: float) -> RconPacket:
        if self._reader is None:
            raise ConnectionError("RCON connection is not open")
        try:
            header = await asyncio.wait_for(self._reader.readexactly(4), timeout=timeout)
        except asyncio.IncompleteReadError as exc:
            if not exc.partial:
                raise ConnectionError("RCON peer closed the connection") from exc
            raise RconProtocolError("RCON packet header was truncated") from exc
        packet_length = struct.unpack("<i", header)[0]
        if packet_length < 10 or packet_length > self.MAX_PACKET_BYTES:
            raise RconProtocolError(f"invalid RCON packet length: {packet_length}")
        try:
            body = await asyncio.wait_for(
                self._reader.readexactly(packet_length),
                timeout=timeout,
            )
        except asyncio.IncompleteReadError as exc:
            raise RconProtocolError("RCON packet body was truncated") from exc
        response, _remaining = RconPacket.decode(header + body)
        return response

    @staticmethod
    def _validate_response(
        response: RconPacket,
        *,
        request_id: int,
        packet_type: PacketType,
    ) -> None:
        if response.request_id != request_id:
            raise RconProtocolError(
                f"RCON response request id mismatch: expected {request_id}, got {response.request_id}"
            )
        if response.packet_type != int(packet_type):
            raise RconProtocolError(
                f"RCON response type mismatch: expected {int(packet_type)}, got {response.packet_type}"
            )

    async def _authenticate(self) -> None:
        request_id = self._next_request_id()
        await self._write_packet(RconPacket(request_id, PacketType.LOGIN, self.password))
        response = await self._read_packet(self.timeout)

        # 部分 Source 服务端会先回一个同 ID 的空 RESPONSE，再回认证结果。
        if (
            response.request_id == request_id
            and response.packet_type == int(PacketType.RESPONSE)
            and response.payload == ""
        ):
            response = await self._read_packet(self.timeout)

        if response.request_id == -1:
            if response.packet_type != int(PacketType.COMMAND):
                raise RconProtocolError("RCON auth rejection used an invalid packet type")
            raise RconAuthenticationError("RCON password was rejected")
        self._validate_response(
            response,
            request_id=request_id,
            packet_type=PacketType.COMMAND,
        )
        if response.payload:
            raise RconProtocolError("RCON auth response payload must be empty")

    @staticmethod
    def _may_have_continuation(payload: str) -> bool:
        payload_bytes = payload.encode("utf-8")
        java_units = len(payload.encode("utf-16-le")) // 2
        return len(payload_bytes) == RCON_MAX_CHUNK_UNITS or java_units == RCON_MAX_CHUNK_UNITS

    async def _send_command(self, payload: str) -> _CollectedResponse:
        request_id = self._next_request_id()
        await self._write_packet(RconPacket(request_id, PacketType.COMMAND, payload))
        response = await self._read_packet(self.timeout)
        self._validate_response(
            response,
            request_id=request_id,
            packet_type=PacketType.RESPONSE,
        )

        payload_parts = [response.payload]
        response_bytes = len(response.payload.encode("utf-8"))
        if response_bytes > self.MAX_RESPONSE_BYTES:
            raise RconResponseLimitError("RCON cumulative response exceeded the safety limit")
        response_deadline = asyncio.get_running_loop().time() + self.timeout
        truncated = False

        while self._may_have_continuation(response.payload):
            remaining = response_deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError("RCON cumulative response timed out")
            try:
                response = await self._read_packet(min(self.RESPONSE_CHUNK_TIMEOUT, remaining))
            except TimeoutError:
                # Minecraft 对“长度恰为整块”的响应不发送额外终止包。
                truncated = True
                break
            self._validate_response(
                response,
                request_id=request_id,
                packet_type=PacketType.RESPONSE,
            )
            if not response.payload:
                break
            response_bytes += len(response.payload.encode("utf-8"))
            if response_bytes > self.MAX_RESPONSE_BYTES:
                raise RconResponseLimitError("RCON cumulative response exceeded the safety limit")
            payload_parts.append(response.payload)

        return _CollectedResponse("".join(payload_parts), truncated=truncated)


__all__ = [
    "PacketType",
    "RconClient",
    "RconCommandResult",
    "RconConnectResult",
    "RconErrorKind",
    "RconPacket",
    "RconProtocolError",
    "RconResponseLimitError",
]
