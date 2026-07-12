from __future__ import annotations

import asyncio
from typing import Any, cast
from unittest.mock import MagicMock

import pytest

from plugins.minecraft import main as mc_main
from plugins.minecraft import rcon as rcon_module
from plugins.minecraft.rcon import (
    PacketType,
    RconClient,
    RconCommandResult,
    RconErrorKind,
    RconPacket,
    RconProtocolError,
)


class _Reader:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    async def readexactly(self, size: int) -> bytes:
        if size > len(self.payload):
            partial, self.payload = self.payload, b""
            raise asyncio.IncompleteReadError(partial=partial, expected=size)
        result, self.payload = self.payload[:size], self.payload[size:]
        return result


class _TimeoutReader:
    async def readexactly(self, _size: int) -> bytes:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class _Writer:
    def __init__(self) -> None:
        self.payload = b""
        self.closed = False

    def write(self, payload: bytes) -> None:
        self.payload += payload

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None

    def is_closing(self) -> bool:
        return self.closed


def _connected_client(payload: bytes, *, timeout: float = 0.1) -> tuple[RconClient, _Writer]:
    client = RconClient("127.0.0.1", 25575, "secret", timeout=timeout)
    writer = _Writer()
    client._reader = cast(asyncio.StreamReader, _Reader(payload))
    client._writer = cast(asyncio.StreamWriter, writer)
    client._connected = True
    return client, writer


def test_packet_decode_requires_exact_double_nul_and_valid_utf8() -> None:
    valid = RconPacket(7, PacketType.RESPONSE, "ok").encode()
    malformed_terminator = valid[:-1] + b"x"
    malformed_utf8 = struct_packet(7, PacketType.RESPONSE, b"\xff")

    with pytest.raises(RconProtocolError, match="double-NUL"):
        RconPacket.decode(malformed_terminator)
    with pytest.raises(RconProtocolError, match="UTF-8"):
        RconPacket.decode(malformed_utf8)


def struct_packet(request_id: int, packet_type: int, payload: bytes) -> bytes:
    import struct

    body = struct.pack("<ii", request_id, packet_type) + payload + b"\x00\x00"
    return struct.pack("<i", len(body)) + body


@pytest.mark.asyncio
async def test_connect_accepts_optional_empty_prelude(monkeypatch: pytest.MonkeyPatch) -> None:
    encoded = (
        RconPacket(1, PacketType.RESPONSE, "").encode()
        + RconPacket(1, PacketType.COMMAND, "").encode()
    )
    reader = _Reader(encoded)
    writer = _Writer()

    async def open_connection(_host: str, _port: int) -> tuple[Any, Any]:
        return reader, writer

    monkeypatch.setattr(rcon_module.asyncio, "open_connection", open_connection)
    client = RconClient("127.0.0.1", 25575, "secret")

    result = await client.connect()

    assert result.success is True
    assert result.error_kind is None
    assert client.connected is True
    request, remaining = RconPacket.decode(writer.payload)
    assert remaining == b""
    assert request.packet_type == PacketType.LOGIN
    assert request.payload == "secret"


@pytest.mark.asyncio
async def test_connect_distinguishes_auth_rejection(monkeypatch: pytest.MonkeyPatch) -> None:
    reader = _Reader(RconPacket(-1, PacketType.COMMAND, "").encode())
    writer = _Writer()

    async def open_connection(_host: str, _port: int) -> tuple[Any, Any]:
        return reader, writer

    monkeypatch.setattr(rcon_module.asyncio, "open_connection", open_connection)
    client = RconClient("127.0.0.1", 25575, "wrong")

    result = await client.connect()

    assert result.success is False
    assert result.error_kind is RconErrorKind.AUTH
    assert "认证失败" in result.error_message
    assert client.connected is False
    assert writer.closed is True


