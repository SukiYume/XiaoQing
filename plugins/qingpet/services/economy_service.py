"""群经济汇总与金币账本一致性展示。"""

from typing import TypedDict

from .database import Database


class GroupStats(TypedDict):
    """管理命令展示所需的稳定群统计字段。"""

    total_pets: int
    total_coins: int
    total_experience: int
    total_intimacy: int
    avg_care_score: float
    active_today: int
    coins_per_pet: float
    ledger_status: str
    ledger_consistent: bool
    ledger_expected_coins: int
    ledger_difference: int


class EconomyService:
    """读取数据库聚合快照，并生成管理员可读的经济摘要。"""

    def __init__(self, db: Database) -> None:
        self.db = db

    def get_group_stats(self, group_id: int) -> GroupStats:
        """用一次聚合查询返回群总量，并附上持久化金币账本核对结果。"""
        snapshot, reconciliation = self.db.get_group_economy_snapshot(group_id)

        return {
            "total_pets": snapshot.total_pets,
            "total_coins": snapshot.total_coins,
            "total_experience": snapshot.total_experience,
            "total_intimacy": snapshot.total_intimacy,
            "avg_care_score": round(snapshot.average_care_score, 1),
            "active_today": snapshot.active_today,
            "coins_per_pet": (
                round(snapshot.total_coins / snapshot.total_pets, 1) if snapshot.total_pets else 0.0
            ),
            "ledger_status": reconciliation.status,
            "ledger_consistent": reconciliation.consistent,
            "ledger_expected_coins": reconciliation.expected_balance,
            "ledger_difference": reconciliation.difference,
        }

    def format_stats(self, group_id: int) -> str:
        """生成管理员命令使用的群统计文本。"""
        stats = self.get_group_stats(group_id)
        lines = [
            "📊 **群宠物统计**",
            "",
            f"🐾 宠物总数: {stats['total_pets']}",
            f"👥 今日活跃: {stats['active_today']}",
            f"💰 金币总量: {stats['total_coins']}",
            f"💰 人均金币: {stats['coins_per_pet']}",
            f"🎯 经验总量: {stats['total_experience']}",
            f"💕 亲密总量: {stats['total_intimacy']}",
            f"🌟 平均照顾评分: {stats['avg_care_score']}%",
        ]
        if stats["ledger_consistent"]:
            label = "已建立基线" if stats["ledger_status"] == "baseline_created" else "一致"
            lines.append(f"📒 金币账本: {label}")
        else:
            lines.append(f"⚠️ 金币账本: 余额与账本不一致 (差异 {stats['ledger_difference']:+d})")
        return "\n".join(lines) + "\n"
