"""群级功能开关、倍率和敏感词配置模型。"""

from dataclasses import dataclass, field


class GroupConfigReadError(RuntimeError):
    """已有群配置无法安全读取时抛出的稳定领域异常。"""

    def __init__(self, group_id: int) -> None:
        self.group_id = group_id
        super().__init__(f"群 {group_id} 的宠物配置不可用")


@dataclass
class GroupConfig:
    """控制单个群的玩法开关、经济倍率和内容过滤扩展。"""

    group_id: int
    enabled: bool = True

    economy_multiplier: float = 1.0
    decay_multiplier: float = 1.0

    trade_enabled: bool = False
    natural_trigger_enabled: bool = False

    activity_enabled: bool = True

    # 群级追加词；内置词始终生效，空列表表示仅继承内置词。
    sensitive_words: list[str] = field(default_factory=list)