@pytest.mark.asyncio
async def test_connect_distinguishes_transport_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    async def refuse(_host: str, _port: int) -> tuple[Any, Any]:
        raise ConnectionRefusedError("refused")

    monkeypatch.setattr(rcon_module.asyncio, "open_connection", refuse)
    client = RconClient("127.0.0.1", 25575, "secret")

    result = await client.connect()

    assert result.success is False
    assert result.error_kind is RconErrorKind.TRANSPORT
    assert "连接" in result.error_message


@pytest.mark.asyncio
async def test_connect_distinguishes_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    async def hang(_host: str, _port: int) -> tuple[Any, Any]:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    monkeypatch.setattr(rcon_module.asyncio, "open_connection", hang)
    client = RconClient("127.0.0.1", 25575, "secret", timeout=0.01)

    result = await client.connect()

    assert result.success is False
    assert result.error_kind is RconErrorKind.TIMEOUT
    assert "超时" in result.error_message


@pytest.mark.asyncio
async def test_empty_response_is_a_success_not_a_failure() -> None:
    client, writer = _connected_client(RconPacket(1, PacketType.RESPONSE, "").encode())

    result = await client.command("save-all")

    assert result == RconCommandResult(success=True, response="")
    assert client.connected is True
    assert writer.closed is False


@pytest.mark.asyncio
async def test_command_timeout_is_typed_and_disconnects() -> None:
    client, writer = _connected_client(b"", timeout=0.01)
    client._reader = cast(asyncio.StreamReader, _TimeoutReader())

    result = await client.command("list")

    assert result.success is False
    assert result.response == ""
    assert result.error_kind is RconErrorKind.TIMEOUT
    assert writer.closed is True
    assert client.connected is False


@pytest.mark.asyncio
async def test_protocol_mismatch_returns_no_partial_response_and_disconnects() -> None:
    encoded = (
        RconPacket(1, PacketType.RESPONSE, "a" * 4096).encode()
        + RconPacket(99, PacketType.RESPONSE, "partial-tail").encode()
    )
    client, writer = _connected_client(encoded)

    result = await client.command("list")

    assert result.success is False
    assert result.response == ""
    assert result.error_kind is RconErrorKind.PROTOCOL
    assert writer.closed is True


@pytest.mark.asyncio
async def test_cumulative_response_limit_is_typed_and_disconnects() -> None:
    encoded = (
        RconPacket(1, PacketType.RESPONSE, "a" * 4096).encode()
        + RconPacket(1, PacketType.RESPONSE, "b" * 512).encode()
    )
    client, writer = _connected_client(encoded)
    client.MAX_RESPONSE_BYTES = 4200

    result = await client.command("list")

    assert result.success is False
    assert result.response == ""
    assert result.error_kind is RconErrorKind.RESPONSE_LIMIT
    assert writer.closed is True


@pytest.mark.asyncio
async def test_main_reports_empty_success_and_typed_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeRcon:
        def __init__(self) -> None:
            self.results = [
                RconCommandResult(success=True, response=""),
                RconCommandResult(
                    success=False,
                    error_kind=RconErrorKind.TIMEOUT,
                    error_message="RCON 操作超时，未收到完整响应",
                ),
            ]

        async def command(self, _command: str) -> RconCommandResult:
            return self.results.pop(0)

    manager = mc_main.ConnectionManager()
    manager.add_connection(
        mc_main.McConnection(
            host="127.0.0.1",
            port=25575,
            password="secret",
            log_file="",
            target_type="private",
            target_id=42,
            rcon_client=cast(Any, FakeRcon()),
            log_monitor=None,
        )
    )
    monkeypatch.setattr(mc_main, "_manager", manager)

    empty = await mc_main._handle_mc_message("save-all", {"user_id": 42}, MagicMock())
    failure = await mc_main._handle_mc_message("list", {"user_id": 42}, MagicMock())

    assert "执行成功（空响应）" in empty[0]["data"]["text"]
    assert "操作超时" in failure[0]["data"]["text"]
    assert "无返回" not in failure[0]["data"]["text"]
