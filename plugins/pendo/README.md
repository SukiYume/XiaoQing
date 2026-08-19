# 📅 Pendo

Pendo 是 XiaoQing 的个人时间与信息管理插件，统一管理日程、待办、笔记、日记、账本、提醒、搜索和数据导入导出。聊天命令适合快速记录与查询，Web 控制台适合集中编辑、统计和批量传输，Scriptable 小组件适合展示 iPhone 主屏摘要。

全部 Pendo 命令限定为私聊场景。

---

## 🚀 快速开始

```text
/pendo
/pendo help event
/pendo event add 明天9点组会，提前30分钟提醒
/pendo todo add 写周报 plan:2030-05-01 cat:工作 p:2
/pendo note add title:会议纪要 content 讨论部署方案 cat:工作 #复盘
/pendo diary add 今天完成了项目复盘 mood:happy score:8
/pendo ledger quick 35.5 午饭 cat:餐饮 account:微信
/pendo search 组会 type=event range=last30d
```

Manifest 还提供 `/日程`、`/待办` 和 `/日记` 三个私聊快捷入口，它们分别转发到 `event`、`todo` 和 `diary` 模块。

---

## ⌨️ 帮助入口

| 用法 | 内容 |
| --- | --- |
| `/pendo` | 模块目录 |
| `/pendo help <模块>` | 指定模块的命令与示例 |
| `/help pendo` | Core 生成的 Pendo 插件目录 |

模块名包括 `event`、`todo`、`note`、`diary`、`ledger`、`search`、`settings`、`web`、`export` 和 `import`。

---

## 📅 日程

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

`e`、`calendar`、`日程`、`事件` 是 `event` 的别名。单次日程对应一个 leaf；重复日程和多节点日程由 `event_collections` 组织多个 leaf。集合 ID 面向整体操作，leaf ID 面向 occurrence 或单个节点。

---

## 📅 待办

```text
/pendo todo add
/pendo todo add <内容> [plan:YYYY-MM-DD] [deadline:YYYY-MM-DDTHH:MM] [remind:时间列表] [cat:分类] [p:1-5] [#标签]
/pendo todo list [today|open|done|cancelled|overdue|upcoming|inbox|分类] [open|done|cancelled] [p:1-5] [all|page:n]
/pendo todo view <id>
/pendo todo done <id>
/pendo todo cancel <id>
/pendo todo undone <id>
/pendo todo edit <id> <内容与字段>
/pendo todo delete <id|cat:分类>
```

`task`、`t`、`待办`、`任务` 是 `todo` 的别名。状态值为 `open`、`done` 和 `cancelled`。

空内容的 `todo add` 会启动轻量会话，依次收集待办内容与计划日期。计划日期接受具体日期、`0` 默认值或 `无`。默认日期在用户本地时间 20:00 前取当天，20:00 起取次日。截止、提醒、分类、优先级和标签可通过一行添加或 `todo edit` 设置。

---

## 📅 笔记

```text
/pendo note add <内容> [cat:分类] [#标签] [ref:条目ID]
/pendo note add title:<标题> content <正文> [cat:分类] [#标签]
/pendo note list [分类|cat:分类] [#标签] [since:范围] [all|page:n]
/pendo note view <id>
/pendo note edit <id> <新内容与字段>
/pendo note append <id> <追加内容>
/pendo note tag <id> #标签
/pendo note untag <id> #标签
/pendo note link <id> <关联条目ID>
/pendo note delete <id|cat:分类>
```

`n`、`idea`、`笔记`、`想法`、`灵感` 是 `note` 的别名。`references` 与 `related_items` 在聊天命令、Web、搜索和 Bundle 中使用同一模型。

---

## 📅 日记

```text
/pendo diary add [日期] <内容> [weather:天气] [location:地点] [mood:心情] [score:1-10] [tags:a,b] [favorite:true]
/pendo diary template [编号|名称|模板ID]
/pendo diary list [范围] [mood:心情] [cat:分类] [#标签]
/pendo diary view [日期|ID]
/pendo diary delete <日期|ID>
```

`d`、`journal`、`日记` 是 `diary` 的别名。同一天可以保存多篇日记；`diary_date` 表示归属日期，`entry_time` 表示记录时刻，模板回答保存在 `template_answers`。

---

## 📅 账本

```text
/pendo ledger add
/pendo ledger add <金额> <描述> [cat:分类] [in|out|transfer|type:] [account:账户] [to:账户] [merchant:商户] [date:日期] [remark:备注]
/pendo ledger quick <金额> <描述> [字段...]
/pendo ledger list [范围] [type:] [account:] [to:] [merchant:] [cat:] [amount:min..max] [all|page:n] [ex]
/pendo ledger view <id>
/pendo ledger edit <id> <字段:值>...
/pendo ledger delete <id>
/pendo ledger summary [范围]
```

