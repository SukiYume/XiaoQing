"""Qingpet 的 SQLite schema、兼容迁移、索引和数据库约束。"""

import logging
import re
import sqlite3

from ..utils.time import utc_now

logger = logging.getLogger(__name__)

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_ALLOWED_SCHEMA_COLUMNS: dict[str, dict[str, str]] = {
    "users": {
        "total_feed_count": "INTEGER DEFAULT 0",
        "total_clean_count": "INTEGER DEFAULT 0",
        "total_play_count": "INTEGER DEFAULT 0",
        "total_train_count": "INTEGER DEFAULT 0",
        "total_explore_count": "INTEGER DEFAULT 0",
        "total_visit_count": "INTEGER DEFAULT 0",
        "total_gift_count": "INTEGER DEFAULT 0",
        "titles": "TEXT DEFAULT '[]'",
        "today_free_feed_count": "INTEGER DEFAULT 0",
        "today_message_count": "INTEGER DEFAULT 0",
        "total_free_feed_count": "INTEGER DEFAULT 0",
        "total_message_count": "INTEGER DEFAULT 0",
        "version": "INTEGER NOT NULL DEFAULT 0",
    },
    "pets": {
        "decay_remainders": "TEXT NOT NULL DEFAULT '{}'",
        "likes": "INTEGER DEFAULT 0",
        "dress_hat": "TEXT",
        "dress_clothes": "TEXT",
        "dress_accessory": "TEXT",
        "dress_background": "TEXT",
        "version": "INTEGER NOT NULL DEFAULT 0",
    },
    "group_configs": {
        "sensitive_words": "TEXT DEFAULT '[]'",
    },
    "tasks": {
        "created_date": "TEXT",
    },
    "trade_listings": {
        "status": "TEXT DEFAULT 'active'",
    },
    "pet_shows": {
        "status": "TEXT DEFAULT 'active'",
        "settled_at": "TEXT",
    },
    "inventories": {
        "version": "INTEGER NOT NULL DEFAULT 0",
    },
    "scheduler_runs": {
        "status": "TEXT NOT NULL DEFAULT 'completed'",
        "lease_until": "TEXT",
        "attempt_count": "INTEGER NOT NULL DEFAULT 1",
        "completed_at": "TEXT",
    },
}

_CURRENT_SCHEMA_VERSION   = 6
_LEGACY_COLUMN_MIGRATIONS = (
    ("pets", "decay_remainders", "TEXT NOT NULL DEFAULT '{}'"),
    ("users", "total_feed_count", "INTEGER DEFAULT 0"),
    ("users", "total_clean_count", "INTEGER DEFAULT 0"),
    ("users", "total_play_count", "INTEGER DEFAULT 0"),
    ("users", "total_train_count", "INTEGER DEFAULT 0"),
    ("users", "total_explore_count", "INTEGER DEFAULT 0"),
    ("users", "total_visit_count", "INTEGER DEFAULT 0"),
    ("users", "total_gift_count", "INTEGER DEFAULT 0"),
    ("users", "titles", "TEXT DEFAULT '[]'"),
    ("pets", "likes", "INTEGER DEFAULT 0"),
    ("pets", "dress_hat", "TEXT"),
    ("pets", "dress_clothes", "TEXT"),
    ("pets", "dress_accessory", "TEXT"),
    ("pets", "dress_background", "TEXT"),
    ("group_configs", "sensitive_words", "TEXT DEFAULT '[]'"),
    ("tasks", "created_date", "TEXT"),
    ("users", "today_free_feed_count", "INTEGER DEFAULT 0"),
    ("users", "today_message_count", "INTEGER DEFAULT 0"),
    ("users", "total_free_feed_count", "INTEGER DEFAULT 0"),
    ("users", "total_message_count", "INTEGER DEFAULT 0"),
    ("users", "version", "INTEGER NOT NULL DEFAULT 0"),
    ("pets", "version", "INTEGER NOT NULL DEFAULT 0"),
    ("inventories", "version", "INTEGER NOT NULL DEFAULT 0"),
    ("trade_listings", "status", "TEXT DEFAULT 'active'"),
    ("pet_shows", "status", "TEXT DEFAULT 'active'"),
    ("pet_shows", "settled_at", "TEXT"),
    ("scheduler_runs", "status", "TEXT NOT NULL DEFAULT 'completed'"),
    ("scheduler_runs", "lease_until", "TEXT"),
    ("scheduler_runs", "attempt_count", "INTEGER NOT NULL DEFAULT 1"),
    ("scheduler_runs", "completed_at", "TEXT"),
)


