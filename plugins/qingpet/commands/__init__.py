from .basic_commands import (
    handle_adopt, handle_status, handle_feed, handle_clean,
    handle_play, handle_sleep, handle_wake
)

from .advanced_commands import (
    handle_train, handle_explore, handle_treat,
    handle_backpack, handle_shop, handle_buy, handle_use,
    handle_gift, handle_visit, handle_ranking,
    handle_activity, handle_task, handle_rename,
    handle_view_pet, handle_like, handle_message,
    handle_title, handle_minigame
)

from .admin_commands import (
    handle_manage_enable, handle_manage_disable, handle_manage_config,
    handle_manage_reset, handle_manage_ban, handle_manage_unban,
    handle_manage_log, handle_manage_stats
)

from .new_commands import (
    handle_recall, handle_dress, handle_trade, handle_show,
    handle_manage_delete, handle_manage_export, handle_manage_announce,
)

__all__ = [
    # basic
    "handle_adopt", "handle_status", "handle_feed", "handle_clean",
    "handle_play", "handle_sleep", "handle_wake",
    # advanced
    "handle_train", "handle_explore", "handle_treat",
    "handle_backpack", "handle_shop", "handle_buy", "handle_use",
    "handle_gift", "handle_visit", "handle_ranking",
    "handle_activity", "handle_task", "handle_rename",
    "handle_view_pet", "handle_like", "handle_message",
    "handle_title", "handle_minigame",
    # new features
    "handle_recall", "handle_dress", "handle_trade", "handle_show",
    # admin
    "handle_manage_enable", "handle_manage_disable", "handle_manage_config",
    "handle_manage_reset", "handle_manage_ban", "handle_manage_unban",
    "handle_manage_log", "handle_manage_stats",
    "handle_manage_delete", "handle_manage_export", "handle_manage_announce",
]