`bill`、`finance`、`记账`、`账单` 是 `ledger` 的别名。空参数的 `ledger add` 会启动多轮会话；金额和描述由用户填写，交易类型、账户和分类可按编号选择。`amount_cents` 是统计主字段，`amount` 是展示镜像。转账使用 `transaction_type=transfer`。

---

## ⏰ 搜索、提醒和设置

```text
/pendo search <关键词> [#标签|tag=] [type=] [range=] [status=] [category=] [transaction_type=] [account=] [merchant=]
/pendo confirm <id>
/pendo snooze <id> <10m|1h|19:00>
/pendo undo [1..5]
/pendo settings
/pendo settings reminder on|off
/pendo settings timezone Asia/Shanghai
/pendo settings quiet_hours 23:00-07:00
/pendo settings daily_report 08:30
/pendo settings daily_briefing on|off
/pendo settings diary_remind 21:30
/pendo settings ai_consent on|off
```

`config` 和 `setting` 是 `settings` 的别名。操作快照保留 5 分钟，`undo` 可将查询窗口缩小到 1～5 分钟。`ai_consent` 控制日记正文向已配置外部 AI 的发送授权，默认值为 `off`；本地规则始终可用。

日程与待办提醒通过 OneBot 私聊发送给条目所有者。条目写入与编辑会同步生成以 `fire_at_utc` 查询的提醒队列行，提醒键使用秒级 UTC。

Web 控制台的日程详情按提醒点提供“提前确认”和“重新开启”操作。按钮仅在提醒触发前可用；提前确认会停止该提醒的首次与重复投递，重新开启会恢复原触发时刻的待发送状态。

---

## 🌐 Web 控制台

### 启动与状态

```text
/pendo web start
/pendo web status
/pendo web stop
/pendo web token
/pendo web widget-token
/pendo web widget-revoke
```

运行参数位于 `config/config.json`：

```json
{
  "plugins": {
    "pendo": {
      "web_enabled": true,
      "web_host": "127.0.0.1",
      "web_port": 12001,
      "web_session_cookie_secure": false,
      "web_demo_enabled": false
    }
  }
}
```

默认地址为 `http://127.0.0.1:12001`。局域网或公网监听需要 TLS 反向代理和 `web_session_cookie_secure: true`。Demo 模式适用于受控的本地演示环境。

TLS 反向代理场景使用以下 Cookie 设置：

```json
{
  "plugins": {
    "pendo": {
      "web_session_cookie_secure": true
    }
  }
}
```

### 登录 Code 与浏览器会话

`/pendo web token` 返回一次性登录码（Code）。Code 有效期为 7 天，成功兑换后创建 7 天 HttpOnly Cookie 会话。Code 摘要与浏览器会话摘要均保存在现有 `pendo.db`，Bot 与 Web 服务重启会继续使用有效记录。

浏览器会话支持设备列表、指定设备撤销和当前会话退出。服务端保存摘要、所有者、设备标识、创建时间、到期时间与最近使用时间。

### Widget Token

`/pendo web widget-token` 返回 365 天 Bearer JWT，权限范围限定为 `/api/widget/*` 的 GET 接口。`/pendo web widget-revoke` 撤销当前用户的有效 Widget Token。浏览器会话与 Widget Token 共享秒级时间计算，分别使用独立凭据类型和权限范围。`PENDO_WEB_TOKEN_SECRET` 用于保持 Widget JWT 签名密钥稳定。

Widget 接口：

```text
GET /api/widget/summary?section=tasks|ledger|notes|all|auto
GET /api/widget/calendar?start_date=YYYY-MM-DD&end_date=YYYY-MM-DD
Authorization: Bearer <widget_token>
```

摘要接口返回最多 5 条近期日程。日历接口返回最长 3660 天闭区间内的完整日程，供 Scriptable 通过上次成功运行日增量补齐 iOS 日历。脚本按 Pendo 条目 ID 新增缺失事件，在 Keychain 中保存同步游标，并在源码顶部读取 Web 地址与 Widget Token。Scriptable 脚本位于 `plugins/pendo/web/scriptable/pendo_widget.js`，配置方式见 [Scriptable 小组件指南](../../docs/pendo-scriptable-widget.md)。

### 页面

| 页面 | 主要功能 |
| --- | --- |
| Dashboard | 核心摘要 |
| Events | 日程、集合、提醒及未到期提醒的确认开关 |
| Tasks | 待办看板、筛选和状态切换 |
| Ledger | 记账、账户、筛选和统计 |
| Notes | 笔记、分类、标签和关联 |
| Diary | 日记时间线、心情和模板字段 |
| Search | 跨模块搜索 |
| Stats | 活跃度、任务、账本、日记和日程统计 |
| Settings | 用户设置和登录设备 |
| Transfer | Bundle 导入导出、预览、冲突策略和日志 |

