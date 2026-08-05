# Pendo - 个人时间与信息管理中枢

Pendo 是 XiaoQing 的个人管理插件，用同一套数据模型管理日程、待办、笔记、日记、账本和提醒。聊天命令适合随手记录、快速查询和处理提醒；Web 控制台适合集中浏览、编辑、统计和迁移数据；Scriptable 小组件适合把只读摘要放到 iPhone 主屏。

全部 Pendo 聊天命令仅允许私聊；群聊请求会在进入插件前由 Core 按 manifest 场景策略拒绝。

## 文档定位

`plugins/pendo/README.md` 是 Pendo 的使用手册，面向部署者和日常用户，描述命令、Web 控制台、数据模型、迁移和常见问题。`plugins/pendo/ARCHITECTURE.md` 是 Pendo 的工程架构文档，面向维护者，描述运行时分层、数据库、Web API、调度和扩展边界。

自动化回归测试位于 `tests/plugins/test_pendo*.py`；本地执行记录统一保存到项目级 `test_reports/runs/plugins/pendo/`，不是当前行为说明。Pendo 的维护入口只有本 README、`ARCHITECTURE.md`、`plugin.json` 和源码。

## 能力边界

| 模块   | 能力                                                           |
| ------ | -------------------------------------------------------------- |
| 日程   | 单次日程、重复日程、多节点日程、地点、备注、提醒规则、提醒确认 |
| 待办   | 计划日期、硬截止、优先级、状态、提醒、分类、标签               |
| 笔记   | 标题、正文、分类、标签、条目引用、关联条目                     |
| 日记   | 同日多篇、模板回答、心情、评分、天气、位置、收藏               |
| 账本   | 支出、收入、转账、账户、对方账户、商户、分类、金额分存储、统计 |
| 搜索   | 跨模块全文搜索和类型、状态、范围、分类、账本字段筛选           |
| Web    | FastAPI + 原生 JS SPA，覆盖 CRUD、统计、迁移、设置和 demo 会话 |
| 迁移   | 聊天端 Markdown 档案导出，Web 端 `.pendo.zip` Bundle 导入导出  |
| 小组件 | `/api/widget/summary` 只读摘要接口和 Scriptable 脚本           |

## 快速开始

```text
/pendo                      # 查看模块化帮助
/pendo help event            # 查看某个模块帮助
/pendo event add 明天9点组会，提前30分钟提醒
/pendo todo add              # 交互式添加待办，只问内容和计划日期
/pendo todo add 写周报 cat:工作 p:2 plan:2030-05-01
/pendo note add title:会议纪要 content 讨论迁移方案 cat:工作 #复盘
/pendo diary add 今天完成了项目复盘 mood:happy score:8
/pendo ledger add 35.5 午饭 cat:餐饮 account:微信
/pendo search 组会 type=event range=last30d
```

## 命令总览

### 日程

```text
/pendo event add <内容>
/pendo event list [today|tomorrow|week|month|year|YYYY-MM|start..end] [cat:分类] [#标签]
/pendo event view <id>
/pendo event edit <id> <内容>
/pendo event delete <id>
/pendo event reminders [id|范围]
/pendo event reminders list [范围]
/pendo event reminders set <id> <提醒描述>
/pendo event reminders delete <id> <all|today|future|提醒时间>
/pendo event reminders confirm <id> [today|future|all|提醒时间]
```

日程以 leaf event 为可操作单元。单次日程是一条 leaf；重复日程和多节点日程由 `event_collections` 组织多个 leaf。集合 ID 用来操作整体，leaf ID 用来操作单个 occurrence 或节点。

`e`、`calendar`、`日程`、`事件` 都是 `event` 的别名。

### 待办

```text
/pendo todo add
/pendo todo add <内容> [plan:YYYY-MM-DD] [deadline:YYYY-MM-DDTHH:MM] [remind:YYYY-MM-DDTHH:MM[,YYYY-MM-DDTHH:MM]] [cat:分类] [p:1-5] [#标签]
/pendo todo list [today|open|done|cancelled|overdue|upcoming|inbox|分类] [open|done|cancelled] [p:1-5] [all|page:n]
/pendo todo view <id>
/pendo todo done <id>
/pendo todo cancel <id>
/pendo todo undone <id>
/pendo todo edit <id> <内容> [plan:/deadline:/remind:/cat:/p:/#标签]
/pendo todo delete <id|cat:分类>
```

`task`、`t`、`待办`、`任务` 都是 `todo` 的别名。状态只有 `open`、`done`、`cancelled`。

`todo add` 后不带内容时会进入轻量多轮交互：先输入待办内容，再填写计划日期；计划日期可以输入 `0` 使用默认值，也可以输入 `无` 或 `不安排` 放入未安排。默认计划日期在用户本地时间 20:00 前是今天，20:00 起是明天。截止、提醒、分类、优先级和标签不在默认交互中逐项询问，请用带内容的一行快捷添加或 `todo edit` 设置。带内容时仍按一行快捷添加解析。

