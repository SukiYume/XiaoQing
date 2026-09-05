"""Qingpet 数据库事务的稳定输入与结果值对象。"""

from dataclasses import dataclass
from typing import Any

from ..models import Pet


@dataclass(frozen=True)
class VisitPetAtomicResult:
    """一次可幂等重放的宠物互访结算结果。"""

    success: bool
    reason: str         = ""
    pet_name: str       = ""
    visitor_grant: int  = 0
    target_grant: int   = 0
    intimacy_grant: int = 0
    duplicate: bool     = False


@dataclass(frozen=True)
class PetShowWinner:
    user_id: str
    pet_name: str
    vote_count: int
    coins_granted: int


@dataclass(frozen=True)
class PetShowSettlementResult:
    show_id: int
    title: str
    winners: tuple[PetShowWinner, ...] = ()


@dataclass(frozen=True)
class MinigameOutcome:
    """小游戏随机结果请求的资产变化及可重放展示数据。"""

    requested_coins: int           = 0
    experience: int                = 0
    energy_cost: int               = 0
    payload: dict[str, Any] | None = None


@dataclass(frozen=True)
class MinigameAtomicResult:
    """一次已提交且可幂等重放的小游戏结算结果。"""

    success: bool
    reason: str                    = ""
    pet_name: str                  = ""
    opponent_pet_name: str         = ""
    coin_grant: int                = 0
    experience_grant: int          = 0
    energy_cost: int               = 0
    payload: dict[str, Any] | None = None
    duplicate: bool                = False


@dataclass(frozen=True)
class LeaveMessageAtomicResult:
    """一次经过配额校验的留言提交结果。"""

    success: bool
    reason: str   = ""
    pet_name: str = ""


@dataclass(frozen=True)
class PetActionAtomicResult:
    """一次受配额约束的宠物动作提交结果。"""

    success: bool
    reason: str        = ""
    remaining: int     = 0
    coins_granted: int = 0


@dataclass(frozen=True)
class TreatPetAtomicResult:
    """一次治疗事务的结果及成功写入后的宠物快照。"""

    success: bool
    reason: str     = ""
    remaining: int  = 0
    pet: Pet | None = None


@dataclass(frozen=True)
class DailyResetResult:
    users_reset: int
    pets_aged: int


@dataclass(frozen=True)
class WeeklyRankingWinner:
    user_id: str
    pet_name: str
    score: float
    coins_granted: int
    title_granted: bool = False


@dataclass(frozen=True)
class WeeklyActivitySettlementResult:
    winners: tuple[WeeklyRankingWinner, ...] = ()


@dataclass(frozen=True)
class GroupEconomySnapshot:
    """单次查询得到的群经济快照；余额以 ``users.coins`` 为准。"""

    total_pets: int           = 0
    total_coins: int          = 0
    total_experience: int     = 0
    total_intimacy: int       = 0
    average_care_score: float = 0.0
    active_today: int         = 0


@dataclass(frozen=True)
class CoinLedgerReconciliation:
    """基于检查点比较权威余额与资产账本增量的结果。"""

    status: str
    current_balance: int
    expected_balance: int
    difference: int
    consistent: bool
