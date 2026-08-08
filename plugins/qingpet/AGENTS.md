# 🛠️ QingPet 维护约定

本文件适用于 `plugins/qingpet/` 下的代码、测试和文档修改。功能现状见 [README.md](README.md)，首次部署验收见 [QUICKSTART.md](QUICKSTART.md)。

---

## 📚 权威来源

修改前依次核对：

1. `plugin.json`：命令、别名、权限、场景与调度契约；
2. `main.py`：插件生命周期、路由和定时入口；
3. `commands/`：参数解析与用户消息；
4. `models/`：领域数据结构；
5. `services/`：事务、仓储和业务规则；
6. `utils/constants.py`：冷却、次数、成长、商品和活动常量；
7. `tests/plugins/test_qingpet*.py`：行为与回归契约。

文档描述当前代码和 Manifest。新增能力时，在同一个变更中同步命令元数据、帮助、用户文档与测试。

---

## 🔐 模块边界

| 目录或文件 | 职责 |
| --- | --- |
| `main.py` | 初始化、关闭、命令路由、权限上下文和调度入口 |
| `commands/basic_commands.py` | 领养与基础照料 |
| `commands/advanced_commands.py` | 成长、道具和进阶行为 |
| `commands/new_commands.py` | 社交、玩法、任务、交易和展示会 |
| `commands/admin_commands.py` | 群配置、用户管理、活动和审计 |
| `models/` | 用户、宠物、道具、背包、群配置和日志模型 |
| `services/database.py` | Repository 组合入口与连接生命周期 |
| `services/database_*` | Schema、身份、社区、动作、调度和共享存储能力 |
| 其他 `services/` | 宠物、用户、道具、社交、经济和管理用例 |
| `utils/` | 路由、校验、格式化、时间和常量 |

命令模块负责消费输入和组织回复；领域 service 负责业务规则；repository 负责数据库读写。跨模块用例通过 service 方法组合。

---

## 🔐 数据安全

生产数据库路径为：

```text
data/qingpet/qingpet/qingpet.db
```

开发与测试遵循以下约定：

- 测试使用 `tmp_path` 创建独立 SQLite 数据库；
- 数据迁移先复制数据库、WAL 和 SHM 文件，再在副本上验证；
- 清理脚本的目标限定为测试目录、缓存目录或显式传入路径；
- Schema 变更采用兼容 migration，并添加已有数据库升级测试；
- 资产与状态修复通过事务 service 和审计记录执行；
- 生产数据读取由用户明确授权的诊断或迁移任务触发。

---

## 🔐 租户与权限

业务数据的租户键是 `group_id`，用户键是 `user_id`。所有查询、更新、排行、任务、活动和交易都需要携带群作用域。

管理员身份来自 Core 已认证事件，适用角色包括群管理员、群主和 Bot 管理员。管理命令在 Manifest 与运行时入口同时声明权限和群场景。重置、删除等破坏性操作使用同条命令确认词，并写入管理审计日志。

---

## 📌 事务与幂等

以下用例在一个数据库事务中完成全部资产和状态变化：

- 喂食、清洁、玩耍、训练、探索和治疗；
- 互访、送礼和小游戏；
- 任务、活动和展示会奖励；
- 交易挂单、购买、撤单与到期退还；
- 每日重置、周结算和资产快照。

来自 OneBot 的可重投操作使用稳定请求身份。数据库唯一约束或幂等记录保存首次结果，后续同身份调用重放结果。事务测试应覆盖成功、业务拒绝、异常回滚、并发竞争和重复投递。

---

## 💾 资产一致性

`users` 表保存金币和友情点当前余额，`asset_ledger` 保存每次受控变化。资产 service 在同一事务中更新余额和账本。群统计负责计算余额与账本聚合差异，并把差异交给管理员排查流程。

新增资产入口时，请定义：

- 变更方向、金额与资产类型；
- 操作类型和关联对象；
- 请求身份与幂等行为；
- 每日上限、冷却与收益衰减；
- 失败回滚和并发测试；
- 管理统计与审计字段。

---

## ⏰ 时间与调度

时间计算通过 `services/database_clock.py` 和 `utils/time.py` 进入数据库事务。测试使用可控时钟。

调度入口保持轻量，实际结算由 `database_scheduler.py` 和领域 service 完成：

| 入口 | 数据动作 |
| --- | --- |
| `scheduled_decay` | 属性衰减、旅行状态与限频清理 |
| `scheduled_daily_reset` | 每日计数、年龄与日期边界 |
| `scheduled_trade_expiry` | 到期订单与托管库存 |
| `scheduled_pet_show_settlement` | 展示会截止与奖励 |
| `scheduled_weekly_activity` | 周排行、奖励和称号 |

调度实现应支持重复 tick、跨重启恢复、单实例重入和多事务竞争。截止时间在事务内重新读取。

---

## ⌨️ 命令与帮助

新增或调整命令时：

1. 在 `plugin.json` 更新规范名、别名、用法、示例、错误示例、权限和场景；
2. 在相应 command 模块实现完整参数消费；
3. 为参数长度、数量、控制字符、QQ 主体和群作用域设置边界；
4. 把命令放入 `基础`、`进阶`、`道具`、`社交`、`玩法` 或 `管理` 帮助类别；
5. 保持手机端摘要短小，并通过分类帮助展示细节；
6. 更新 README 和 Manifest 文档契约测试。

公开错误信息使用稳定中文提示。日志记录操作类型、长度、数量、状态和摘要，用户文本与敏感值留在日志边界之外。

---

## 🛠️ Schema 修改

1. 在 `database_schema.py` 定义新表、列、索引或约束；
2. 为已有数据库增加幂等 migration；
3. 在对应 repository 文件实现读写；
4. 在 `database.py` 组合所需 mixin；
5. 更新关闭、连接登记和事务测试；
6. 添加新库创建、旧库升级和并发访问测试；
7. 更新 README 的数据边界。

Repository 拆分保持一个 `Database` 组合对象和一个 SQLite 文件。

---

## ✅ 验证要求

先运行与修改模块直接相关的测试，再运行 QingPet 全集：

```bash
python -m ruff check plugins/qingpet tests/plugins/test_qingpet*.py
python -m mypy plugins/qingpet
python -m pytest -q tests/plugins -k qingpet -n 2
```

高风险改动还需要覆盖：

- 真实 SQLite 事务与约束；
- 两个并发连接的竞争；
- 定时任务重复执行；
- OneBot 消息重投；
- 管理员和普通成员权限；
- 群间租户隔离；
- 数据库关闭与插件重载；
- 手机端帮助长度和目录结构。

---

## ✅ 提交前检查

- `plugin.json` 与路由实现一致；
- 公开帮助与 README 一致；
- 新字段具有 migration、序列化和验证；
- 资产变化具有账本、幂等与回滚；
- 定时状态具有截止时间和重复执行测试；
- 测试数据位于临时目录；
- Ruff、mypy 和 QingPet pytest 全部通过；
- `git diff --check` 通过。