### 笔记

```text
/pendo note add <内容> [cat:分类] [#标签] [ref:条目ID]
/pendo note add title:<标题> content <正文> [cat:分类] [#标签]
/pendo note list [分类名|cat:分类] [#标签] [since:范围] [all|page:n]
/pendo note view <id>
/pendo note edit <id> <新内容> [cat:分类] [#标签]
/pendo note append <id> <追加内容>
/pendo note tag <id> #标签
/pendo note untag <id> #标签
/pendo note link <id> <关联条目ID>
/pendo note delete <id|cat:分类>
```

笔记的 `references` 和 `related_items` 会在 CLI、Web、搜索、Bundle 导入导出中保持一致。

`n`、`idea`、`笔记`、`想法`、`灵感` 都是 `note` 的别名。

### 日记

```text
/pendo diary add [日期] <内容> [weather:天气] [location:地点] [mood:happy] [score:1-10] [tags:a,b] [favorite:true]
/pendo diary template [编号|名称|模板ID]
/pendo diary list [范围] [mood:心情] [cat:分类] [#标签]
/pendo diary view [日期|ID]
/pendo diary delete <日期|ID>
```

同一天可以写多篇日记。`diary_date` 表示归属日期，`entry_time` 表示具体记录时间；模板回答结构化保存在 `template_answers`。

`d`、`journal`、`日记` 都是 `diary` 的别名。

### 账本

```text
/pendo ledger add
/pendo ledger add <金额> <描述> [cat:分类] [in|out|transfer|type:expense/income/transfer] [account:账户] [to:账户] [merchant:商户] [date:YYYY-MM-DD] [remark:备注]
/pendo ledger quick <金额> <描述> [cat:分类] [in|out|transfer|type:expense/income/transfer] [account:账户] [to:账户] [merchant:商户] [date:YYYY-MM-DD] [remark:备注]
/pendo ledger list [范围] [type:expense/income/transfer] [account:账户] [to:账户] [merchant:商户] [cat:分类] [amount:min..max] [all|page:n] [ex]
/pendo ledger view <id>
/pendo ledger edit <id> <字段:值>...
/pendo ledger delete <id>
/pendo ledger summary [范围]
```

`ledger add` 可以直接带一条记录，例如 `/pendo ledger add 28 午饭 cat:餐饮 account:微信`；只发送 `/pendo ledger add` 时会进入多轮交互：金额和描述需要手动输入，交易类型、账户和分类可以输入编号选择。未填写的普通字段使用默认值：支出、其他分类、现金账户、今天。

账本以 `amount_cents` 为统计主字段，`amount` 是展示镜像。转账使用 `transaction_type=transfer`，统计时和收入、支出分开处理。

`bill`、`finance`、`记账`、`账单` 都是 `ledger` 的别名。

### 搜索、提醒和设置

```text
/pendo search <关键词> [#标签|tag=] [type=event/task/note/diary/ledger] [range=] [status=] [category=] [transaction_type=] [account=] [merchant=]
/pendo confirm <id>
/pendo snooze <id> <10m|1h|19:00>
/pendo undo [分钟]
/pendo settings
/pendo settings reminder on/off
/pendo settings timezone Asia/Shanghai
/pendo settings quiet_hours 23:00-07:00
/pendo settings daily_report 08:30
/pendo settings daily_briefing on/off
/pendo settings diary_remind 21:30
/pendo settings ai_consent on/off
```

`config` 是 `settings` 的别名；`undo` 的删除与编辑撤销窗口统一为 5 分钟。可显式传入 `1..5` 分钟来缩小查找范围，不能扩大快照保留窗口。

`ai_consent` 默认关闭，只控制日记正文是否允许发送给已配置的外部 AI 做分析；关闭时使用本地规则，可随时执行 `/pendo settings ai_consent off` 撤回。

## Web 控制台

### 启动和登录

```text
/pendo web start
/pendo web status
/pendo web stop
/pendo web token
/pendo web widget-token
/pendo web widget-revoke
```

默认监听 `http://127.0.0.1:12001`。Web 运行参数统一配置在 `config/config.json`；保存后配置热重载会原子发布同一代设置：

```json
{
  "plugins": {
    "pendo": {
      "web_enabled": true,
      "web_host": "127.0.0.1",
      "web_port": 12003,
      "web_session_cookie_secure": false,
      "web_demo_enabled": false
    }
  }
}
```

Web 登录不需要账号密码。执行 `/pendo web token` 后，私聊会只发送可直接复制的一次性登录码；登录码 7 天内有效且仅可使用一次，在 Pendo Web 登录页粘贴后会建立浏览器会话。`PENDO_WEB_TOKEN_SECRET` 可用于保持多实例或重启后的签名密钥稳定。

