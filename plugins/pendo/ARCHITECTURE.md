# Pendo 插件架构与代码结构

本文件把 Pendo 的运行时结构、模块边界、数据模型和扩展方式梳理在一起。它与 `plugins/pendo/README.md` 共同组成 Pendo 的正式文档入口。README 面向使用和部署，ARCHITECTURE 面向维护和二次开发。

## 定位

Pendo 是 XiaoQing 的个人时间与信息管理中枢，当前功能边界如下。

- `event + reminders`: 日程、重复日程、多节点日程、提醒确认与延后。
- `note`: 笔记、标签、分类、条目引用、关联条目。
- `diary`: 多篇日记、模板回答、心情、天气、位置、收藏。
- `todo/task`: 计划日期、硬截止、提醒、优先级、open/done/cancelled 状态。
- `ledger`: 账目、账户、转账、商户、金额分存储、统计。

核心设计原则如下。

- 单一条目管线：大部分业务对象都落在 `items` 表，按 `type` 区分。
- 明确产品语义：字段只保留前后端和 CLI 都能闭环使用的语义。
- 类型统一校验：CLI、Web API、Bundle 导入都应走 `utils/validators.py` 的类型归一化。
- 多用户隔离：所有读写都以 `owner_id` 过滤。
- Web 与 CLI 共用后端服务：Web 负责高密度管理，CLI 负责快速录入和提醒操作。

## 正式文档边界

Pendo 插件目录中可能存在测试提示和运行报告等执行产物。当前维护时只把以下两个文件当作长期文档入口。

- `README.md`: 用户手册，覆盖命令、Web 控制台、迁移、配置和排障。
- `ARCHITECTURE.md`: 工程手册，覆盖运行时分层、数据库、服务、Web API、调度和测试边界。

测试任务文档和 test report 属于执行记录，不参与架构说明。

## 目录结构

```text
plugins/pendo/
├── main.py                      # 插件入口、生命周期、命令注册、scheduled_* 入口
├── config.py                    # 配置常量、提醒策略、日记模板
├── plugin.json                  # 插件清单和定时任务
├── README.md                    # 用户使用说明
├── ARCHITECTURE.md              # 架构文档
├── requirements.txt
│
├── core/
│   ├── router.py                # CommandRouter、别名解析、帮助入口
│   ├── runtime.py               # runtime 状态、服务缓存
│   ├── types.py                 # PendoContext / PendoServices 等类型
│   └── exceptions.py            # 业务异常
│
├── models/
│   ├── item.py                  # Item / EventItem / TaskItem / NoteItem / DiaryItem / LedgerItem
│   ├── types.py                 # 通用命令结果类型
│   └── constants.py             # ItemFields 字段常量
│
├── handlers/
│   ├── event.py                 # 日程命令；单次/多节点/重复日程的 leaf 操作
│   ├── event_support.py         # 日程展示、提醒规则、集合辅助逻辑
│   ├── task.py                  # 待办命令
│   ├── note.py                  # 笔记命令
│   ├── diary.py                 # 日记命令和模板会话
│   ├── ledger.py                # 账本命令、快捷记账和交互式记账引导
│   ├── search.py                # 全文搜索命令
│   └── web.py                   # Web 服务启动、Token 命令
│
├── services/
│   ├── db.py                    # SQLite、schema、缓存、CRUD、审计
│   ├── event_graph.py           # event_collections 与 leaf events 的组装/拆解
│   ├── reminder.py              # 提醒计算、发送、日志去重
│   ├── ai_parser.py             # 日程自然语言解析入口
│   ├── rule_parser.py           # 规则解析回退
│   ├── exporter.py              # CLI Markdown 档案导出
│   └── llm_client.py            # LLM 客户端封装
│
├── commands/
│   ├── operations.py            # confirm / snooze / undo
│   ├── scheduled.py             # 每分钟提醒、日报、日记提醒、待办迁移、财务总结
│   ├── session.py               # 多轮会话恢复
│   └── settings.py              # 用户设置命令
│
├── utils/
│   ├── db_ops.py                # DbOpsMixin 和 Database singleton
│   ├── error_handlers.py        # 统一错误处理
│   ├── formatters.py            # CLI 消息格式化
│   ├── session_utils.py         # 多轮会话工具
│   ├── settings_utils.py        # 用户设置解析
│   ├── time_utils.py            # 时间范围、时区、提醒时间解析
│   └── validators.py            # event/note/diary/task/ledger 统一归一化
│
├── scripts/
│   ├── migrate_pendo_redesign.py # 一次性迁移：event/note/diary/task/ledger 新结构
│   ├── migrate_event_graph.py    # 事件图迁移辅助
│   └── *.py.old                  # 已停用的历史脚本
│
├── data/
│   ├── pendo.db                 # SQLite 数据库
│   └── web_token_secret.txt     # Web Token 签名密钥
│
└── web/
    ├── server.py                # FastAPI app 生命周期
    ├── auth.py                  # Web Token 生成和校验
    ├── deps.py                  # owner_id / Database 依赖
    ├── api/                     # FastAPI routers
    ├── analytics/               # Dashboard/Stats 聚合逻辑
    ├── services/                # Bundle 导入导出、demo 数据
    ├── scriptable/              # iOS Scriptable 小组件脚本
    └── static/                  # SPA 前端
```