def _create_schema_migration_table(cursor: sqlite3.Cursor) -> None:
    cursor.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
        version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL
    )""")


def _create_identity_schema(cursor: sqlite3.Cursor) -> None:
    """创建用户、宠物、背包和群配置等权威数据表。"""

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id TEXT NOT NULL, group_id INTEGER NOT NULL,
            coins INTEGER DEFAULT 100, friendship_points INTEGER DEFAULT 0,
            today_coins_earned INTEGER DEFAULT 0,
            today_feed_count INTEGER DEFAULT 0, today_clean_count INTEGER DEFAULT 0,
            today_play_count INTEGER DEFAULT 0, today_train_count INTEGER DEFAULT 0,
            today_explore_count INTEGER DEFAULT 0,
            today_visit_count INTEGER DEFAULT 0, today_gift_count INTEGER DEFAULT 0,
            total_feed_count INTEGER DEFAULT 0, total_clean_count INTEGER DEFAULT 0,
            total_play_count INTEGER DEFAULT 0, total_train_count INTEGER DEFAULT 0,
            total_explore_count INTEGER DEFAULT 0,
            total_visit_count INTEGER DEFAULT 0, total_gift_count INTEGER DEFAULT 0,
            last_visit_time TEXT, last_gift_time TEXT,
            trustee_until TEXT, is_banned BOOLEAN DEFAULT 0, ban_until TEXT,
            titles TEXT DEFAULT '[]', created_at TEXT, last_active TEXT,
            version INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (user_id, group_id)
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS pets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL, group_id INTEGER NOT NULL,
            name TEXT NOT NULL, stage TEXT NOT NULL, form TEXT DEFAULT '普通',
            hunger INTEGER DEFAULT 100, mood INTEGER DEFAULT 100,
            clean INTEGER DEFAULT 100, energy INTEGER DEFAULT 100,
            health INTEGER DEFAULT 100, age INTEGER DEFAULT 0,
            experience INTEGER DEFAULT 0, intimacy INTEGER DEFAULT 0,
            personality TEXT DEFAULT '活泼', favorite_food TEXT,
            status TEXT DEFAULT '正常', status_expire_time TEXT,
            dress_hat TEXT, dress_clothes TEXT, dress_accessory TEXT,
            dress_background TEXT,
            last_update TEXT, last_feed TEXT, last_clean TEXT,
            last_play TEXT, last_train TEXT, last_explore TEXT,
            likes INTEGER DEFAULT 0, created_at TEXT,
            version INTEGER NOT NULL DEFAULT 0,
            UNIQUE (user_id, group_id)
        )
    """)
    cursor.execute("""CREATE TABLE IF NOT EXISTS inventories (
        user_id TEXT NOT NULL, group_id INTEGER NOT NULL,
        items TEXT, version INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (user_id, group_id))""")
    cursor.execute("""CREATE TABLE IF NOT EXISTS group_configs (
        group_id INTEGER PRIMARY KEY, enabled BOOLEAN DEFAULT 1,
        economy_multiplier REAL DEFAULT 1.0, decay_multiplier REAL DEFAULT 1.0,
        trade_enabled BOOLEAN DEFAULT 0, natural_trigger_enabled BOOLEAN DEFAULT 0,
        activity_enabled BOOLEAN DEFAULT 1, sensitive_words TEXT DEFAULT '[]')""")


