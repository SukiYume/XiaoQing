"""QingPet 真实命令入口的同一宠物生命周期测试。"""

from __future__ import annotations

import pytest

from plugins.qingpet import main as qingpet_main
from plugins.qingpet.services import database_community
from plugins.qingpet.utils.constants import PetStatus
from tests.helpers.assertions import text_segments_text


class _Clock:
    """限流按 ``time.time`` 计窗口；每条命令前拨快，让连续命令不触发群级限流。"""

    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def time(self) -> float:
        return self.now


@pytest.mark.asyncio
async def test_real_command_entry_reuses_one_pet_and_invalid_steps_do_not_mutate(
    monkeypatch: pytest.MonkeyPatch,
    qingpet_runtime,
) -> None:
    """领养、查看、改名、睡醒和删除必须作用于同一只真实宠物。"""

    context, qingpet_db = qingpet_runtime
    group_id = 880_721_001
    user_id  = "880721002"
    event    = {
        "message_type": "group",
        "group_id"    : group_id,
        "user_id"     : int(user_id),
        "message_id"  : 1,
    }
    assert qingpet_db.update_group_config_field(group_id, "enabled", True) is True

    clock = _Clock()
    monkeypatch.setattr(database_community, "time", clock)

    async def call(args: str) -> str:
        clock.now += 11  # 超过群级限流窗口
        result = await qingpet_main.handle("qingpet", args, event, context)
        return text_segments_text(result, separator="\n")

    assert "还没有宠物" in await call("status")
    assert "请提供宠物名字" in await call("adopt")
    assert qingpet_db.get_pet(user_id, group_id) is None

    assert "小青团" in await call("adopt 小青团")
    pet = qingpet_db.get_pet(user_id, group_id)
    assert pet is not None
    original_pet_id = pet.id

    duplicate_text = await call("adopt 第二只")
    assert "已经" in duplicate_text or "只能" in duplicate_text
    pet = qingpet_db.get_pet(user_id, group_id)
    assert pet is not None
    assert pet.id == original_pet_id
    assert pet.name == "小青团"
    assert "小青团" in await call("status")

    assert "请提供新名字" in await call("rename")
    unchanged = qingpet_db.get_pet(user_id, group_id)
    assert unchanged is not None and unchanged.name == "小青团"

    assert "小蓝团" in await call("rename 小蓝团")
    renamed = qingpet_db.get_pet(user_id, group_id)
    assert renamed is not None
    assert renamed.id == original_pet_id
    assert renamed.name == "小蓝团"

    assert "睡" in await call("sleep")
    sleeping = qingpet_db.get_pet(user_id, group_id)
    assert sleeping is not None and sleeping.status == PetStatus.SLEEPING
    second_sleep = await call("sleep")
    assert "已经" in second_sleep or "正在" in second_sleep
    still_sleeping = qingpet_db.get_pet(user_id, group_id)
    assert still_sleeping is not None and still_sleeping.status == PetStatus.SLEEPING

    assert "睡醒" in await call("wake")
    awake = qingpet_db.get_pet(user_id, group_id)
    assert awake is not None and awake.status != PetStatus.SLEEPING
    second_wake = await call("wake")
    assert "没有" in second_wake or "已经" in second_wake
    still_awake = qingpet_db.get_pet(user_id, group_id)
    assert still_awake is not None and still_awake.status != PetStatus.SLEEPING

    assert qingpet_db.admin_delete_pet_atomic(user_id, group_id, "lifecycle-test")
    assert qingpet_db.get_pet(user_id, group_id) is None
    assert "还没有宠物" in await call("status")