默认地址只监听本机。若把 `web_host` 改为非 loopback 地址，服务会要求同时配置 `"web_session_cookie_secure": true`，并且必须部署在 TLS 反向代理之后；不要把明文 HTTP 登录入口直接暴露到网络。`web_demo_enabled` 只应用于受控的本地演示环境。布尔值必须使用 JSON 布尔值，`web_port` 必须是 `1..65535` 的整数；非法配置会被拒绝，不会覆盖上一代有效设置。

### 页面

| 页面      | 说明                                               |
| --------- | -------------------------------------------------- |
| Dashboard | 日程、待办、账本、笔记等核心摘要                   |
| Events    | 单次、重复、多节点日程和提醒                       |
| Tasks     | 待办看板、筛选、状态切换                           |
| Ledger    | 快速记账、筛选、账户、收支和转账                   |
| Notes     | 笔记、分类、标签、关联条目                         |
| Diary     | 日记时间线、心情、模板字段                         |
| Search    | 跨模块搜索                                         |
| Stats     | 活跃度、任务、账本、日记、事件等统计               |
| Settings  | 用户设置                                           |
| Transfer  | `.pendo.zip` Bundle 导入导出、预览、冲突策略和日志 |

Web 后端位于 `plugins/pendo/web/server.py`，使用 FastAPI + uvicorn。API 统一挂载在 `/api`，静态前端位于 `plugins/pendo/web/static`。

### Scriptable 小组件

Pendo 提供只读 widget API：

```text
GET /api/widget/summary?section=tasks|ledger|notes|all|auto
Authorization: Bearer <widget_token>
```

脚本路径：

```text
plugins/pendo/web/scriptable/pendo_widget.js
```

脚本中只保留 `BASE_URL`，不保留 Token 常量。使用前将 `BASE_URL` 改成自己的
Pendo Web 地址，然后在 Scriptable App 内直接运行一次脚本，将
`/pendo web widget-token` 私聊生成的只读 Token 存入 iOS Keychain。Token 默认
365 天过期；可用 `/pendo web widget-revoke` 吊销自己所有未过期的组件令牌。
详细说明见 `docs/pendo-scriptable-widget.md`。

## 导入、导出和迁移

### 聊天端 Markdown 档案

```text
/pendo export 我的档案
/pendo export 工作回顾 last30d event,todo
/pendo export 账本快照 2026-03 ledger
/pendo export 本月随笔 month note,diary
```

聊天端只负责 Markdown 档案导出，结果通过 OneBot 私聊文件消息发送。

### Web 端 Pendo Bundle

Web Transfer 页负责 `.pendo.zip` 数据包：

- 导出预览和下载
- 导入 inspect、samples、execute
- 冲突策略：跳过、覆盖、复制
- 选择“生成副本”时重写条目 ID 和跨条目关系
- `transfer_logs` 和 `imported_bundles` 记录审计和幂等信息

`/pendo import` 只显示 Web 迁移入口和支持格式，不读取服务器本地路径。聊天端不再提供 Markdown 导入；跨设备迁移和恢复请使用 Web Bundle 流程。

## 数据模型

| 类型     | 关键字段                                                                                                                                |
| -------- | --------------------------------------------------------------------------------------------------------------------------------------- |
| `event`  | `start_time`、`end_time`、`timezone`、`location`、`participants`、`event_role`、`event_collection_id`、`reminder_rules`、`remind_times` |
| `task`   | `plan_date`、`deadline_at`、`priority`、`status`、`reminder_rules`、`remind_times`、`completed_at`、`cancelled_at`                      |
| `note`   | `tags`、`category`、`references`、`related_items`、`last_viewed`                                                                        |
| `diary`  | `diary_date`、`entry_time`、`mood`、`mood_score`、`template_answers`、`is_favorite`                                                     |
| `ledger` | `amount_cents`、`transaction_type`、`ledger_category`、`ledger_date`、`account_name`、`counter_account_name`、`merchant`                |

主要表：

- `items`
- `schema_migrations`
- `event_collections`
- `items_fts`
- `reminder_logs`
- `scheduled_delivery_outbox`
- `operation_logs`（撤销会标记来源日志并追加反向事件，不会消费后物理删除）
- `user_settings`
- `transfer_logs`
- `imported_bundles`

字段写入边界集中在 `plugins/pendo/utils/validators.py`。CLI handler、Web API、Bundle 导入都应复用这些归一化函数。

## 目录结构

