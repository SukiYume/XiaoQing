from dataclasses import dataclass, field
from typing import Dict, Optional, List


@dataclass
class GroupConfig:
    group_id: int
    enabled: bool = True
    
    economy_multiplier: float = 1.0
    decay_multiplier: float = 1.0
    
    trade_enabled: bool = False
    natural_trigger_enabled: bool = False
    
    activity_enabled: bool = True
    
    # 群级敏感词过滤列表
    sensitive_words: List[str] = field(default_factory=list)
    
    @classmethod
    def default(cls, group_id: int) -> "GroupConfig":
        return cls(group_id=group_id)


@dataclass
class PluginConfig:
    version: str = "1.0.0"
    
    global_activity_schedule: Dict[str, bool] = field(default_factory=dict)
    
    anti_spam_threshold: int = 10
    anti_spam_window: int = 60
    
    sensitive_words: list = field(default_factory=list)
    
    @classmethod
    def default(cls) -> "PluginConfig":
        return cls()