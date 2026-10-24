# 🛠️ QingPet maintenance rules

[中文](AGENTS.md) | English

These rules apply to code, tests, and documents under `plugins/qingpet/`. See [README.en.md](README.en.md) for current functions. See [QUICKSTART.en.md](QUICKSTART.en.md) for initial deployment checks.

## 📚 Authoritative sources

Before changes, check these sources in sequence:

1. Check `plugin.json` for commands, aliases, permissions, contexts, and schedule contracts.
2. Check `main.py` for the plugin lifecycle, routing, and schedule entries.
3. Check `commands/` for argument parsing and user messages.
4. Check `models/` for domain data structures.
5. Check `services/` for transactions, repositories, and business rules.
6. Check `utils/constants.py` for cooldown, count, growth, product, and activity constants.
7. Check `tests/plugins/qingpet/` for behavior and regression contracts.

Documents describe the current code and Manifest. For new functions, update command metadata, help, user documents, and tests in the same change.

## 🔐 Module boundaries

| Directory or file | Function |
| --- | --- |
| `main.py` | Initialization, shutdown, command routing, permission context, and schedule entries |
| `commands/basic_commands.py` | Adoption and basic care |
| `commands/advanced_commands.py` | Growth, items, and advanced actions |
| `commands/new_commands.py` | Social actions, games, tasks, trades, and pet shows |
| `commands/admin_commands.py` | Group settings, user management, activities, and audits |
| `models/` | User, pet, item, inventory, group setting, and log models |
| `services/database.py` | Repository composition and connection lifecycle |
| `services/database_*` | Schema, identity, community, action, schedule, and shared storage functions |
| Other `services/` | Pet, user, item, social, economy, and administration operations |
| `utils/` | Routing, validation, formatting, time, and constants |

Command modules consume input and prepare replies. Domain services apply business rules. Repositories read and write the database. Operations across modules combine service methods.

## 🔐 Data safety

The production database path is:

```text
data/qingpet/qingpet/qingpet.db
```

Development and tests use these rules:

- Tests use `tmp_path` for isolated SQLite databases.
- Before migration, use SQLite `Connection.backup()` for a consistent online snapshot. It includes committed WAL data. Validate migration on a snapshot copy.
- Cleanup scripts target test directories, cache directories, or explicitly supplied paths.
- Schema changes use compatible migrations and existing-database upgrade tests.
- Asset and state repairs use transaction services and audit records.
- Production data reads must belong to a diagnostic or migration task explicitly authorized by the user.

## 🔐 Tenants and permissions

The business tenant key is `group_id`. The user key is `user_id`. All queries, updates, rankings, tasks, activities, and trades must include group scope.

Administrator identity comes from Core-authenticated events. Applicable roles are group administrator, group owner, and Bot administrator. Administration commands declare permissions and group context in the Manifest and runtime entry. Destructive operations, such as reset and delete, must include confirmation in the same command. They also write an administration audit log.

## 📌 Transactions and idempotency

Each operation below completes all asset and state changes in one database transaction:

- Feeding, cleaning, play, training, exploration, and treatment.
- Visits, gifts, and games.
- Task, activity, and pet show rewards.
- Trade listings, purchases, cancellations, and expiry returns.
- Daily resets, weekly settlement, and asset snapshots.

OneBot operations that can be delivered again use stable request identities. Database unique constraints or idempotency records keep the first result. Later calls with that identity return the saved result. Transaction tests cover success, business rejection, exception rollback, concurrent competition, and repeated delivery.

## 💾 Asset consistency

The `users` table holds current coin and friendship point balances. `asset_ledger` holds each controlled change. Asset services update balances and the ledger in the same transaction. Group statistics calculate differences between balances and ledger aggregates. Administrators investigate these differences.

For each new asset entry, define these details:

- Change direction, amount, and asset type.
- Operation type and related object.
- Request identity and idempotency behavior.
- Daily limit, cooldown, and reward reduction.
- Failure rollback and concurrency tests.
- Administration statistics and audit fields.

## ⏰ Time and schedules

Time calculations enter database transactions through `services/database_clock.py` and `utils/time.py`. Tests use a controllable clock.

Schedule entries stay small. `database_scheduler.py` and domain services do settlement.

| Entry | Core delivery | Data action |
| --- | --- | --- |
| `scheduled_decay` | `targeted` | Attribute decreases, travel state, rate-limit cleanup, and threshold alerts |
| `scheduled_daily_reset` | `silent` | Daily counts, age, and date boundaries |
| `scheduled_trade_expiry` | `silent` | Expired orders and escrow inventory |
| `scheduled_pet_show_settlement` | `targeted` | Pet show closure, rewards, and group settlement results |
| `scheduled_weekly_activity` | `targeted` | Weekly rankings, rewards, titles, and group settlement results |

Schedules support repeated ticks, recovery across restarts, re-entry within one instance, and competing transactions. Read deadlines again inside the transaction.

## ⌨️ Commands and help

For new or changed commands:

1. Update names, aliases, usage, examples, error examples, permissions, and contexts in `plugin.json`.
2. Implement full argument consumption in the applicable command module.
3. Set limits for argument length, count, control characters, QQ identity, and group scope.
4. Put the command in `基础`, `进阶`, `道具`, `社交`, `玩法`, or `管理` help.
5. Keep mobile summaries short. Show details through category help.
6. Update README and Manifest document contract tests.

Public errors use stable Chinese messages. Logs record operation type, length, count, status, and summaries. Keep user text and sensitive values outside logs.

## 🛠️ Schema changes

1. Define new tables, columns, indexes, or constraints in `database_schema.py`.
2. Add idempotent migrations for existing databases.
3. Implement reads and writes in the applicable repository file.
4. Combine the necessary mixin in `database.py`.
5. Update shutdown, connection registration, and transaction tests.
6. Add tests for new databases, old database upgrades, and concurrent access.
7. Update data boundaries in README.

Repository separation keeps one combined `Database` object and one SQLite file.

## ✅ Required checks

First run tests for the changed module. Then run the full QingPet suite:

```bash
python -m ruff check plugins/qingpet tests/plugins/qingpet
python -m mypy plugins/qingpet
python -m pytest -q tests/plugins/qingpet -n 2
```

These checks are also necessary for high-risk changes:

- Real SQLite transactions and constraints.
- Competition between two concurrent connections.
- Repeated scheduled task execution.
- Repeated OneBot message delivery.
- Administrator and standard member permissions.
- Tenant isolation between groups.
- Database shutdown and plugin reload.
- Mobile help length and category structure.

## ✅ Checks before commit

- `plugin.json` agrees with the routing implementation.
- Public help agrees with README.
- New fields have migration, serialization, and validation.
- Asset changes have ledger records, idempotency, and rollback.
- Scheduled states have deadlines and repeated-execution tests.
- Test data stays in temporary directories.
- Ruff, mypy, and all QingPet pytest tests pass.
- `git diff --check` passes.