def _create_interaction_schema(cursor: sqlite3.Cursor) -> None:
    """创建活动、任务、留言、展示会、配额和调度状态表。"""

    cursor.execute("""CREATE TABLE IF NOT EXISTS operation_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, group_id INTEGER NOT NULL,
        user_id TEXT NOT NULL, target_user_id TEXT,
        operation_type TEXT NOT NULL, params TEXT,
        result TEXT DEFAULT 'success', created_at TEXT)""")
    cursor.execute("""CREATE TABLE IF NOT EXISTS activities (
        id INTEGER PRIMARY KEY AUTOINCREMENT, group_id INTEGER NOT NULL,
        activity_type TEXT NOT NULL, title TEXT DEFAULT '',
        description TEXT DEFAULT '', target_value INTEGER NOT NULL,
        current_value INTEGER DEFAULT 0, reward_coins INTEGER DEFAULT 0,
        reward_items TEXT DEFAULT '{}', start_time TEXT, end_time TEXT,
        is_active BOOLEAN DEFAULT 0)""")
    cursor.execute("""CREATE TABLE IF NOT EXISTS tasks (
        user_id TEXT NOT NULL, group_id INTEGER NOT NULL,
        task_type TEXT NOT NULL, target_value INTEGER NOT NULL,
        current_value INTEGER DEFAULT 0, reward_coins INTEGER DEFAULT 0,
        claimed BOOLEAN DEFAULT 0, created_date TEXT,
        created_at TEXT, PRIMARY KEY (user_id, group_id, task_type, created_date))""")
    cursor.execute("""CREATE TABLE IF NOT EXISTS message_board (
        id INTEGER PRIMARY KEY AUTOINCREMENT, group_id INTEGER NOT NULL,
        from_user_id TEXT NOT NULL, to_user_id TEXT NOT NULL,
        message TEXT NOT NULL, created_at TEXT)""")
    cursor.execute("""CREATE TABLE IF NOT EXISTS pet_shows (
        id INTEGER PRIMARY KEY AUTOINCREMENT, group_id INTEGER NOT NULL,
        title TEXT DEFAULT '宠物展示会',
        start_time TEXT, end_time TEXT, is_active BOOLEAN DEFAULT 0)""")
    cursor.execute("""CREATE TABLE IF NOT EXISTS pet_show_votes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        show_id INTEGER NOT NULL, voter_user_id TEXT NOT NULL,
        pet_user_id TEXT NOT NULL, created_at TEXT)""")
    cursor.execute("""CREATE TABLE IF NOT EXISTS command_timestamps (
        user_id TEXT NOT NULL, group_id INTEGER NOT NULL, timestamp REAL NOT NULL)""")
    cursor.execute("""CREATE TABLE IF NOT EXISTS minigame_cooldowns (
        user_id TEXT NOT NULL, group_id INTEGER NOT NULL, game_type TEXT NOT NULL,
        available_at REAL NOT NULL,
        PRIMARY KEY (user_id, group_id, game_type)
    )""")
    cursor.execute("""CREATE TABLE IF NOT EXISTS action_quotas (
        user_id TEXT NOT NULL, group_id INTEGER NOT NULL, action TEXT NOT NULL,
        period_date TEXT NOT NULL, action_count INTEGER NOT NULL DEFAULT 0,
        available_at REAL NOT NULL DEFAULT 0,
        PRIMARY KEY (user_id, group_id, action, period_date)
    )""")
    cursor.execute("""CREATE TABLE IF NOT EXISTS scheduler_runs (
        job_name TEXT NOT NULL, period_key TEXT NOT NULL,
        claimed_at TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'running', lease_until TEXT,
        attempt_count INTEGER NOT NULL DEFAULT 1, completed_at TEXT,
        PRIMARY KEY (job_name, period_key)
    )""")


def _sync_daily_coin_cap_trigger(
    cursor: sqlite3.Cursor,
    daily_coin_limit: int,
) -> None:
    """重建每日金币触发器，使部署后的上限配置立即成为数据库约束。"""

    cursor.execute("DROP TRIGGER IF EXISTS trg_users_daily_coin_cap")
    cursor.execute(f"""CREATE TRIGGER IF NOT EXISTS trg_users_daily_coin_cap
        AFTER UPDATE OF today_coins_earned ON users
        WHEN NEW.today_coins_earned > {daily_coin_limit}
        BEGIN
            UPDATE users SET
                coins = MAX(0, NEW.coins - (NEW.today_coins_earned - {daily_coin_limit})),
                today_coins_earned = {daily_coin_limit}
            WHERE user_id = NEW.user_id AND group_id = NEW.group_id;
        END""")


