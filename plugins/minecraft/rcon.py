"""Strict asynchronous Source RCON client used by the Minecraft plugin."""

from __future__ import annotations

import asyncio
import logging
import struct
from dataclasses import dataclass
from enum import Enum, IntEnum

from core.sensitive_audit import summarize_sensitive

from .audit import audit_error_type

logger = logging.getLogger(__name__)


class PacketType(IntEnum):
    """Source RCON packet types (auth responses share value 2)."""

    RESPONSE = 0
    COMMAND = 2
    LOGIN = 3


class RconErrorKind(str, Enum):
    TRANSPORT = "transport"
    AUTH = "auth"
    TIMEOUT = "timeout"
    PROTOCOL = "protocol"
    RESPONSE_LIMIT = "response_limit"
    INTERNAL = "internal"


@dataclass(frozen=True)
class RconConnectResult:
    success: bool
    error_kind: RconErrorKind | None = None
    error_message: str = ""

    def __bool__(self) -> bool:
        return self.success


@dataclass(frozen=True)
class RconCommandResult:
    success: bool
    response: str = ""
    error_kind: RconErrorKind | None = None
    error_message: str = ""

    def __bool__(self) -> bool:
        return self.success


class RconProtocolError(ValueError):
    """The peer returned a malformed or out-of-sequence packet."""


class RconAuthenticationError(PermissionError):
    """The server explicitly rejected the RCON password."""


class RconResponseLimitError(ValueError):
    """The accumulated command response exceeded the memory safety limit."""


@dataclass
class RconPacket:
    request_id: int
    packet_type: int
    payload: str

    def encode(self) -> bytes:
        payload_bytes = self.payload.encode("utf-8") + b"\x00\x00"
        length = 8 + len(payload_bytes)
        return struct.pack("<iii", length, self.request_id, self.packet_type) + payload_bytes

    @classmethod
    def decode(cls, data: bytes) -> tuple[RconPacket, bytes]:
        if len(data) < 4:
            raise RconProtocolError("RCON packet header is incomplete")
        length = struct.unpack("<i", data[:4])[0]
        if length < 10:
            raise RconProtocolError(f"invalid RCON packet length: {length}")
        packet_end = 4 + length
        if len(data) < packet_end:
            raise RconProtocolError("RCON packet body is incomplete")
        body = data[4:packet_end]
        if body[-2:] != b"\x00\x00":
            raise RconProtocolError("RCON packet is missing the double-NUL terminator")
        request_id, packet_type = struct.unpack("<ii", body[:8])
        try:
            payload = body[8:-2].decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise RconProtocolError("RCON packet payload is not valid UTF-8") from exc
        return cls(request_id, packet_type, payload), data[packet_end:]


