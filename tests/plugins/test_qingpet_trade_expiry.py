from __future__ import annotations

import asyncio
import json
import os
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from plugins.qingpet import main as qingpet_main
from plugins.qingpet.services import database_clock
from plugins.qingpet.services.database import Database
from plugins.qingpet.services.user_service import UserService

GROUP_ID = 13579
SELLER_ID = "seller"
BUYER_ID = "buyer"
FROZEN_NOW = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def trade_db(monkeypatch):
    monkeypatch.setattr(database_clock, "now", lambda: FROZEN_NOW)
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as file:
        db_path = file.name
    database = Database(db_path)
    users = UserService(database)
    seller = users.get_or_create_user(SELLER_ID, GROUP_ID)
    buyer = users.get_or_create_user(BUYER_ID, GROUP_ID)
    seller.coins = 100
    buyer.coins = 200
    assert database.update_user(seller)
    assert database.update_user(buyer)
    assert database.purchase_item_atomic(SELLER_ID, GROUP_ID, "apple", 3, 0)[0]
    assert database.create_trade_listing_atomic(
        SELLER_ID,
        GROUP_ID,
        "apple",
        3,
        90,
        expire_hours=72,
        max_listings=5,
    )
    listing_id = int(
        database._get_connection().execute("SELECT id FROM trade_listings").fetchone()["id"]
    )
    try:
        yield database, db_path, listing_id
    finally:
        database.cleanup()
        os.unlink(db_path)
        backup_path = db_path + ".pre-migration.bak"
        if os.path.exists(backup_path):
            os.unlink(backup_path)


def _set_expiry(database: Database, listing_id: int, expires_at: datetime) -> None:
    database._get_connection().execute(
        "UPDATE trade_listings SET expires_at = ? WHERE id = ?",
        (expires_at.isoformat(), listing_id),
    )
    database._get_connection().commit()


def _raw_listing(database: Database, listing_id: int) -> dict:
    row = (
        database._get_connection()
        .execute("SELECT * FROM trade_listings WHERE id = ?", (listing_id,))
        .fetchone()
    )
    assert row is not None
    return dict(row)


def _asset_state(database: Database, listing_id: int) -> dict:
    seller = database.get_user(SELLER_ID, GROUP_ID)
    buyer = database.get_user(BUYER_ID, GROUP_ID)
    assert seller is not None and buyer is not None
    ledger_count = int(
        database._get_connection()
        .execute(
            "SELECT COUNT(*) AS count FROM asset_ledger WHERE reference_id = ?",
            (f"trade-expire:{listing_id}",),
        )
        .fetchone()["count"]
    )
    return {
        "seller_coins": seller.coins,
        "buyer_coins": buyer.coins,
        "seller_items": database.get_or_create_inventory(SELLER_ID, GROUP_ID).items,
        "buyer_items": database.get_or_create_inventory(BUYER_ID, GROUP_ID).items,
        "ledger_count": ledger_count,
    }


def test_saved_listing_id_cannot_purchase_at_expiry_and_refunds_once(trade_db):
    database, _db_path, listing_id = trade_db
    _set_expiry(database, listing_id, FROZEN_NOW)

    success, message = database.purchase_trade_listing(listing_id, BUYER_ID, GROUP_ID, 0.05)

    assert not success
    assert "过期" in str(message)
    assert _raw_listing(database, listing_id)["status"] == "expired"
    assert _asset_state(database, listing_id) == {
        "seller_coins": 100,
        "buyer_coins": 200,
        "seller_items": {"apple": 3},
        "buyer_items": {},
        "ledger_count": 1,
    }
    assert database.settle_expired_trade_listings(GROUP_ID) == 0
    assert database.get_listing_by_id(listing_id, GROUP_ID) is None
    assert _asset_state(database, listing_id)["seller_items"] == {"apple": 3}
    assert _asset_state(database, listing_id)["ledger_count"] == 1