def _create_settlement_schema(
    cursor: sqlite3.Cursor,
    daily_coin_limit: int,
) -> None:
    """创建幂等结算、资产账本、奖励领取及余额约束结构。"""

    cursor.execute("""CREATE TABLE IF NOT EXISTS asset_ledger (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL, group_id INTEGER NOT NULL,
        asset_type TEXT NOT NULL, delta INTEGER NOT NULL,
        reason TEXT NOT NULL, reference_id TEXT,
        created_at TEXT NOT NULL,
        UNIQUE (asset_type, reference_id)
    )""")
    cursor.execute("""CREATE TABLE IF NOT EXISTS asset_reconciliation_checkpoints (
        group_id INTEGER NOT NULL, asset_type TEXT NOT NULL,
        balance INTEGER NOT NULL, ledger_total INTEGER NOT NULL,
        checked_at TEXT NOT NULL,
        PRIMARY KEY (group_id, asset_type)
    )""")
    cursor.execute("""CREATE TABLE IF NOT EXISTS visit_settlements (
        reference_id TEXT PRIMARY KEY,
        visitor_user_id TEXT NOT NULL, target_user_id TEXT NOT NULL,
        group_id INTEGER NOT NULL, pet_name TEXT NOT NULL,
        visitor_grant INTEGER NOT NULL, target_grant INTEGER NOT NULL,
        intimacy_grant INTEGER NOT NULL, created_at TEXT NOT NULL
    )""")
    cursor.execute("""CREATE TABLE IF NOT EXISTS minigame_settlements (
        reference_id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL, group_id INTEGER NOT NULL,
        game_type TEXT NOT NULL, opponent_user_id TEXT NOT NULL DEFAULT '',
        pet_name TEXT NOT NULL, opponent_pet_name TEXT NOT NULL DEFAULT '',
        coin_grant INTEGER NOT NULL, experience_grant INTEGER NOT NULL,
        energy_cost INTEGER NOT NULL, outcome_payload TEXT NOT NULL,
        created_at TEXT NOT NULL
    )""")
    cursor.execute("""CREATE TABLE IF NOT EXISTS group_task_claims (
        group_id INTEGER NOT NULL, task_type TEXT NOT NULL,
        created_date TEXT NOT NULL, user_id TEXT NOT NULL,
        claimed_at TEXT NOT NULL,
        PRIMARY KEY (group_id, task_type, created_date, user_id)
    )""")
    cursor.execute("""CREATE TABLE IF NOT EXISTS activity_claims (
        activity_id INTEGER NOT NULL, user_id TEXT NOT NULL,
        claimed_at TEXT NOT NULL,
        PRIMARY KEY (activity_id, user_id)
    )""")
    _sync_daily_coin_cap_trigger(cursor, daily_coin_limit)
    cursor.execute("""CREATE TRIGGER IF NOT EXISTS trg_users_nonnegative_insert
        BEFORE INSERT ON users WHEN NEW.coins < 0 OR NEW.friendship_points < 0
        BEGIN SELECT RAISE(ABORT, 'negative asset balance'); END""")
    cursor.execute("""CREATE TRIGGER IF NOT EXISTS trg_users_nonnegative_update
        BEFORE UPDATE OF coins, friendship_points ON users
        WHEN NEW.coins < 0 OR NEW.friendship_points < 0
        BEGIN SELECT RAISE(ABORT, 'negative asset balance'); END""")


def _create_market_schema(cursor: sqlite3.Cursor) -> None:
    """创建交易、每日群任务和装扮所有权表。"""

    cursor.execute("""CREATE TABLE IF NOT EXISTS trade_listings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        seller_user_id TEXT NOT NULL, group_id INTEGER NOT NULL,
        item_id TEXT NOT NULL, amount INTEGER DEFAULT 1,
        price INTEGER NOT NULL, created_at TEXT, expires_at TEXT,
        is_active BOOLEAN DEFAULT 1, status TEXT DEFAULT 'active')""")
    cursor.execute("""CREATE TABLE IF NOT EXISTS group_tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id INTEGER NOT NULL, task_type TEXT NOT NULL,
        target_value INTEGER NOT NULL, current_value INTEGER DEFAULT 0,
        reward_coins INTEGER DEFAULT 0, description TEXT DEFAULT '',
        created_date TEXT NOT NULL, is_completed BOOLEAN DEFAULT 0,
        UNIQUE (group_id, task_type, created_date))""")
    _migrate_group_tasks_table(cursor)
    cursor.execute("""CREATE TABLE IF NOT EXISTS dress_inventory (
        user_id TEXT NOT NULL, group_id INTEGER NOT NULL,
        dress_item_id TEXT NOT NULL,
        PRIMARY KEY (user_id, group_id, dress_item_id))""")


