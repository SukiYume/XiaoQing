"""Source RCON 编解码、错误分类和并发状态机契约。"""

from __future__ import annotations

import asyncio
import logging
import struct
from typing import Any, cast

import pytest

from plugins.minecraft import rcon as rcon_module
from plugins.minecraft.rcon import (
    PacketType,
    RconClient,
    RconCommandResult,
    RconErrorKind,
    RconPacket,
    RconProtocolError,
    RconResponseLimitError,
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


class _PayloadThenTimeoutReader(_Reader):
    async def readexactly(self, size: int) -> bytes:
        if self.payload:
            return await super().readexactly(size)
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class _Writer:
    def __init__(self) -> None:
        self.payload     = b""
        self.closed      = False
        self.close_calls = 0

    def write(self, payload: bytes) -> None:
        self.payload += payload

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True
        self.close_calls += 1

    async def wait_closed(self) -> None:
        return None

    def is_closing(self) -> bool:
        return self.closed


class _BrokenCloseWriter(_Writer):
    async def wait_closed(self) -> None:
        raise OSError("close failed")


def _connected_client(
    payload: bytes,
    *,
    timeout: float         = 0.1,
    reader: Any            = None,
    writer: _Writer | None = None,
) -> tuple[RconClient, _Writer]:
    client = RconClient("127.0.0.1", 25575, "secret", timeout=timeout)
    actual_writer     = writer or _Writer()
    client._reader    = cast(asyncio.StreamReader, reader or _Reader(payload))  # noqa: SLF001
    client._writer    = cast(asyncio.StreamWriter, actual_writer)  # noqa: SLF001
    client._connected = True  # noqa: SLF001
    return client, actual_writer


def _packet(request_id: int, packet_type: int, payload: bytes) -> bytes:
    body = struct.pack("<ii", request_id, packet_type) + payload + b"\x00\x00"
    return struct.pack("<i", len(body)) + body


class TestRconPacket:
    def test_round_trip_and_remaining_buffer(self) -> None:
        first  = RconPacket(7, PacketType.RESPONSE, "你好").encode()
        second = RconPacket(8, PacketType.RESPONSE, "tail").encode()
        decoded, remaining = RconPacket.decode(first + second)
        assert decoded == RconPacket(7, PacketType.RESPONSE, "你好")
        assert remaining == second
        assert struct.unpack("<i", first[:4])[0] == len(first) - 4

    @pytest.mark.parametrize(
        "payload, match",
        [
            (b"", "header"),
            (struct.pack("<i", 9) + b"x" * 9, "length"),
            (struct.pack("<i", 20) + b"x" * 10, "body"),
        ],
    )
    def test_decode_rejects_incomplete_or_invalid_lengths(self, payload: bytes, match: str) -> None:
        with pytest.raises(RconProtocolError, match=match):
            RconPacket.decode(payload)

    def test_decode_requires_double_nul_valid_utf8_and_no_embedded_nul(self) -> None:
        valid = RconPacket(7, PacketType.RESPONSE, "ok").encode()
        with pytest.raises(RconProtocolError, match="double-NUL"):
            RconPacket.decode(valid[:-1] + b"x")
        with pytest.raises(RconProtocolError, match="UTF-8"):
            RconPacket.decode(_packet(7, PacketType.RESPONSE, b"\xff"))
        with pytest.raises(RconProtocolError, match="embedded NUL"):
            RconPacket.decode(_packet(7, PacketType.RESPONSE, b"a\x00b"))

    def test_encode_rejects_embedded_nul_and_oversized_payload(self) -> None:
        with pytest.raises(RconProtocolError, match="embedded NUL"):
            RconPacket(1, PacketType.COMMAND, "a\0b").encode()
        with pytest.raises(RconProtocolError, match="safety limit"):
            RconPacket(1, PacketType.COMMAND, "x" * RconClient.MAX_PACKET_BYTES).encode()


class TestRconConstruction:
    @pytest.mark.parametrize(
        "args",
        [
            ("", 25575, "secret", 1.0),
            (" bad", 25575, "secret", 1.0),
            ("bad host", 25575, "secret", 1.0),
            ("host", True, "secret", 1.0),
            ("host", 0, "secret", 1.0),
            ("host", 65536, "secret", 1.0),
            ("host", 25575, "", 1.0),
            ("host", 25575, "bad\0secret", 1.0),
            ("host", 25575, "x" * 4097, 1.0),
            ("host", 25575, "secret", True),
            ("host", 25575, "secret", 0.001),
            ("host", 25575, "secret", 61.0),
        ],
    )
    def test_constructor_rejects_invalid_network_and_secret_bounds(
        self,
        args: tuple[Any, ...],
    ) -> None:
        with pytest.raises(ValueError):
            RconClient(*args)

    def test_request_id_wraps_before_signed_integer_overflow(self) -> None:
        client             = RconClient("host", 25575, "secret")
        client._request_id = 2_147_483_647  # noqa: SLF001
        assert client._next_request_id() == 1  # noqa: SLF001


class TestRconConnection:
    @pytest.mark.asyncio
    async def test_connect_accepts_optional_empty_prelude(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        reader = _Reader(
            RconPacket(1, PacketType.RESPONSE, "").encode()
            + RconPacket(1, PacketType.COMMAND, "").encode()
        )
        writer = _Writer()

        async def open_connection(_host: str, _port: int) -> tuple[Any, Any]:
            return reader, writer

        monkeypatch.setattr(rcon_module.asyncio, "open_connection", open_connection)
        client = RconClient("127.0.0.1", 25575, "secret")

        result = await client.connect()

        request, remaining = RconPacket.decode(writer.payload)
        assert result.success is True
        assert client.connected is True
        assert remaining == b""
        assert request.packet_type == PacketType.LOGIN
        assert request.payload == "secret"

    @pytest.mark.asyncio
    async def test_concurrent_connect_opens_and_authenticates_once(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls  = 0
        reader = _Reader(RconPacket(1, PacketType.COMMAND, "").encode())
        writer = _Writer()

        async def open_connection(_host: str, _port: int) -> tuple[Any, Any]:
            nonlocal calls
            calls += 1
            await asyncio.sleep(0)
            return reader, writer

        monkeypatch.setattr(rcon_module.asyncio, "open_connection", open_connection)
        client = RconClient("127.0.0.1", 25575, "secret")

        first, second = await asyncio.gather(client.connect(), client.connect())

        assert first.success and second.success
        assert calls == 1

    @pytest.mark.asyncio
    async def test_auth_rejection_is_typed_and_closes_writer(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        reader = _Reader(RconPacket(-1, PacketType.COMMAND, "").encode())
        writer = _Writer()

        async def open_connection(_host: str, _port: int) -> tuple[Any, Any]:
            return reader, writer

        monkeypatch.setattr(rcon_module.asyncio, "open_connection", open_connection)
        client = RconClient("127.0.0.1", 25575, "wrong")

        result = await client.connect()

        assert result.error_kind is RconErrorKind.AUTH
        assert "认证失败" in result.error_message
        assert writer.closed is True
        assert client.connected is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "failure, kind",
        [
            (ConnectionRefusedError("refused"), RconErrorKind.TRANSPORT),
            (RconProtocolError("bad packet"), RconErrorKind.PROTOCOL),
        ],
    )
    async def test_connect_classifies_open_failures(
        self,
        monkeypatch: pytest.MonkeyPatch,
        failure: BaseException,
        kind: RconErrorKind,
    ) -> None:
        async def fail(_host: str, _port: int) -> tuple[Any, Any]:
            raise failure

        monkeypatch.setattr(rcon_module.asyncio, "open_connection", fail)
        result = await RconClient("127.0.0.1", 25575, "secret").connect()
        assert result.error_kind is kind

    @pytest.mark.asyncio
    async def test_connect_timeout_is_typed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def hang(_host: str, _port: int) -> tuple[Any, Any]:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        monkeypatch.setattr(rcon_module.asyncio, "open_connection", hang)
        result = await RconClient("127.0.0.1", 25575, "secret", timeout=0.01).connect()
        assert result.error_kind is RconErrorKind.TIMEOUT

    @pytest.mark.asyncio
    async def test_disconnect_is_idempotent_and_contains_close_failure(self) -> None:
        client, writer = _connected_client(b"", writer=_BrokenCloseWriter())
        await client.disconnect()
        await client.disconnect()
        assert writer.close_calls == 1
        assert client.connected is False


class TestRconCommand:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "command, marker",
        [
            ("", "不能为空"),
            ("   ", "不能为空"),
            ("say a\0b", "控制字符"),
            ("x" * 4097, "4096 字节"),
            ("界" * 1400, "4096 字节"),
        ],
    )
    async def test_invalid_input_is_rejected_before_connect(
        self, command: str, marker: str
    ) -> None:
        client = RconClient("127.0.0.1", 25575, "secret")
        result = await client.command(command)
        assert result.error_kind is RconErrorKind.INPUT
        assert marker in result.error_message
        assert client._writer is None  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_empty_response_is_success(self) -> None:
        client, writer = _connected_client(RconPacket(1, PacketType.RESPONSE, "").encode())
        result = await client.command("save-all")
        assert result == RconCommandResult(success=True, response="")
        assert client.connected is True
        assert writer.closed is False

    @pytest.mark.asyncio
    async def test_command_timeout_is_typed_and_disconnects(self) -> None:
        client, writer = _connected_client(
            b"",
            timeout = 0.01,
            reader  = _TimeoutReader(),
        )
        result = await client.command("list")
        assert result.error_kind is RconErrorKind.TIMEOUT
        assert writer.closed is True
        assert client.connected is False

    @pytest.mark.asyncio
    async def test_cancellation_disconnects_and_propagates(self) -> None:
        client, writer = _connected_client(b"", reader=_TimeoutReader())
        task = asyncio.create_task(client.command("list"))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert writer.closed is True
        assert client.connected is False

    @pytest.mark.asyncio
    async def test_protocol_mismatch_discards_partial_response_and_disconnects(self) -> None:
        payload = (
            RconPacket(1, PacketType.RESPONSE, "a" * 4096).encode()
            + RconPacket(99, PacketType.RESPONSE, "partial-tail").encode()
        )
        client, writer = _connected_client(payload)
        result = await client.command("list")
        assert result.response == ""
        assert result.error_kind is RconErrorKind.PROTOCOL
        assert writer.closed is True

    @pytest.mark.asyncio
    async def test_cumulative_response_limit_is_typed_and_disconnects(self) -> None:
        payload = (
            RconPacket(1, PacketType.RESPONSE, "a" * 4096).encode()
            + RconPacket(1, PacketType.RESPONSE, "b" * 512).encode()
        )
        client, writer = _connected_client(payload)
        client.MAX_RESPONSE_BYTES = 4200
        result                    = await client.command("list")
        assert result.error_kind is RconErrorKind.RESPONSE_LIMIT
        assert writer.closed is True

    @pytest.mark.asyncio
    async def test_commands_are_serialized_on_one_connection(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client, _writer = _connected_client(b"")
        active  = 0
        maximum = 0

        async def send(command: str) -> Any:
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0.01)
            active -= 1
            return rcon_module._CollectedResponse(command)  # noqa: SLF001

        monkeypatch.setattr(client, "_send_command", send)
        first, second = await asyncio.gather(client.command("one"), client.command("two"))
        assert {first.response, second.response} == {"one", "two"}
        assert maximum == 1

    @pytest.mark.asyncio
    async def test_raw_command_and_response_never_enter_logs(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        command  = "VERY-SECRET-COMMAND"
        response = "VERY-SECRET-RESPONSE"
        client, _writer = _connected_client(RconPacket(1, PacketType.RESPONSE, response).encode())
        with caplog.at_level(logging.INFO):
            result = await client.command(command)
        assert result.response == response
        assert command not in caplog.text
        assert response not in caplog.text


class TestRconResponseFraming:
    @pytest.mark.asyncio
    async def test_single_large_non_boundary_response_is_returned(self) -> None:
        payload = "x" * 5000
        client, _writer = _connected_client(RconPacket(1, PacketType.RESPONSE, payload).encode())
        response = await client._send_command("list")  # noqa: SLF001
        assert response.payload == payload

    @pytest.mark.asyncio
    async def test_ascii_split_response_is_joined(self) -> None:
        first  = "a" * 4096
        second = "b" * 512
        client, _writer = _connected_client(
            RconPacket(1, PacketType.RESPONSE, first).encode()
            + RconPacket(1, PacketType.RESPONSE, second).encode()
        )
        response = await client._send_command("list")  # noqa: SLF001
        assert response.payload == first + second

    @pytest.mark.asyncio
    async def test_utf8_byte_boundary_split_response_is_joined(self) -> None:
        first  = "é" * 2048  # 4096 UTF-8 字节，但不足 4096 个字符。
        second = "tail"
        client, _writer = _connected_client(
            RconPacket(1, PacketType.RESPONSE, first).encode()
            + RconPacket(1, PacketType.RESPONSE, second).encode()
        )
        response = await client._send_command("list")  # noqa: SLF001
        assert response.payload == first + second

    @pytest.mark.asyncio
    async def test_java_utf16_boundary_split_response_is_joined(self) -> None:
        first  = "🧱" * 2048  # Java 中恰为 4096 个 UTF-16 code units。
        second = "tail"
        client, _writer = _connected_client(
            RconPacket(1, PacketType.RESPONSE, first).encode()
            + RconPacket(1, PacketType.RESPONSE, second).encode()
        )
        response = await client._send_command("list")  # noqa: SLF001
        assert response.payload == first + second

    @pytest.mark.asyncio
    async def test_exact_full_chunk_without_terminator_ends_after_short_timeout(self) -> None:
        first   = "a" * 4096
        encoded = RconPacket(1, PacketType.RESPONSE, first).encode()
        reader  = _PayloadThenTimeoutReader(encoded)
        client, _writer = _connected_client(
            b"",
            timeout = 1.0,
            reader  = reader,
        )
        client.RESPONSE_CHUNK_TIMEOUT = 0.01
        response                      = await client._send_command("list")  # noqa: SLF001
        assert response.payload == first
        assert response.truncated is True

    @pytest.mark.asyncio
    async def test_empty_continuation_packet_terminates_response(self) -> None:
        first = "a" * 4096
        client, _writer = _connected_client(
            RconPacket(1, PacketType.RESPONSE, first).encode()
            + RconPacket(1, PacketType.RESPONSE, "").encode()
        )
        response = await client._send_command("list")  # noqa: SLF001
        assert response.payload == first

    @pytest.mark.asyncio
    async def test_cumulative_deadline_expires_before_next_chunk(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        first = "a" * 4096
        client, _writer = _connected_client(
            RconPacket(1, PacketType.RESPONSE, first).encode(),
            timeout=1.0,
        )

        class Clock:
            values = iter((10.0, 12.0))

            def time(self) -> float:
                return next(self.values)

        clock = Clock()
        monkeypatch.setattr(rcon_module.asyncio, "get_running_loop", lambda: clock)
        with pytest.raises(TimeoutError, match="cumulative"):
            await client._send_command("list")  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_oversized_packet_is_rejected_before_body_read(self) -> None:
        encoded_length = (RconClient.MAX_PACKET_BYTES + 1).to_bytes(4, "little", signed=True)
        client         = RconClient("127.0.0.1", 25575, "secret")
        client._reader = cast(asyncio.StreamReader, _Reader(encoded_length))  # noqa: SLF001
        with pytest.raises(RconProtocolError, match="packet length"):
            await client._read_packet(1.0)  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_truncated_header_body_and_peer_close_are_distinct(self) -> None:
        client         = RconClient("127.0.0.1", 25575, "secret")
        client._reader = cast(asyncio.StreamReader, _Reader(b"\x01"))  # noqa: SLF001
        with pytest.raises(RconProtocolError, match="header was truncated"):
            await client._read_packet(1.0)  # noqa: SLF001

        client._reader = cast(  # noqa: SLF001
            asyncio.StreamReader,
            _Reader(struct.pack("<i", 10) + b"x" * 5),
        )
        with pytest.raises(RconProtocolError, match="body was truncated"):
            await client._read_packet(1.0)  # noqa: SLF001

        client._reader = cast(asyncio.StreamReader, _Reader(b""))  # noqa: SLF001
        with pytest.raises(ConnectionError, match="closed"):
            await client._read_packet(1.0)  # noqa: SLF001


@pytest.mark.asyncio
async def test_real_loopback_tcp_auth_and_split_command_round_trip() -> None:
    requests: list[RconPacket] = []
    handler_done               = asyncio.Event()

    async def read_packet(reader: asyncio.StreamReader) -> RconPacket:
        header = await reader.readexactly(4)
        length = struct.unpack("<i", header)[0]
        body   = await reader.readexactly(length)
        packet, remaining = RconPacket.decode(header + body)
        assert remaining == b""
        return packet

    async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            auth = await read_packet(reader)
            requests.append(auth)
            writer.write(RconPacket(auth.request_id, PacketType.COMMAND, "").encode())
            await writer.drain()

            command = await read_packet(reader)
            requests.append(command)
            writer.write(RconPacket(command.request_id, PacketType.RESPONSE, "a" * 4096).encode())
            writer.write(RconPacket(command.request_id, PacketType.RESPONSE, "tail").encode())
            await writer.drain()
            await reader.read()
        finally:
            writer.close()
            await writer.wait_closed()
            handler_done.set()

    server = await asyncio.start_server(handle_client, "127.0.0.1", 0)
    socket = server.sockets[0]
    port   = int(socket.getsockname()[1])
    client = RconClient("127.0.0.1", port, "loopback-secret", timeout=1.0)
    try:
        connected = await client.connect()
        result    = await client.command("list")
        assert connected.success is True
        assert result == RconCommandResult(True, response="a" * 4096 + "tail")
        assert [(packet.packet_type, packet.payload) for packet in requests] == [
            (PacketType.LOGIN, "loopback-secret"),
            (PacketType.COMMAND, "list"),
        ]
    finally:
        await client.disconnect()
        await asyncio.wait_for(handler_done.wait(), timeout=1.0)
        server.close()
        await server.wait_closed()


def test_internal_response_limit_exception_remains_specific() -> None:
    assert issubclass(RconResponseLimitError, ValueError)


def test_unexpected_exception_uses_internal_error_classification() -> None:
    kind, message = RconClient._classify_error(RuntimeError("boom"))  # noqa: SLF001
    assert kind is RconErrorKind.INTERNAL
    assert "内部错误" in message


@pytest.mark.asyncio
async def test_cancelled_connect_cleans_state_and_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = asyncio.Event()

    async def hang(_host: str, _port: int) -> tuple[Any, Any]:
        entered.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    monkeypatch.setattr(rcon_module.asyncio, "open_connection", hang)
    client = RconClient("127.0.0.1", 25575, "secret")
    task   = asyncio.create_task(client.connect())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert client.connected is False
    assert client._reader is None and client._writer is None  # noqa: SLF001


@pytest.mark.asyncio
async def test_cancelled_disconnect_clears_references_and_propagates() -> None:
    class HangingCloseWriter(_Writer):
        async def wait_closed(self) -> None:
            await asyncio.Event().wait()

    client, writer = _connected_client(b"", writer=HangingCloseWriter())
    task = asyncio.create_task(client.disconnect())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert writer.closed is True
    assert client._reader is None and client._writer is None  # noqa: SLF001


@pytest.mark.asyncio
async def test_command_returns_failed_automatic_reconnect_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = RconClient("127.0.0.1", 25575, "secret")

    async def fail_connect() -> Any:
        return rcon_module.RconConnectResult(
            False,
            error_kind    = RconErrorKind.TRANSPORT,
            error_message = "RCON 连接不可用或已断开",
        )

    monkeypatch.setattr(client, "_connect_locked", fail_connect)
    result = await client.command("list")
    assert result.error_kind is RconErrorKind.TRANSPORT


@pytest.mark.asyncio
async def test_unexpected_command_failure_is_internal_and_disconnects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, writer = _connected_client(b"")

    async def explode(_command: str) -> Any:
        raise RuntimeError("unexpected")

    monkeypatch.setattr(client, "_send_command", explode)
    result = await client.command("list")
    assert result.error_kind is RconErrorKind.INTERNAL
    assert writer.closed is True


@pytest.mark.asyncio
async def test_packet_io_requires_an_open_reader_and_writer() -> None:
    client = RconClient("127.0.0.1", 25575, "secret")
    with pytest.raises(ConnectionError, match="not open"):
        await client._write_packet(RconPacket(1, PacketType.COMMAND, "list"))  # noqa: SLF001
    with pytest.raises(ConnectionError, match="not open"):
        await client._read_packet(1.0)  # noqa: SLF001


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        RconPacket(-1, PacketType.RESPONSE, ""),
        RconPacket(1, PacketType.COMMAND, "unexpected"),
    ],
)
async def test_invalid_auth_response_is_protocol_failure(
    monkeypatch: pytest.MonkeyPatch,
    response: RconPacket,
) -> None:
    reader = _Reader(response.encode())
    writer = _Writer()

    async def open_connection(_host: str, _port: int) -> tuple[Any, Any]:
        return reader, writer

    monkeypatch.setattr(rcon_module.asyncio, "open_connection", open_connection)
    result = await RconClient("127.0.0.1", 25575, "secret").connect()
    assert result.error_kind is RconErrorKind.PROTOCOL
    assert writer.closed is True


@pytest.mark.asyncio
async def test_wrong_command_response_type_is_protocol_failure() -> None:
    client, writer = _connected_client(RconPacket(1, PacketType.COMMAND, "bad").encode())
    result = await client.command("list")
    assert result.error_kind is RconErrorKind.PROTOCOL
    assert writer.closed is True


@pytest.mark.asyncio
async def test_first_response_chunk_can_exceed_cumulative_limit() -> None:
    client, writer = _connected_client(RconPacket(1, PacketType.RESPONSE, "x" * 100).encode())
    client.MAX_RESPONSE_BYTES = 99
    result                    = await client.command("list")
    assert result.error_kind is RconErrorKind.RESPONSE_LIMIT
    assert writer.closed is True
