from .admin_commands import (
    handle_manage_activity,
    handle_manage_ban,
    handle_manage_config,
    handle_manage_disable,
    handle_manage_enable,
    handle_manage_log,
    handle_manage_reset,
    handle_manage_stats,
    handle_manage_unban,
)
from .advanced_commands import (
    handle_activity,
    handle_backpack,
    handle_buy,
    handle_explore,
    handle_gift,
    handle_group_task,
    handle_like,
    handle_message,
    handle_minigame,
    handle_ranking,
    handle_rename,
    handle_shop,
    handle_task,
    handle_title,
    handle_train,
    handle_treat,
    handle_use,
    handle_view_pet,
    handle_visit,
)
from .basic_commands import (
    handle_adopt,
    handle_clean,
    handle_feed,
    handle_play,
    handle_sleep,
    handle_status,
    handle_wake,
)
from .new_commands import (
    handle_dress,
    handle_manage_announce,
    handle_manage_delete,
    handle_recall,
    handle_show,
    handle_trade,
)

__all__ = [
    # basic
    "handle_adopt", "handle_status", "handle_feed", "handle_clean",
    "handle_play", "handle_sleep", "handle_wake",
    # advanced
    "handle_train", "handle_explore", "handle_treat",
    "handle_backpack", "handle_shop", "handle_buy", "handle_use",
    "handle_gift", "handle_visit", "handle_ranking",
    "handle_activity", "handle_task", "handle_group_task", "handle_rename",
    "handle_view_pet", "handle_like", "handle_message",
    "handle_title", "handle_minigame",
    # new features
    "handle_recall", "handle_dress", "handle_trade", "handle_show",
    # admin
    "handle_manage_enable", "handle_manage_disable", "handle_manage_config",
    "handle_manage_reset", "handle_manage_ban", "handle_manage_unban",
    "handle_manage_log", "handle_manage_stats", "handle_manage_activity",
    "handle_manage_delete", "handle_manage_announce",
]
