"""Start a standalone Pendo Web server with deterministic E2E seed data.

This avoids loading the whole host application, so it is useful when testing the
Pendo SPA and Web API in isolation.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from plugins.pendo.config import PendoConfig
from plugins.pendo.services.db import Database
from plugins.pendo.web import server
from plugins.pendo.web.auth import generate_token


OWNER_ID = "pendo-web-e2e"


def _seed_payloads(owner_id: str) -> list[dict]:
    now = "2026-04-30T20:20:00"
    return [
        {
            "id": "TEST_WEB_EVENT_20260430",
            "owner_id": owner_id,
            "type": "event",
            "title": "TEST_WEB_EVENT_概览",
            "start_time": "2026-04-30T09:00:00",
            "end_time": "2026-04-30T10:00:00",
            "category": "测试",
            "location": "上海",
            "created_at": now,
            "updated_at": now,
        },
        {
            "id": "TEST_WEB_TASK_20260430",
            "owner_id": owner_id,
            "type": "task",
            "title": "TEST_WEB_TASK_检查页面",
            "content": "XSS <img src=x onerror=alert(1)>",
            "status": "open",
            "priority": 2,
            "plan_date": "2026-04-30",
            "created_at": now,
            "updated_at": now,
        },
        {
            "id": "TEST_WEB_NOTE_20260430",
            "owner_id": owner_id,
            "type": "note",
            "title": "TEST_WEB_NOTE_导入说明",
            "content": "<script>alert(1)</script>",
            "category": "测试",
            "tags": ["TEST_WEB"],
            "created_at": now,
            "updated_at": now,
        },
        {
            "id": "TEST_WEB_DIARY_20260430",
            "owner_id": owner_id,
            "type": "diary",
            "title": "TEST_WEB_DIARY_回归",
            "content": "页面回归测试",
            "diary_date": "2026-04-30",
            "entry_time": "2026-04-30T21:00:00",
            "created_at": now,
            "updated_at": now,
        },
        {
            "id": "TEST_WEB_LEDGER_20260430_EXP",
            "owner_id": owner_id,
            "type": "ledger",
            "title": "TEST_WEB_LEDGER_午饭",
            "amount": 0,
            "amount_cents": 3250,
            "transaction_type": "expense",
            "ledger_category": "餐饮",
            "ledger_date": "2026-04-30",
            "account_name": "微信",
            "created_at": now,
            "updated_at": now,
        },
        {
            "id": "TEST_WEB_LEDGER_20260430_INC",
            "owner_id": owner_id,
            "type": "ledger",
            "title": "TEST_WEB_LEDGER_工资",
            "amount": 0,
            "amount_cents": 1800000,
            "transaction_type": "income",
            "ledger_category": "工资",
            "ledger_date": "2026-04-30",
            "account_name": "招行",
            "created_at": now,
            "updated_at": now,
        },
    ]


def seed_data(db: Database, owner_id: str) -> None:
    for payload in _seed_payloads(owner_id):
        item_id = payload["id"]
        if db.get_item(item_id, owner_id):
            db.update_item(item_id, payload, owner_id=owner_id)
        else:
            db.insert_item(payload)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="plugins/pendo/data/pendo.db")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18765)
    parser.add_argument("--owner-id", default=OWNER_ID)
    parser.add_argument("--no-seed", action="store_true")
    args = parser.parse_args()

    db_path = Path(args.db)
    db = Database(str(db_path))
    if not args.no_seed:
        seed_data(db, args.owner_id)

    PendoConfig.WEB_HOST = args.host
    PendoConfig.WEB_PORT = args.port
    token = generate_token(args.owner_id, expires_hours=24)
    print(f"OWNER_ID={args.owner_id}", flush=True)
    print(f"TOKEN={token}", flush=True)
    started = server.start(db)
    print(f"STARTED={started}", flush=True)
    while True:
        time.sleep(5)


if __name__ == "__main__":
    main()
