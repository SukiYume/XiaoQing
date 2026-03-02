"""
Minecraft RCON 客户端实现

实现 Source RCON 协议，用于与 Minecraft 服务器通信。
协议文档: https://developer.valvesoftware.com/wiki/Source_RCON_Protocol
"""

import asyncio
import struct
import logging
from typing import Optional
from dataclasses import dataclass
from enum import IntEnum

logger = logging.getLogger(__name__)

class PacketType(IntEnum):
    """RCON 数据包类型"""
    RESPONSE = 0
    COMMAND = 2
    LOGIN = 3

@dataclass
class RconPacket:
    """RCON 数据包"""
    request_id: int
    packet_type: int
    payload: str

    def encode(self) -> bytes:
        """编码数据包为字节流"""
        payload_bytes = self.payload.encode("utf-8") + b"\x00\x00"
        length = 4 + 4 + len(payload_bytes)  # id + type + payload
        return struct.pack("<iii", length, self.request_id, self.packet_type) + payload_bytes

    @classmethod
    def decode(cls, data: bytes) -> tuple["RconPacket", bytes]:
        """从字节流解码数据包，返回 (packet, remaining_data)"""
        if len(data) < 4:
            raise ValueError("数据不完整")
        
        length = struct.unpack("<i", data[:4])[0]
        if len(data) < 4 + length:
            raise ValueError("数据不完整")
        
        request_id, packet_type = struct.unpack("<ii", data[4:12])
        payload = data[12:4 + length - 2].decode("utf-8", errors="replace")
        remaining = data[4 + length:]
        
        return cls(request_id, packet_type, payload), remaining

class RconClient:
    """
    异步 RCON 客户端
    
    用法:
        async with RconClient("127.0.0.1", 25575, "password") as client:
            response = await client.command("list")
    """
    
    def __init__(self, host: str, port: int, password: str, timeout: float = 10.0):
        self.host = host
        self.port = port
        self.password = password
        self.timeout = timeout
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._request_id = 0
        self._lock = asyncio.Lock()
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected and self._writer is not None and not self._writer.is_closing()

    async def connect(self) -> bool:
        """连接到 RCON 服务器并认证"""
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port),
                timeout=self.timeout
            )
            
            # 发送认证请求
            response = await self._send_packet(PacketType.LOGIN, self.password)
            
            if response.request_id == -1:
                logger.error("RCON 认证失败: 密码错误")
                await self.disconnect()
                return False
            
            self._connected = True
            logger.info("RCON 连接成功: %s:%d", self.host, self.port)
            return True
            
        except asyncio.TimeoutError:
            logger.error("RCON 连接超时: %s:%d", self.host, self.port)
            return False
        except ConnectionRefusedError:
            logger.error("RCON 连接被拒绝: %s:%d", self.host, self.port)
            return False
        except Exception as e:
            logger.error("RCON 连接失败: %s", e)
            return False

    async def disconnect(self) -> None:
        """断开连接"""
        self._connected = False
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
            self._writer = None
            self._reader = None
        logger.info("RCON 已断开连接")

    async def command(self, cmd: str) -> Optional[str]:
        """
        发送命令并获取响应
        
        Args:
            cmd: 要执行的命令（不带 /）
            
        Returns:
            命令响应文本，失败返回 None
        """
        if not self.connected:
            logger.warning("RCON 未连接，尝试重新连接...")
            if not await self.connect():
                return None
        
        try:
            response = await self._send_packet(PacketType.COMMAND, cmd)
            return response.payload
        except Exception as e:
            logger.error("RCON 命令执行失败: %s", e)
            self._connected = False
            return None

    async def _send_packet(self, packet_type: PacketType, payload: str) -> RconPacket:
        """发送数据包并等待响应"""
        async with self._lock:
            self._request_id += 1
            request_id = self._request_id
            
            packet = RconPacket(request_id, packet_type, payload)
            
            if not self._writer or not self._reader:
                raise ConnectionError("未连接到服务器")
            
            self._writer.write(packet.encode())
            await self._writer.drain()
            
            # 读取响应
            header = await asyncio.wait_for(
                self._reader.readexactly(4),
                timeout=self.timeout,
            )
            packet_length = struct.unpack("<i", header)[0]
            body = await asyncio.wait_for(
                self._reader.readexactly(packet_length),
                timeout=self.timeout,
            )
            data = header + body
            
            if not data:
                raise ConnectionError("服务器关闭连接")
            
            response, _ = RconPacket.decode(data)
            return response

    async def __aenter__(self) -> "RconClient":
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.disconnect()