重构方案文档不再放在插件目录下，统一放到 `docs/plans/`，例如：

- `docs/plans/2026-04-24-pendo-event-redesign.md.old`
- `docs/plans/2026-04-25-pendo-note-redesign.md`
- `docs/plans/2026-04-29-pendo-diary-redesign.md`
- `docs/plans/2026-04-29-pendo-task-redesign.md`
- `docs/plans/2026-04-29-pendo-ledger-redesign.md`

## 运行时分层

```text
XiaoQing Plugin Runtime
        |
        v
plugins/pendo/main.py
  - init / cleanup
  - handle(command, args, event, context)
  - scheduled_* handlers
  - service cache bootstrap
        |
        v
core/router.py CommandRouter
  - top-level alias resolution
  - help fallback
  - dispatch to handlers
        |
        +-------------------------------+
        |                               |
        v                               v
handlers/*.py                    web/api/*.py
CLI command workflows            FastAPI CRUD / analytics / transfer
        |                               |
        +---------------+---------------+
                        v
services/*.py + utils/validators.py
DB, reminders, parser, exporter, normalization
                        |
                        v
services/db.py SQLite schema
```

## 命令路由

`main.py::_build_command_router()` 注册当前 CLI 子命令：

- `event`
- `todo`
- `note`
- `diary`
- `ledger`
- `search`
- `export`
- `import`
- `settings`
- `confirm`
- `snooze`
- `undo`
- `web`

CLI 不再提供 Markdown 导入命令；聊天端只保留 `/pendo export <文件名> [范围] [类型]`。数据迁移、恢复、预览和冲突处理走 Web 端 Bundle 流程。

## 数据模型

### 通用 Item

`models/item.py` 中所有条目继承基础 `Item`：

- `id`, `type`, `title`, `content`
- `tags`, `category`
- `created_at`, `updated_at`
- `owner_id`, `context`
- `deleted`, `deleted_at`

### Event

日程以 leaf event 为可操作单元：

- 单次事件：一条 `items(type='event')`。
- 多节点日程：一个 `event_collections(kind='multi_node')` 包住多条 leaf event。
- 重复日程：一个 `event_collections(kind='recurring')` 包住展开后的 occurrence leaf event。

关键字段：

- `start_time`, `end_time`, `location`
- `reminder_rules`, `remind_times`
- `event_role`: `single` / `multi_node_child` / `recurring_occurrence`
- `event_collection_id`, `event_collection_kind`
- `event_index`, `event_node_key`
- `source_item_id`, `notes`

多节点和重复日程的整体标题、共享分类、共享提醒规则等保存在 `event_collections`；每个节点仍能像单次事件一样查、改、删和提醒。

### Task / Todo

待办是个人 GTD-lite 模型，不再把日期塞进 `category`：

