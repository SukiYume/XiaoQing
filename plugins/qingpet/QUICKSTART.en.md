# 🚀 QingPet quick start

[中文](QUICKSTART.md) | English

This guide helps first-time operators check permissions, the database, pet care, social actions, and scheduled tasks with QQ group commands. See [README.en.md](README.en.md) for all functions.

## 🚀 1. Start the project

QingPet loads with XiaoQing. Run this command in an environment with the project dependencies installed:

```bash
python main.py
```

Startup logs must show the QingPet Manifest load and SQLite initialization results. The database path is:

```text
data/qingpet/qingpet/qingpet.db
```

## ✅ 2. Check group administrator permissions

A current group administrator, group owner, or Bot administrator sends these commands:

```text
/宠物 管理 开启
/宠物 管理 配置 查看
```

The reply must include `enabled`, economy and decay multipliers, and the trade, natural-trigger, and activity switches.

## 🚀 3. Complete initial pet care

Use a standard group member account. Send these commands in sequence:

```text
/宠物 领养 小白
/宠物 状态
/宠物 喂食 apple
/宠物 清洁
/宠物 玩耍
```

The expected results are:

- Adoption creates one pet egg.
- The status card shows five attributes, stage, experience, and assets.
- Each care action updates state, experience, task progress, and the asset ledger.
- Repeated actions within a short interval return a cooldown message.

The egg hatches at 10 experience points. Later evolution uses experience, age, and the care score.

## ✅ 4. Check items

```text
/宠物 商店
/宠物 购买 apple 2
/宠物 背包
/宠物 装扮 商店
/宠物 装扮 查看
```

The purchase quantity range is 1–99. Product output gives the current price, currency, and inventory rules.

## ✅ 5. Check social actions

Prepare two group member accounts with adopted pets:

```text
/宠物 查看 @对方QQ
/宠物 互访 @对方QQ
/宠物 摸摸 @对方QQ
/宠物 送礼 @对方QQ apple 1
/宠物 留言 @对方QQ 今天也要开心
/宠物 排行 care_score
```

Check group isolation, atomic inventory and reward updates, message content validation, and ranking updates for both accounts.

## ✅ 6. Check activities and trades

```text
/宠物 游戏 骰子
/宠物 任务
/宠物 群任务
/宠物 活动
/宠物 展示
```

A group administrator enables the market:

```text
/宠物 管理 配置 设置 trade_enabled true
/宠物 交易 挂单 apple 1 20
/宠物 交易 列表
```

Use the second account to buy a listed order. Then check both inventories, coin balances, and group statistics.

## ✅ 7. Check administrator actions

```text
/宠物 管理 日志 20
/宠物 管理 统计
/宠物 管理 活动 创建 feed 10 50 全群喂食挑战
/宠物 管理 公告 展示会 首次展示会
```

Reset and delete commands must include the confirmation word in the same command:

```text
/宠物 管理 重置 @测试账号 确认
/宠物 管理 删除 @测试账号 确认
```

Use a dedicated test account for destructive administration commands.

## ✅ 8. Check scheduled tasks

Keep the Bot running. Monitor logs and data:

| Time | Expected task |
| --- | --- |
| Next minute | Attribute decreases and pet show settlement |
| Next 5-minute boundary | Expired trade listing cleanup |
| Next day at 00:00 | Daily count reset and pet age increase |
| Monday at 10:00 | Weekly ranking rewards and title settlement |

Before production data checks, use SQLite `Connection.backup()` to make a consistent backup. The backup includes committed WAL data. Use the backup copy and an isolated data directory for checks.

## 📌 9. Automated regression checks

```bash
python -m ruff check plugins/qingpet tests/plugins/qingpet
python -m mypy plugins/qingpet
python -m pytest -q tests/plugins -k qingpet -n 2
```

After all checks pass, use `/宠物 帮助` in the target QQ client. Check mobile pagination and message length.