class RconClient:
    """One serialized, reconnecting Source RCON connection."""

    MAX_COMMAND_RESPONSE_CHUNK_SIZE = 4096
    MAX_PACKET_BYTES = 1024 * 1024
    MAX_RESPONSE_BYTES = 4 * 1024 * 1024
    RESPONSE_CHUNK_TIMEOUT = 0.2

    def __init__(self, host: str, port: int, password: str, timeout: float = 10.0):
        self.host = host
        self.port = port
        self.password = password
        self.timeout = timeout
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._request_id = 0
        self._lock = asyncio.Lock()
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
        """Connect and authenticate, preserving a typed failure reason."""

        if self.connected:
            return RconConnectResult(success=True)
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port),
                timeout=self.timeout,
            )
            await self._authenticate()
            self._connected = True
            target_audit = summarize_sensitive(f"{self.host}\0{self.port}")
            logger.info(
                "sensitive_audit operation=minecraft.rcon_connect status=success "
                "payload_kind=%s payload_length=%d payload_bytes=%d payload_fingerprint=%s",
                target_audit.kind,
                target_audit.length,
                target_audit.byte_length,
                target_audit.fingerprint,
            )
            return RconConnectResult(success=True)
        except asyncio.CancelledError:
            await self.disconnect()
            raise
        except Exception as exc:
            kind, message = self._classify_error(exc)
            target_audit = summarize_sensitive(f"{self.host}\0{self.port}")
            logger.error(
                "sensitive_audit operation=minecraft.rcon_connect status=failed "
                "error_kind=%s error_type=%s payload_kind=%s payload_length=%d "
                "payload_bytes=%d payload_fingerprint=%s",
                kind.value,
                audit_error_type(exc),
                target_audit.kind,
                target_audit.length,
                target_audit.byte_length,
                target_audit.fingerprint,
            )
            await self.disconnect()
            return RconConnectResult(
                success=False,
                error_kind=kind,
                error_message=message,
            )

    async def disconnect(self) -> None:
        self._connected = False
        writer, self._writer = self._writer, None
        self._reader = None
        if writer is not None:
            try:
                writer.close()
                await asyncio.wait_for(writer.wait_closed(), timeout=self.timeout)
            except Exception:
                pass
        logger.info("RCON disconnected")

    async def command(self, cmd: str) -> RconCommandResult:
        """Execute an unrestricted admin command and return a typed outcome."""

        command_audit = summarize_sensitive(cmd)
        if not self.connected:
            connection = await self.connect()
            if not connection.success:
                return RconCommandResult(
                    success=False,
                    error_kind=connection.error_kind,
                    error_message=connection.error_message,
                )
        try:
            response = await self._send_command(cmd)
            response_audit = summarize_sensitive(response.payload)
            logger.info(
                "sensitive_audit operation=minecraft.rcon_command status=success "
                "payload_kind=%s payload_length=%d payload_bytes=%d payload_fingerprint=%s "
                "response_kind=%s response_length=%d response_bytes=%d "
                "response_fingerprint=%s",
                command_audit.kind,
                command_audit.length,
                command_audit.byte_length,
                command_audit.fingerprint,
                response_audit.kind,
                response_audit.length,
                response_audit.byte_length,
                response_audit.fingerprint,
            )
            return RconCommandResult(success=True, response=response.payload)
        except asyncio.CancelledError:
            await self.disconnect()
            raise
        except Exception as exc:
            kind, message = self._classify_error(exc)
            logger.error(
                "sensitive_audit operation=minecraft.rcon_command status=failed "
                "error_kind=%s error_type=%s payload_kind=%s payload_length=%d "
                "payload_bytes=%d payload_fingerprint=%s",
                kind.value,
                audit_error_type(exc),
                command_audit.kind,
                command_audit.length,
                command_audit.byte_length,
                command_audit.fingerprint,
            )
            await self.disconnect()
            return RconCommandResult(
                success=False,
                error_kind=kind,
                error_message=message,
            )

    async def _write_packet(self, packet: RconPacket) -> None:
        if self._writer is None or self._reader is None:
            raise ConnectionError("RCON connection is not open")
        self._writer.write(packet.encode())
        await asyncio.wait_for(self._writer.drain(), timeout=self.timeout)

    async def _read_packet(self, timeout: float) -> tuple[RconPacket, int]:
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
        response, remaining = RconPacket.decode(header + body)
        if remaining:
            raise RconProtocolError("RCON decoder left unexpected packet bytes")
        return response, packet_length

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
        async with self._lock:
            request_id = self._next_request_id()
            await self._write_packet(RconPacket(request_id, PacketType.LOGIN, self.password))
            response, _packet_length = await self._read_packet(self.timeout)

            if (
                response.request_id == request_id
                and response.packet_type == int(PacketType.RESPONSE)
                and response.payload == ""
            ):
                response, _packet_length = await self._read_packet(self.timeout)

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

    async def _send_command(self, payload: str) -> RconPacket:
        async with self._lock:
            request_id = self._next_request_id()
            await self._write_packet(RconPacket(request_id, PacketType.COMMAND, payload))
            response, packet_length = await self._read_packet(self.timeout)
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

            while packet_length - 10 == self.MAX_COMMAND_RESPONSE_CHUNK_SIZE:
                remaining = response_deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise TimeoutError("RCON cumulative response timed out")
                next_response, packet_length = await self._read_packet(
                    min(self.RESPONSE_CHUNK_TIMEOUT, remaining)
                )
                self._validate_response(
                    next_response,
                    request_id=request_id,
                    packet_type=PacketType.RESPONSE,
                )
                if not next_response.payload:
                    break
                response_bytes += len(next_response.payload.encode("utf-8"))
                if response_bytes > self.MAX_RESPONSE_BYTES:
                    raise RconResponseLimitError(
                        "RCON cumulative response exceeded the safety limit"
                    )
                payload_parts.append(next_response.payload)

            response.payload = "".join(payload_parts)
            return response

    async def _send_packet(self, packet_type: PacketType, payload: str) -> RconPacket:
        """Compatibility shim retained for protocol-level tests."""

        if packet_type == PacketType.COMMAND:
            return await self._send_command(payload)
        if packet_type == PacketType.LOGIN:
            await self._authenticate()
            return RconPacket(self._request_id, PacketType.COMMAND, "")
        raise RconProtocolError(f"unsupported outbound RCON packet type: {int(packet_type)}")

    async def __aenter__(self) -> RconClient:
        result = await self.connect()
        if not result.success:
            raise ConnectionError(result.error_message)
        return self

    async def __aexit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        await self.disconnect()


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