```text
plugins/pendo/
├── main.py                      # 插件入口、生命周期、命令注册、scheduled_* 入口
├── config.py                    # 配置常量、提醒策略、日记模板
├── plugin.json                  # 插件清单和定时任务
├── core/                        # 路由、runtime、类型、异常
├── models/                      # Item / EventItem / TaskItem / NoteItem / DiaryItem / LedgerItem
├── handlers/                    # event / task / note / diary / ledger / search / web 命令处理
├── services/                    # SQLite、事件图、提醒、解析、导出、LLM
├── commands/                    # confirm / snooze / undo / settings / scheduled / session
├── utils/                       # 校验、格式化、时间、设置、数据库辅助
├── scripts/                     # 一次性迁移脚本和共享迁移工具
├── data/                        # 本地运行时数据，不应提交
└── web/
    ├── server.py                # FastAPI app 和 uvicorn 生命周期
    ├── auth.py                  # Token 生成和校验
    ├── deps.py                  # owner_id / Database 依赖
    ├── api/                     # REST API routers
    ├── analytics/               # Dashboard / Stats 聚合逻辑
    ├── services/                # Bundle 导入导出和 demo 数据
    ├── scriptable/              # Scriptable 小组件脚本
    └── static/                  # 无构建 SPA
```

## 定时任务

| 任务                              | 说明                                  |
| --------------------------------- | ------------------------------------- |
| `pendo_reminders`                 | 每分钟检查日程/待办提醒               |
| `pendo_daily_briefing`            | 每分钟检查是否到用户本地日报时间      |
| `pendo_diary_reminder`            | 每分钟检查是否到用户本地日记提醒时间  |
| `pendo_migrate_todos`             | 每天 00:05 顺延昨日仍 open 的计划待办 |
| `pendo_prune_operation_logs`      | 每天 00:15 清理过期日志，并仅擦除已越过 5 分钟窗口的撤销快照 |
| `pendo_weekly_finance_summary`    | 每周日 21:00 财务总结                 |
| `pendo_month_end_finance_summary` | 每月最后一天 21:00 财务总结           |
| `pendo_cleanup_demo_data`         | 每 6 小时清理过期 Web demo 数据       |

## 安装和配置

仓库根目录的 `requirements.txt` 已包含 Pendo Web 依赖：

```bash
python -m pip install -r requirements.txt
```

日程自然语言解析，以及用户明确同意后的日记情绪分析，都可以使用 LLM。Pendo 只声明统一的 `parse` route，模型 profile 与连接信息复用项目级 `config.ai`：

```json
{
  "plugins": {
    "pendo": {
      "ai": {
        "routes": {
          "parse": {
            "models": ["deepseek-flash", "glm-5.2"],
            "temperature": 0.3,
            "max_tokens": 1000,
            "timeout_seconds": 30,
            "total_timeout_seconds": 60,
            "max_retry": 1
          }
        }
      }
    }
  }
}
```

API Key 统一位于 `config/secrets.json -> ai.providers.<provider>.api_key`，Pendo 插件本身读取不到密钥。route 未配置、请求失败或用户未同意共享日记正文时，对应功能会回退到本地规则，日程和日记仍可正常使用。

## 常见问题

**Pendo Web 打不开**

1. 先执行 `/pendo web status`。
2. 确认环境安装了 `fastapi`、`uvicorn` 和 `PyJWT`（均包含在根目录 `requirements.txt` 中）。
3. Windows 上如果默认端口绑定失败且 `netstat -ano` 看不到占用，优先修改 `config/config.json` 中的 `plugins.pendo.web_port`。
4. 登录前先执行 `/pendo web token` 获取有效 token。

**提醒没有收到**

1. 检查 `/pendo settings` 中提醒是否开启。
2. 检查是否处在静默时段。
3. 用 `/pendo event reminders <id>` 查看提醒是否存在。

**如何备份数据**

1. Web Transfer 页导出 `.pendo.zip`。
2. 聊天端 `/pendo export <文件名>` 导出 Markdown 档案。
3. 停止服务后复制 `data/pendo/pendo.db`。

## 测试

常用验证命令如下。

```powershell
python -m compileall -q plugins/pendo
node --check plugins/pendo/web/scriptable/pendo_widget.js
$files = Get-ChildItem -LiteralPath 'tests/plugins' -Filter 'test_pendo*.py' | Sort-Object Name | ForEach-Object { $_.FullName }
$serverFiles = Get-ChildItem -LiteralPath 'tests' -Filter 'test_server*.py' | Sort-Object Name | ForEach-Object { $_.FullName }
python -m pytest @files @serverFiles
```

完整验证以仓库内 `tests/plugins/test_pendo*.py` 和 `tests/test_server*.py` 为准；本地黑盒执行记录可写入已忽略的项目级 `test_reports/runs/plugins/pendo/`。

## 相关文档

- `plugins/pendo/ARCHITECTURE.md`
- `docs/09-plugins.md`
- `docs/pendo-scriptable-widget.md`
