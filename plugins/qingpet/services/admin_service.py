"""管理员可修改的群配置校验与操作审计日志。"""

from ..models import OperationLog
from .database import Database

_MULTIPLIER_KEYS = frozenset({"economy_multiplier", "decay_multiplier"})
_BOOLEAN_KEYS    = frozenset({"trade_enabled", "natural_trigger_enabled", "activity_enabled"})
_TRUE_VALUES     = frozenset({"true", "1", "yes"})
_FALSE_VALUES    = frozenset({"false", "0", "no"})


### 校验并原子更新一个允许公开配置的群级字段；倍率限定 0.1~10，开关只认 true/false/1/0/yes/no
def set_group_config(db: Database, group_id: int, key: str, value: str) -> bool:

    new_value: float | bool
    if key in _MULTIPLIER_KEYS:
        try:
            new_value = float(value)
        except ValueError:
            return False
        if not 0.1 <= new_value <= 10.0:
            return False
    elif key in _BOOLEAN_KEYS:
        normalized_value = value.strip().casefold()
        if normalized_value not in _TRUE_VALUES | _FALSE_VALUES:
            return False
        new_value = normalized_value in _TRUE_VALUES
    else:
        return False
    # 只改目标字段：整份旧快照写回会覆盖并发请求刚提交的其他配置。
    return db.update_group_config_field(group_id, key, new_value)


### 记录一条非事务型管理操作日志
def log_admin_operation(
    db: Database,
    group_id: int,
    user_id: str,
    operation_type: str,
    params: str                = "",
    target_user_id: str | None = None,
) -> bool:

    return db.log_operation(
        OperationLog(
            id             = 0,
            group_id       = group_id,
            user_id        = user_id,
            target_user_id = target_user_id,
            operation_type = operation_type,
            params         = params,
            result         = "success",
        )
    )