- `plan_date`: 计划处理日期，决定今天/未来/收件箱视图。
- `deadline_at`: 真正硬截止时间。
- `remind_times`: 明确提醒时间。
- `priority`: 1-5。
- `status`: `open`, `done`, `cancelled`。
- `completed_at`, `cancelled_at`, `repeat_rule`。

历史 `due_time`, `estimate`, `subtasks`, `dependencies`, `progress` 只属于迁移输入，不属于新运行时模型。

### Note

笔记支持轻量知识管理：

- `tags`, `category`
- `references`: 结构化引用，记录引用类型、目标 ID 或外部描述。
- `related_items`: 由引用派生或显式维护的关联条目 ID。

Web、CLI、搜索、Bundle 导入导出都应保留 `references` 与 `related_items` 的一致性。

### Diary

日记支持同一天多篇：

- `diary_date`: 日记归属日期。
- `entry_time`: 具体记录时间，用于排序和回看。
- `mood`, `mood_score`
- `weather`, `location`
- `template_id`, `template_answers`
- `is_favorite`

CLI、Web 和导入路径都通过 `normalize_diary_fields()` 统一校验心情、分数、模板回答结构和时间字段。

### Ledger

账本以金额分为主存储：

- `amount_cents`: 正整数金额分，统计主字段。
- `amount`: 展示镜像，由 `amount_cents` 派生。
- `currency`: 默认 `CNY`。
- `transaction_type`: `expense`, `income`, `transfer`。
- `ledger_category`, `ledger_date`
- `account_name`: 来源账户。
- `counter_account_name`: 转账目标账户。
- `merchant`, `remark`

转账不是收入或支出，统计时和收支汇总分开处理。

## 数据库

`services/db.py` 管理 SQLite schema、连接、缓存和审计。主要表：

- `items`: 统一条目表。
- `event_collections`: 多节点/重复日程的集合头。
- `items_fts`: FTS5 全文搜索索引。
- `reminder_logs`: 提醒发送/确认日志。
- `operation_logs`: create/update/delete/export/import 审计。
- `user_settings`: 用户时区、静默时段、日报时间等。
- `transfer_logs`: Web Bundle 导入导出日志。
- `imported_bundles`: Bundle 幂等导入记录。

核心索引覆盖：

- `owner_id + type + deleted`
- event `start_time`
- task `plan_date`, `deadline_at`
- diary `diary_date`, `entry_time`
- ledger `ledger_date`, `transaction_type`
- event collection `owner_id/kind/time`

## 统一校验路径

`utils/validators.py` 是重构后的数据边界：

- `normalize_event_fields()`
- `normalize_task_fields()`
- `normalize_note_fields()`
- `normalize_diary_fields()`
- `normalize_ledger_fields()`

原则：

- 写入或更新条目必须先归一化再写库。
- Web API、CLI handler、Bundle 导入不要各自手写不同校验。
- 新结构不保留旧字段兜底；旧数据只允许在一次性迁移脚本里被读取和转换。

## Web 后端

`web/server.py` 创建 FastAPI 应用，路由在 `web/api/__init__.py` 聚合：

- `auth_routes.py`: Web Token 校验。
- `items.py`: 统一条目 CRUD、ledger quick stats。
- `events.py`: event collection/leaf 的 Web 专用接口。
- `dashboard.py`: 首页聚合。
- `search.py`: Web 搜索。
- `stats.py`: 各模块统计。
- `settings.py`: Web 设置。
- `config_routes.py`: 前端配置。
- `widget.py`: Scriptable 小组件数据。
- `transfer.py`: `.pendo.zip` Bundle 预览、导出、导入、日志。

`web/analytics/` 存放可测试的聚合逻辑，避免把复杂统计塞进路由函数：

- `dashboard_overview.py`
- `event_schedule.py`, `events_overview.py`
- `task_overview.py`
- `notes_overview.py`
- `diary_overview.py`
- `ledger_insights.py`

## Web 前端

`web/static/js` 是无构建 SPA：

- `app.js`, `router.js`, `store.js`, `api.js`
- `components/`: header、sidebar、modal、toast、pagination、自定义 select、通用 form。
- `pages/`: dashboard、events、tasks、notes、diary、ledger、search、stats、settings、transfer。
- `utils/`: date range 和 UI/format helpers。

