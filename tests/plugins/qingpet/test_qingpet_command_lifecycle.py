"""QingPet 真实命令入口的同一宠物生命周期测试。"""

from __future__ import annotations

import pytest

from plugins.qingpet import main as qingpet_main
from plugins.qingpet.services.admin_service import AdminService
from plugins.qingpet.utils.constants import PetStatus
from tests.helpers.assertions import text_segments_text


@pytest.mark.asyncio
async def test_real_command_entry_reuses_one_pet_and_invalid_steps_do_not_mutate(
    monkeypatch: pytest.MonkeyPatch,
    qingpet_db,
) -> None:
    """领养、查看、改名、睡醒和删除必须作用于同一只真实宠物。"""

    group_id = 880_721_001
    user_id  = "880721002"
    event    = {
        "message_type": "group",
        "group_id": group_id,
        "user_id": int(user_id),
        "message_id": 1,
    }
    assert AdminService(qingpet_db).enable_plugin(group_id) is True

    # 隔离限流噪声，只验证生产路由、处理器和数据库之间的业务契约。
    monkeypatch.setattr(qingpet_main, "_db_instance", qingpet_db)
    monkeypatch.setattr(qingpet_main, "_router", None)
    monkeypatch.setattr(qingpet_main, "_is_group_rate_limited", lambda _group_id: False)
    monkeypatch.setattr(
        qingpet_main,
        "_get_anti_spam_state",
        lambda _user_id, _group_id: (None, 1.0),
    )
    monkeypatch.setattr(qingpet_main, "_record_command", lambda _user_id, _group_id: None)

    async def call(args: str) -> str:
        result = await qingpet_main.handle("qingpet", args, event, None)
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

    assert AdminService(qingpet_db).delete_user_pet(
        user_id,
        group_id,
        operator_user_id="lifecycle-test",
    )
    assert qingpet_db.get_pet(user_id, group_id) is None
    assert "还没有宠物" in await call("status")
