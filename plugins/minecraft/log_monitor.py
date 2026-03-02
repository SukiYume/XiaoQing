"""
Minecraft 服务器日志监控

监控服务器日志文件，提取玩家聊天消息和事件。
"""

import re
import logging
from pathlib import Path
from dataclasses import dataclass
from typing import Optional, Callable, Awaitable
from enum import Enum

logger = logging.getLogger(__name__)

class LogEventType(Enum):
    """日志事件类型"""
    CHAT = "chat"           # 玩家聊天
    JOIN = "join"           # 玩家加入
    LEAVE = "leave"         # 玩家离开
    DEATH = "death"         # 玩家死亡
    ADVANCEMENT = "advancement"  # 获得成就
    UNKNOWN = "unknown"

@dataclass
class LogEvent:
    """日志事件"""
    event_type: LogEventType
    player: Optional[str]
    message: Optional[str]
    raw_line: str
    timestamp: Optional[str] = None

class LogMonitor:
    """
    日志文件监控器
    
    追踪日志文件的变化，解析新增的日志行。
    """
    
    # 日志行模式
    # 格式: [HH:MM:SS] [Thread/INFO]: <Player> Message
    # Paper 服务器可能使用异步线程: [Async Chat Thread - #N/INFO]
    CHAT_PATTERN = re.compile(
        r"\[[\d:]+\] \[[^\]]+/INFO\]: <(\w+)> (.+)"
    )
    
    # 玩家加入: Player joined the game
    JOIN_PATTERN = re.compile(
        r"\[[\d:]+\] \[[^\]]+/INFO\]: (\w+) joined the game"
    )
    
    # 玩家离开: Player left the game
    LEAVE_PATTERN = re.compile(
        r"\[[\d:]+\] \[[^\]]+/INFO\]: (\w+) left the game"
    )
    
    # 玩家死亡 (各种死亡消息)
    DEATH_PATTERNS = [
        re.compile(r"\[[\d:]+\] \[[^\]]+/INFO\]: (\w+) (was slain|was shot|drowned|burned|fell|hit the ground|was blown up|was killed|died|withered away|was squashed|was pricked|walked into a cactus|suffocated|starved|was impaled|was fireballed|was pummeled|was stung|froze|was skewered|was obliterated)"),
    ]
    
    # 获得成就
    ADVANCEMENT_PATTERN = re.compile(
        r"\[[\d:]+\] \[[^\]]+/INFO\]: (\w+) has (made the advancement|completed the challenge|reached the goal) \[(.+)\]"
    )
    
    # 时间戳提取
    TIMESTAMP_PATTERN = re.compile(r"\[([\d:]+)\]")

    def __init__(self, log_path: str):
        self.log_path = Path(log_path)
        self._last_position = 0
        self._last_size = 0
        self._initialized = False

    def initialize(self) -> bool:
        """初始化监控器，定位到文件末尾"""
        if not self.log_path.exists():
            logger.warning("日志文件不存在: %s", self.log_path)
            return False
        
        try:
            self._last_size = self.log_path.stat().st_size
            self._last_position = self._last_size  # 从文件末尾开始
            self._initialized = True
            logger.info("日志监控已初始化: %s (位置: %d)", self.log_path, self._last_position)
            return True
        except Exception as e:
            logger.error("初始化日志监控失败: %s", e)
            return False

    def check_updates(self) -> list[LogEvent]:
        """
        检查日志文件更新
        
        Returns:
            新日志事件列表
        """
        if not self._initialized:
            if not self.initialize():
                return []
        
        if not self.log_path.exists():
            logger.warning("日志文件不存在")
            return []
        
        events = []
        
        try:
            current_size = self.log_path.stat().st_size
            
            # 文件可能被轮换（新的 latest.log）
            if current_size < self._last_position:
                logger.info("检测到日志文件轮换，重新开始监控")
                self._last_position = 0
            
            if current_size == self._last_position:
                return []  # 没有新内容
            
            # 读取新内容
            with open(self.log_path, "r", encoding="utf-8", errors="replace") as f:
                f.seek(self._last_position)
                new_content = f.read()
                self._last_position = f.tell()
            
            # 解析每一行
            for line in new_content.strip().split("\n"):
                if line.strip():
                    event = self._parse_line(line)
                    if event and event.event_type != LogEventType.UNKNOWN:
                        events.append(event)
            
            self._last_size = current_size
            
        except Exception as e:
            logger.error("读取日志文件失败: %s", e)
        
        return events

    def _parse_line(self, line: str) -> Optional[LogEvent]:
        """解析单行日志"""
        # 提取时间戳
        timestamp = None
        ts_match = self.TIMESTAMP_PATTERN.search(line)
        if ts_match:
            timestamp = ts_match.group(1)
        
        # 尝试匹配聊天消息
        match = self.CHAT_PATTERN.search(line)
        if match:
            return LogEvent(
                event_type=LogEventType.CHAT,
                player=match.group(1),
                message=match.group(2),
                raw_line=line,
                timestamp=timestamp
            )
        
        # 尝试匹配玩家加入
        match = self.JOIN_PATTERN.search(line)
        if match:
            return LogEvent(
                event_type=LogEventType.JOIN,
                player=match.group(1),
                message=None,
                raw_line=line,
                timestamp=timestamp
            )
        
        # 尝试匹配玩家离开
        match = self.LEAVE_PATTERN.search(line)
        if match:
            return LogEvent(
                event_type=LogEventType.LEAVE,
                player=match.group(1),
                message=None,
                raw_line=line,
                timestamp=timestamp
            )
        
        # 尝试匹配死亡消息
        for pattern in self.DEATH_PATTERNS:
            match = pattern.search(line)
            if match:
                return LogEvent(
                    event_type=LogEventType.DEATH,
                    player=match.group(1),
                    message=match.group(2),
                    raw_line=line,
                    timestamp=timestamp
                )
        
        # 尝试匹配成就
        match = self.ADVANCEMENT_PATTERN.search(line)
        if match:
            return LogEvent(
                event_type=LogEventType.ADVANCEMENT,
                player=match.group(1),
                message=match.group(3),
                raw_line=line,
                timestamp=timestamp
            )
        
        return None

    def reset(self) -> None:
        """重置监控位置到文件末尾"""
        self._initialized = False
        self.initialize()