def test_exact_expiry_purchase_cancel_and_settlement_have_one_terminal_state(trade_db):
    database, db_path, listing_id = trade_db
    _set_expiry(database, listing_id, FROZEN_NOW)
    contenders = [Database(db_path) for _ in range(3)]
    barrier = threading.Barrier(3)

    def purchase():
        barrier.wait()
        return contenders[0].purchase_trade_listing(listing_id, BUYER_ID, GROUP_ID, 0.05)

    def cancel():
        barrier.wait()
        return contenders[1].cancel_trade_listing(listing_id, SELLER_ID, GROUP_ID)

    def expire():
        barrier.wait()
        return contenders[2].settle_expired_trade_listings(GROUP_ID)

    try:
        with ThreadPoolExecutor(max_workers=3) as executor:
            purchase_result, cancel_result, settled = [
                future.result()
                for future in [
                    executor.submit(purchase),
                    executor.submit(cancel),
                    executor.submit(expire),
                ]
            ]
    finally:
        for contender in contenders:
            contender.cleanup()

    assert purchase_result[0] is False
    assert cancel_result is False
    assert settled in {0, 1}
    listing = _raw_listing(database, listing_id)
    assert listing["is_active"] == 0
    assert listing["status"] == "expired"
    assert _asset_state(database, listing_id) == {
        "seller_coins": 100,
        "buyer_coins": 200,
        "seller_items": {"apple": 3},
        "buyer_items": {},
        "ledger_count": 1,
    }


def test_unexpired_purchase_and_cancel_claim_only_one_terminal_state(trade_db):
    database, db_path, listing_id = trade_db
    _set_expiry(database, listing_id, FROZEN_NOW + timedelta(seconds=1))
    purchase_db = Database(db_path)
    cancel_db = Database(db_path)
    barrier = threading.Barrier(2)

    def purchase():
        barrier.wait()
        return purchase_db.purchase_trade_listing(listing_id, BUYER_ID, GROUP_ID, 0.05)

    def cancel():
        barrier.wait()
        return cancel_db.cancel_trade_listing(listing_id, SELLER_ID, GROUP_ID)

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            purchase_future = executor.submit(purchase)
            cancel_future = executor.submit(cancel)
            purchase_result = purchase_future.result()
            cancel_result = cancel_future.result()
    finally:
        purchase_db.cleanup()
        cancel_db.cleanup()

    assert int(purchase_result[0]) + int(cancel_result) == 1
    listing = _raw_listing(database, listing_id)
    assert listing["is_active"] == 0
    state = _asset_state(database, listing_id)
    if listing["status"] == "purchased":
        assert state == {
            "seller_coins": 186,
            "buyer_coins": 110,
            "seller_items": {},
            "buyer_items": {"apple": 3},
            "ledger_count": 0,
        }
    else:
        assert listing["status"] == "cancelled"
        assert state == {
            "seller_coins": 100,
            "buyer_coins": 200,
            "seller_items": {"apple": 3},
            "buyer_items": {},
            "ledger_count": 0,
        }


def test_trade_expiry_has_independent_scheduled_handler(monkeypatch):
    calls = 0

    class FakeDatabase:
        def settle_expired_trade_listings(self):
            nonlocal calls
            calls += 1
            return 2

    monkeypatch.setattr(qingpet_main, "_db_instance", FakeDatabase())

    assert asyncio.run(qingpet_main.scheduled_trade_expiry(None)) == []
    assert calls == 1
    root = Path(__file__).resolve().parents[2]
    manifest = json.loads((root / "plugins/qingpet/plugin.json").read_text(encoding="utf-8"))
    jobs = {job["id"]: job for job in manifest["schedule"]}
    assert jobs["qingpet_trade_expiry"]["handler"] == "scheduled_trade_expiry"
    assert jobs["qingpet_trade_expiry"]["cron"] == {"minute": "*/5"}