def _apply_legacy_column_migrations(cursor: sqlite3.Cursor) -> None:
    """按既定顺序为历史数据库补齐缺失字段。"""

    for table, column, column_type in _LEGACY_COLUMN_MIGRATIONS:
        _safe_add_column(cursor, table, column, column_type)


def _create_auxiliary_schema(cursor: sqlite3.Cursor) -> None:
    cursor.execute("""CREATE TABLE IF NOT EXISTS daily_likes (
        user_id TEXT NOT NULL, target_user_id TEXT NOT NULL,
        group_id INTEGER NOT NULL, like_date TEXT NOT NULL,
        like_count INTEGER DEFAULT 1,
        PRIMARY KEY (user_id, target_user_id, group_id, like_date))""")
    cursor.execute("""CREATE TABLE IF NOT EXISTS title_expiry (
        user_id TEXT NOT NULL, group_id INTEGER NOT NULL,
        title TEXT NOT NULL, expires_at TEXT NOT NULL,
        PRIMARY KEY (user_id, group_id, title))""")
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_daily_likes "
        "ON daily_likes(user_id, target_user_id, group_id, like_date)"
    )


def _repair_pet_show_schema_and_state(cursor: sqlite3.Cursor) -> None:
    """升级旧投票键，并保证每个群最多存在一个进行中的展示会。"""

    _migrate_pet_show_votes_table(cursor)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_pet_show_votes_show_voter "
        "ON pet_show_votes(show_id, voter_user_id)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_pet_show_votes_show_pet "
        "ON pet_show_votes(show_id, pet_user_id)"
    )
    cursor.execute(
        """UPDATE pet_shows SET is_active = 0, status = 'superseded'
           WHERE is_active = 1 AND EXISTS (
               SELECT 1 FROM pet_shows AS newer
               WHERE newer.group_id = pet_shows.group_id
                 AND newer.is_active = 1 AND newer.id > pet_shows.id
           )"""
    )
    cursor.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS uq_pet_shows_one_active_group
           ON pet_shows(group_id) WHERE is_active = 1"""
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_trade_expiry "
        "ON trade_listings(is_active, expires_at, group_id)"
    )


def _record_schema_version(cursor: sqlite3.Cursor) -> None:
    for version in range(1, _CURRENT_SCHEMA_VERSION + 1):
        cursor.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (version, utc_now().isoformat()),
        )
    cursor.execute(f"PRAGMA user_version={_CURRENT_SCHEMA_VERSION}")


def initialize_schema(
    conn: sqlite3.Connection,
    *,
    daily_coin_limit: int,
) -> None:
    """在一个事务中初始化或升级全部 Qingpet 数据库结构。"""

    cursor = conn.cursor()
    try:
        _create_schema_migration_table(cursor)
        _create_identity_schema(cursor)
        _create_interaction_schema(cursor)
        _create_settlement_schema(cursor, daily_coin_limit)
        _create_market_schema(cursor)
        _apply_legacy_column_migrations(cursor)
        _create_indexes(cursor)
        _create_auxiliary_schema(cursor)
        _repair_pet_show_schema_and_state(cursor)
        _record_schema_version(cursor)
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _create_indexes(cursor: sqlite3.Cursor) -> None:
    """在所有兼容迁移完成后创建常用查询索引。"""
    indexes = [
        "CREATE INDEX IF NOT EXISTS idx_cmd_ts_user ON command_timestamps(user_id, group_id, timestamp)",
        "CREATE INDEX IF NOT EXISTS idx_cmd_ts_group ON command_timestamps(group_id, timestamp)",
        "CREATE INDEX IF NOT EXISTS idx_op_logs ON operation_logs(group_id, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_tasks_date ON tasks(user_id, group_id, created_date)",
        "CREATE INDEX IF NOT EXISTS idx_pets_group ON pets(group_id)",
        "CREATE INDEX IF NOT EXISTS idx_trade_group ON trade_listings(group_id, is_active)",
        "CREATE INDEX IF NOT EXISTS idx_shows_group ON pet_shows(group_id, is_active)",
        "CREATE INDEX IF NOT EXISTS idx_group_tasks ON group_tasks(group_id, created_date)",
    ]
    for idx_sql in indexes:
        cursor.execute(idx_sql)


def _migrate_pet_show_votes_table(cursor: sqlite3.Cursor) -> None:
    row = cursor.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'pet_show_votes'"
    ).fetchone()
    sql_text = str(row["sql"] or "") if row else ""
    if "PRIMARY KEY (show_id, voter_user_id)" not in sql_text:
        return

    cursor.execute("ALTER TABLE pet_show_votes RENAME TO pet_show_votes_old")
    cursor.execute("""CREATE TABLE pet_show_votes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        show_id INTEGER NOT NULL,
        voter_user_id TEXT NOT NULL,
        pet_user_id TEXT NOT NULL,
        created_at TEXT)""")
    cursor.execute(
        """INSERT INTO pet_show_votes (show_id, voter_user_id, pet_user_id, created_at)
           SELECT show_id, voter_user_id, pet_user_id, created_at
           FROM pet_show_votes_old"""
    )
    cursor.execute("DROP TABLE pet_show_votes_old")


def _migrate_group_tasks_table(cursor: sqlite3.Cursor) -> None:
    """合并旧版重复群任务，并建立每日任务唯一键。"""
    table_info            = cursor.execute("PRAGMA table_info(group_tasks)").fetchall()
    created_date_not_null = any(
        row["name"] == "created_date" and int(row["notnull"]) == 1 for row in table_info
    )
    has_unique_key = False
    for index in cursor.execute("PRAGMA index_list(group_tasks)").fetchall():
        if int(index["unique"]) != 1:
            continue
        index_name = str(index["name"]).replace('"', '""')
        columns    = [
            str(row["name"])
            for row in cursor.execute(f'PRAGMA index_info("{index_name}")').fetchall()
        ]
        if columns == ["group_id", "task_type", "created_date"]:
            has_unique_key = True
            break
    if created_date_not_null and has_unique_key:
        return

    cursor.execute("DROP TABLE IF EXISTS group_tasks_v2")
    cursor.execute("""
        CREATE TABLE group_tasks_v2 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER NOT NULL,
            task_type TEXT NOT NULL,
            target_value INTEGER NOT NULL,
            current_value INTEGER DEFAULT 0,
            reward_coins INTEGER DEFAULT 0,
            description TEXT DEFAULT '',
            created_date TEXT NOT NULL,
            is_completed BOOLEAN DEFAULT 0,
            UNIQUE (group_id, task_type, created_date)
        )
    """)
    cursor.execute("""
        INSERT INTO group_tasks_v2
            (id, group_id, task_type, target_value, current_value,
             reward_coins, description, created_date, is_completed)
        SELECT
            keep_id,
            group_id,
            task_type,
            target_value,
            MIN(current_value, target_value),
            reward_coins,
            description,
            created_date,
            CASE
                WHEN was_completed = 1 OR current_value >= target_value THEN 1
                ELSE 0
            END
        FROM (
            SELECT
                MIN(id) AS keep_id,
                group_id,
                task_type,
                MAX(COALESCE(target_value, 0)) AS target_value,
                MAX(COALESCE(current_value, 0)) AS current_value,
                MAX(COALESCE(reward_coins, 0)) AS reward_coins,
                MAX(COALESCE(description, '')) AS description,
                COALESCE(created_date, '1970-01-01') AS created_date,
                MAX(COALESCE(is_completed, 0)) AS was_completed
            FROM group_tasks
            GROUP BY group_id, task_type, COALESCE(created_date, '1970-01-01')
        ) AS merged
    """)
    cursor.execute("DROP TABLE group_tasks")
    cursor.execute("ALTER TABLE group_tasks_v2 RENAME TO group_tasks")


def _safe_add_column(
    cursor: sqlite3.Cursor,
    table: str,
    column: str,
    col_type: str,
) -> None:
    if not _IDENTIFIER_RE.match(table) or not _IDENTIFIER_RE.match(column):
        logger.warning("Skip adding column with invalid identifier: %s.%s", table, column)
        return

    expected_type = _ALLOWED_SCHEMA_COLUMNS.get(table, {}).get(column)
    if expected_type is None:
        logger.warning("Skip adding unsupported schema column: %s.%s", table, column)
        return

    if col_type != expected_type:
        logger.warning(
            "Skip adding column with mismatched type: %s.%s expected=%s actual=%s",
            table,
            column,
            expected_type,
            col_type,
        )
        return

    existing_columns = {
        str(row["name"]) for row in cursor.execute(f"PRAGMA table_info({table})").fetchall()
    }
    if column in existing_columns:
        return
    sql = "ALTER TABLE " + table + " ADD COLUMN " + column + " " + col_type
    cursor.execute(sql)