手机端页面保持单列内容流。Events 与 Diary 月历使用七列紧凑视图；Ledger 明细保持横向紧凑排列，分类标签与账户信息对齐；详情弹窗的操作栏按两列换行，并在底部安全区内完整显示。

Web 后端使用 FastAPI 与 uvicorn，API 前缀为 `/api`，静态前端位于 `plugins/pendo/web/static/`。

---

## 📅 导入与导出

聊天端导出 Markdown 档案，并通过 OneBot 私聊文件消息发送：

```text
/pendo export 我的档案
/pendo export 工作回顾 last30d event,todo
/pendo export 账本快照 2026-03 ledger
/pendo export 本月随笔 month note,diary
```

`/pendo import` 返回 Web Transfer 入口说明。Transfer 页面处理 `.pendo.zip` Bundle，提供检查、样例预览、执行、跳过、覆盖、生成副本和操作日志。Bundle 包含版本化 manifest、校验和与结构化数据文件。

---

## 💾 数据模型

运行数据位于 `data/pendo/pendo.db`。主要表如下：

| 表 | 内容 |
| --- | --- |
| `items` | 日程 leaf、待办、笔记、日记和账本主记录 |
| `event_collections` | 重复日程与多节点日程集合 |
| `reminder_logs` | 已投递提醒记录 |
| `scheduled_delivery_outbox` | 定时消息待办与逐目标确认 |
| `operation_logs` | 编辑、删除与撤销快照 |
| `user_settings` | 时区、静默时段、简报和 AI 授权 |
| `transfer_logs` | Bundle 导入日志 |
| `imported_bundles` | 已执行 Bundle 身份 |
| `login_code_registry` | 一次性登录 Code 摘要和期限 |
| `web_session_registry` | 浏览器会话摘要、设备和期限 |
| `widget_token_registry` | Widget Token 身份、期限与撤销状态 |

所有业务查询按 `user_id` 隔离。数据库使用 WAL、外键、busy timeout、事务和 schema migration。自然语言中的日程钟点先按用户或日程 IANA 时区解释，时间戳按 UTC 保存，展示时转换回对应 IANA 时区。

---

## ⏰ 定时任务

| Manifest ID | 时间 | Core 投递 | 任务 |
| --- | --- | --- | --- |
| `pendo_reminders` | 每分钟 | `targeted` 私聊 | 日程与待办提醒 |
| `pendo_daily_briefing` | 每分钟 | `targeted` 私聊 | 检查用户本地每日简报时间 |
| `pendo_diary_reminder` | 每分钟 | `targeted` 私聊 | 检查用户本地日记提醒时间 |
| `pendo_migrate_todos` | 每天 00:05 | `targeted` 私聊 | 顺延昨日 open 计划待办并通知所有者 |
| `pendo_prune_operation_logs` | 每天 00:15 | `silent` | 清理过期操作日志与撤销快照 |
| `pendo_weekly_finance_summary` | 每周日 21:00 | `targeted` 私聊 | 财务周报 |
| `pendo_month_end_finance_summary` | 每月最后一天 21:00 | `targeted` 私聊 | 财务月报 |
| `pendo_cleanup_demo_data` | 每 6 小时 | `silent` | 清理过期 Demo 数据 |

定时消息通过 `scheduled_delivery_outbox` 保存待办状态，并按目标确认结果推进投递。静默任务由 Core 保持零消息投递。

---

## ⚙️ 依赖与配置

Pendo 依赖 `python-dateutil`、`fastapi`、`uvicorn`、`PyJWT`、`pydantic` 和 `starlette`。项目常规安装会包含这些运行依赖。

Web 签名 secret 可通过环境变量 `PENDO_WEB_TOKEN_SECRET` 提供。AI 分析使用 Core 的 OpenAI 兼容配置，并受用户 `ai_consent` 控制。

---

## 🩺 排障

| 现象 | 检查项 |
| --- | --- |
| 命令进入模块目录 | 使用 `/pendo help <模块>` 查看完整子命令 |
| 提醒持续待办 | 检查用户设置、默认发送目标和 outbox 状态 |
| Web 启动失败 | 检查依赖、端口占用、监听地址和 Cookie 安全配置 |
| 登录 Code 校验失败 | 生成新 Code，并核对浏览器访问的 Web 实例 |
| 浏览器再次显示登录页 | 检查 Cookie、`pendo.db` 权限、服务时钟和会话期限 |
| Widget 返回 401 | 生成新 Token，或检查撤销状态与系统时间 |
| Bundle 导入失败 | 先执行检查与样例预览，再查看 Transfer 日志 |

---

## ✅ 开发验证

在仓库根目录运行：

```bash
python -m pytest -q tests/plugins/pendo
python -m ruff check plugins/pendo
python -m mypy plugins/pendo
```

工程边界与数据流见 [ARCHITECTURE.md](ARCHITECTURE.md)。
