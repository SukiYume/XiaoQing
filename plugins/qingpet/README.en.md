# 🐾 QingPet

[中文](README.md) | English

QingPet is a pet care plugin. It keeps data separate for each QQ group. Members can adopt, care for, train, and dress pets. They can visit members, give gifts, play games, complete tasks, trade items, and enter pet shows. Group administrators control settings, activities, and plugin users.

## ⌨️ Command entry

| Function | Recommended command | Manifest aliases |
| --- | --- | --- |
| QQ group pet system | `/宠物` | `/qingpet` `/pet` |

Mobile help has these categories:

```text
/宠物 帮助
/宠物 帮助 基础
/宠物 帮助 进阶
/宠物 帮助 道具
/宠物 帮助 社交
/宠物 帮助 玩法
/宠物 帮助 管理
```

A help category can also be a subcommand. Example: `/宠物 社交`.

## 🐾 Adoption and care

| Command | Function |
| --- | --- |
| `/宠物 领养 <名字>` | Adopt an egg in the current group. |
| `/宠物 状态 [群号]` | Show attributes, clothing, assets, and titles. |
| `/宠物 喂食 [群号] [食物]` | Use the specified food. The default food is an apple. |
| `/宠物 清洁 [群号]` | Increase cleanliness and health. |
| `/宠物 玩耍 [群号]` | Increase mood and intimacy. |
| `/宠物 睡觉 [群号]` | Start rest. |
| `/宠物 起床 [群号]` | End rest. |
| `/宠物 训练 [群号] [体力\|敏捷\|智力]` | Use energy to increase growth. |
| `/宠物 探索 [群号] [森林\|海边\|山洞\|废墟]` | Start random events and item rewards. |
| `/宠物 治疗 [群号] [药品]` | Remove medicine and restore health in one transaction. |
| `/宠物 召回 [群号]` | Pay coins to recall a pet from travel. |
| `/宠物 改名 [群号] <新名字>` | Change the pet name. |

## 🐾 Items and clothing

```text
/宠物 商店
/宠物 背包
/宠物 购买 <道具> [1-99]
/宠物 使用 [群号] <加速卡或托管券>
/宠物 装扮 商店
/宠物 装扮 查看
/宠物 装扮 购买 <装扮ID>
/宠物 装扮 穿戴 <装扮ID>
/宠物 装扮 卸下 <帽子|衣服|饰品|背景>
```

Standard items cost coins. Clothing costs coins or friendship points, as specified by the product configuration.

## 🐾 Social actions

```text
/宠物 查看 @QQ号
/宠物 互访 @QQ号
/宠物 摸摸 @QQ号
/宠物 送礼 @QQ号 <道具> [数量]
/宠物 留言 @QQ号 <内容>
/宠物 留言
/宠物 排行 [care_score|intimacy|experience|coins]
```

Views, visits, petting, gifts, and messages use the current group. Social actions have cooldowns, daily limits, content checks, and transaction protection.

## 🐾 Group activities

```text
/宠物 游戏 猜拳 <石头|剪刀|布>
/宠物 游戏 骰子
/宠物 游戏 赛跑 @QQ号
/宠物 任务 [群号] [领取]
/宠物 群任务 [领取]
/宠物 活动 [领取 <活动ID>]
/宠物 称号
/宠物 展示 [投票 @QQ号]
```

Personal tasks count actions for each user and group. Group tasks count actions for the group. Activity, task, game, and pet show rewards use unique request identifiers or database constraints for idempotency.

## 🐾 Trade market

The group setting `trade_enabled` controls the market.

```text
/宠物 交易 列表
/宠物 交易 挂单 <道具ID> <数量> <价格>
/宠物 交易 购买 <订单号>
/宠物 交易 撤单 <订单号>
```

A listing puts items in escrow. Purchases, cancellations, and expiry returns update orders, inventory, balances, and the asset ledger in one transaction.

## 🐾 Growth rules

| Current stage | Required experience | Required age | Next stage |
| --- | ---: | ---: | --- |
| Egg | 10 | 0 days | Young |
| Young | 50 | 7 days | Growing |
| Growing | 100 | 21 days | Mature |
| Mature | 150 | 60 days | Old |

The care score affects the form of young and growing pets. The score is the mean proportion of hunger, mood, cleanliness, energy, and health.

Attributes decrease with time. Managed care halves this decrease. Low health causes illness. A long period of low care can cause travel. Treatment and recall help the pet recover.

Schema 6 stores fractional attribute decreases in `decay_remainders`. This keeps cumulative decreases consistent across settlement intervals and restarts. Daily counts, check-ins, consecutive days, and weekly rankings use the `Asia/Shanghai` business date. Scheduled trigger times use the Core scheduler timezone.

## 🔐 Economy and abuse controls

