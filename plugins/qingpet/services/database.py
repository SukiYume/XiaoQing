"""QingPet 的 SQLite 模式迁移、领域持久化和原子结算实现。"""

import logging
import sqlite3
import threading
import uuid
from contextlib import closing
from pathlib import Path

from .database_actions import AtomicActionRepositoryMixin
from .database_community import CommunityRepositoryMixin
from .database_identity import IdentityRepositoryMixin
from .database_scheduler import SchedulerRepositoryMixin
from .database_schema import initialize_schema
from .database_support import (
    _DAILY_COIN_LIMIT,
)
from .database_types import (
    CoinLedgerReconciliation,
    DailyResetResult,
    GroupEconomySnapshot,
    LeaveMessageAtomicResult,
    MinigameAtomicResult,
    MinigameOutcome,
    PetActionAtomicResult,
    PetShowSettlementResult,
    PetShowWinner,
    TreatPetAtomicResult,
    VisitPetAtomicResult,
    WeeklyActivitySettlementResult,
    WeeklyRankingWinner,
)

logger = logging.getLogger(__name__)

# 这些结果类型此前由 database.py 定义；继续从原路径导出，避免破坏服务与测试的公开导入。
__all__ = [
    "CoinLedgerReconciliation",
    "DailyResetResult",
    "Database",
    "GroupEconomySnapshot",
    "LeaveMessageAtomicResult",
    "MinigameAtomicResult",
    "MinigameOutcome",
    "PetActionAtomicResult",
    "PetShowSettlementResult",
    "PetShowWinner",
    "TreatPetAtomicResult",
    "VisitPetAtomicResult",
    "WeeklyActivitySettlementResult",
    "WeeklyRankingWinner",
]


class Database(
    IdentityRepositoryMixin,
    CommunityRepositoryMixin,
    AtomicActionRepositoryMixin,
    SchedulerRepositoryMixin,
):
    """QingPet 数据库服务层。

    负责表结构迁移、索引、缓存和领域数据持久化；涉及配额、资产或多张表的状态变更由本类
    在同一 SQLite 事务中提交，避免服务层分步写入造成部分成功。
    """

    def __init__(self, db_path: str) -> None:
        self.db_path                                                     = db_path
        self._local                                                      = threading.local()
        self._connections_lock                                           = threading.Lock()
        self._all_connections: dict[int, tuple[int, sqlite3.Connection]] = {}
        path                                                             = Path(db_path)
        if path.exists() and path.stat().st_size > 0:
            backup = path.with_suffix(path.suffix + ".pre-migration.bak")
            if not backup.exists():
                # SQLite 在线备份同时读取已提交的 WAL，发布前先完成一致快照。
                temporary = backup.with_suffix(backup.suffix + f".{uuid.uuid4().hex}.tmp")
                try:
                    with (
                        closing(sqlite3.connect(path)) as source,
                        closing(sqlite3.connect(temporary)) as target,
                    ):
                        source.backup(target)
                    temporary.replace(backup)
                finally:
                    temporary.unlink(missing_ok=True)
        self._init_database()

    def _get_connection(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            # 查询与事务不跨线程共享；关闭时由生命周期线程统一处理登记连接。
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = conn
            with self._connections_lock:
                self._all_connections[id(conn)] = (threading.get_ident(), conn)
        return conn

    def cleanup(self) -> None:
        with self._connections_lock:
            connections = list(self._all_connections.values())
            self._all_connections.clear()

        failures: list[sqlite3.Error] = []
        for thread_id, conn in connections:
            try:
                conn.close()
            except sqlite3.Error as exc:
                logger.error(
                    "Failed to close QingPet SQLite connection thread=%s error_type=%s",
                    thread_id,
                    type(exc).__name__,
                )
                failures.append(exc)
        if hasattr(self._local, "conn"):
            self._local.conn = None
        if failures:
            raise RuntimeError(
                f"Failed to close {len(failures)} QingPet SQLite connection(s)"
            ) from failures[0]

    # ──────────────────── 初始化 ────────────────────

    def _init_database(self) -> None:
        """创建目录，并让 schema 模块在同一连接和事务中完成初始化。"""

        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        initialize_schema(
            self._get_connection(),
            daily_coin_limit=_DAILY_COIN_LIMIT,
        )