前端原则：

- 用户输入进入 `innerHTML` 前必须转义。
- 复杂表单状态应先在页面层归一化，再交给 API 层二次校验。
- event/note/diary/task/ledger 页面展示字段要和模型字段一一对应，避免保留旧结构标签。

## 导出、导入与迁移

### CLI Markdown 档案导出

`services/exporter.py` 只负责聊天端 Markdown 档案导出：

```text
/pendo export 我的档案
/pendo export 工作回顾 last30d event,todo
/pendo export 账本快照 2026-03 ledger
```

导出的文件写到用户私有导出目录，并通过私聊发送。

### Web Bundle

Web 端 `transfer.py` 和 `web/services/transfer_bundle.py` 负责 `.pendo.zip`：

- 导出预览和下载。
- 导入 inspect/samples/execute。
- 冲突策略：跳过、覆盖、复制。
- duplicate 导入时重写条目 ID 和跨条目关系。
- `transfer_logs` 与 `imported_bundles` 记录审计和幂等信息。

### 一次性迁移脚本

当前结构重构迁移入口是：

```text
plugins/pendo/scripts/migrate_pendo_redesign.py
```

它负责把旧 event/note/diary/task/ledger 数据转换成新结构。迁移完成后，运行时代码不应该继续兼容旧字段语义。

## 定时任务

`plugin.json` 注册的 handler 都在 `main.py` 暴露，并转到 `commands/scheduled.py`：

- `scheduled`: 每分钟检查日程/待办提醒。
- `scheduled_daily_briefing`: 每分钟检查是否到用户本地日报时间。
- `scheduled_diary_reminder`: 每分钟检查是否到用户本地日记提醒时间。
- `scheduled_migrate_todos`: 每天 00:05 将昨日仍 open 的计划顺延到今天。
- `scheduled_weekly_finance_summary`: 每周日 21:00 财务总结。
- `scheduled_month_end_finance_summary`: 每月最后一天 21:00 财务总结。
- `scheduled_cleanup_demo_data`: 每 6 小时清理过期 Web demo 数据。

## 常见修改入口

| 任务 | 先读文件 |
|---|---|
| 改 CLI 命令 | `main.py`, `core/router.py`, `handlers/<module>.py` |
| 改 Web CRUD 字段 | `web/api/items.py`, `utils/validators.py`, `models/item.py`, `services/db.py` |
| 改日程集合/节点 | `handlers/event.py`, `handlers/event_support.py`, `services/event_graph.py`, `services/db.py`, `web/api/events.py` |
| 改提醒 | `services/reminder.py`, `commands/operations.py`, `commands/scheduled.py` |
| 改搜索 | `handlers/search.py`, `web/api/search.py`, `services/db.py` |
| 改 Dashboard/统计 | `web/analytics/*.py`, `web/api/dashboard.py`, `web/api/stats.py` |
| 改 Bundle 导入导出 | `web/api/transfer.py`, `web/services/transfer_bundle.py`, `web/services/bundle_import.py` |
| 改 CLI Markdown 导出 | `services/exporter.py` |
| 改前端页面 | `web/static/js/pages/<page>.js`, `web/static/css/app.css` |
| 改迁移 | `scripts/migrate_pendo_redesign.py`, `tests/plugins/test_pendo_event_migration.py` |

## 文档归属

- 插件内用户文档：`README.md`
- 插件内架构文档：`ARCHITECTURE.md`
- 全局插件手册：`docs/09-plugins.md`
- 重构/实现计划：`docs/plans/`
- 历史 superpowers 过程记录：`docs/superpowers/`

不要在用户手册里描述个人迁移临时文件或已停用脚本；需要记录迁移细节时写入 `docs/plans/`。

## 验证建议

文档或架构调整后至少运行以下命令。

```powershell
python -m pytest tests/plugins/test_pendo.py::TestPendoDocumentation -q
git diff --check -- plugins/pendo/ARCHITECTURE.md docs/plans
```

涉及运行时字段、API 或前端时，再补充对应 `tests/plugins/test_pendo_*` 用例。