- The `users` table holds the current coin and friendship point balances.
- Each controlled asset change also enters `asset_ledger`.
- Group statistics calculate differences between balances and the asset ledger.
- Important actions have cooldowns, daily limits, group response rate limits, and reduced rewards for repeated actions.
- Visits, games, treatment, trades, tasks, and activity rewards use atomic transactions or idempotency identifiers.
- Names, messages, and display text have checks for length, control characters, and prohibited words.

## 🐾 Group administration

Core must authenticate the caller as a group administrator, group owner, or Bot administrator.

```text
/宠物 管理 开启
/宠物 管理 关闭
/宠物 管理 配置 查看
/宠物 管理 配置 设置 <key> <value>
/宠物 管理 重置 @QQ号 确认
/宠物 管理 删除 @QQ号 确认
/宠物 管理 封禁 @QQ号 <天数>
/宠物 管理 解封 @QQ号
/宠物 管理 日志 [1-100]
/宠物 管理 统计
/宠物 管理 活动 创建 <feed|clean|play|train|explore> <目标数> <奖励金币> [标题]
/宠物 管理 公告 展示会 [标题]
/宠物 管理 公告 结束展示会
```

Reset and delete commands must include `确认` in the same command. Administrator actions enter the audit log.

## ⚙️ Group settings

| Setting | Type or range | Default |
| --- | --- | --- |
| `enabled` | Boolean | `true` |
| `economy_multiplier` | 0.1–10.0 | `1.0` |
| `decay_multiplier` | 0.1–10.0 | `1.0` |
| `trade_enabled` | Boolean | `false` |
| `natural_trigger_enabled` | Boolean | `false` |
| `activity_enabled` | Boolean | `true` |

## ✨ Group selection in private chat

For an account with one pet, inventory, shops, clothing, and self actions automatically select the pet group. For pets in multiple groups, put the group number first in self-action arguments.

```text
/宠物 喂食 123456 apple
/宠物 任务 123456 领取
```

`/宠物 状态` without a group number lists all group pets for the account. Use social actions and group activities in the target group.

## ⏰ Scheduled tasks

| Task | Interval | Core delivery | Function |
| --- | --- | --- | --- |
| `qingpet_decay` | Each minute | `targeted` | Apply attribute decreases. Give threshold alerts to Core for group delivery. |
| `qingpet_pet_show_settlement` | Each minute | `targeted` | Settle expired pet shows atomically. Give results to Core for group delivery. |
| `qingpet_trade_expiry` | Each 5 minutes | `silent` | Return escrow items from expired listings. |
| `qingpet_daily_reset` | Daily at 00:00 | `silent` | Reset daily counts and increase pet age. |
| `qingpet_weekly_activity` | Monday at 10:00 | `targeted` | Settle weekly ranking rewards and the “本周之星” title. Publish group results. |

Targeted tasks return `ScheduledDelivery`. Core checks each group against the schedule `group_ids` or the global default groups, then sends the message. Silent tasks update business state only.

## 🔐 Data and internal boundaries

Normal loading uses this SQLite database:

```text
data/qingpet/qingpet/qingpet.db
```

One `Database` combines repository mixins. They share one SQLite file, connection lifecycle, and transaction boundary.

| Module | Function |
| --- | --- |
| `services/database.py` | Composition entry, thread connections, and shutdown lifecycle |
| `services/database_schema.py` | Tables, indexes, migrations, and constraints |
| `services/database_identity.py` | Users, pets, inventory, and group settings |
| `services/database_community.py` | Tasks, activities, messages, trades, and pet shows |
| `services/database_actions.py` | Quotas, assets, and pet action transactions |
| `services/database_scheduler.py` | Periodic settlement, expiry cleanup, and economy snapshots |
| Domain services | Pet, social, item, economy, user, and administration operations |

The plugin and administrator backup procedures manage runtime data. Before migration, SQLite online backup creates a consistent `.pre-migration.bak` snapshot. This snapshot includes committed WAL data. Routine administrator backups also use the SQLite backup interface.

## 🩺 Troubleshooting

| Symptom | Check or action |
| --- | --- |
| Action has a cooldown | Read the remaining time in the reply. Try again after that time. |
| Rewards decrease | Check daily counts and repeated-action controls. |
| Market is closed | A group administrator sets `trade_enabled=true`. |
| Private chat requests a group | Put the group number first, or use the command in the target group. |
| Administration permission fails | Check group roles in the OneBot event and Core administrator settings. |
| Database error | Keep the logs. Make a consistent SQLite backup. Investigate transaction and migration problems on a copy. |

## ✅ Development checks

Run these commands from the repository root:

```bash
python -m ruff check plugins/qingpet tests/plugins/qingpet
python -m mypy plugins/qingpet
python -m pytest -q tests/plugins -k qingpet -n 2
```

See [QUICKSTART.en.md](QUICKSTART.en.md) for initial deployment. See [AGENTS.en.md](AGENTS.en.md) for maintenance rules.
