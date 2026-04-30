# Pendo 个人时间与信息管理中枢

> Pendo 是 XiaoQing 内置的个人时间与信息管理插件。它把日程、待办、笔记、日记、账本、提醒、搜索、统计和 Web 控制台放在同一套数据模型中，聊天端适合快速录入和提醒处理，Web 端适合集中管理、批量迁移和可视化复盘。

## 快速开始

```text
# 查看帮助
/pendo
/pendo help event
/pendo ledger

# 快速录入
/pendo event add 明天9点组会，提前30分钟提醒
/pendo todo add 提交周报 plan:2026-05-01 deadline:2026-05-01T18:00 cat:工作 p:2
/pendo note add title:会议纪要 content 讨论数据迁移方案 cat:工作 #复盘
/pendo diary add 今天完成了项目复盘 mood:happy score:8
/pendo ledger quick 35.5 午饭 cat:餐饮 account:微信

# 查询和界面
/pendo search 组会 type=event range=last30d
/pendo web start
/pendo web token
```

## 设计原则

| 原则 | 说明 |
|---|---|
| 统一条目模型 | 大部分对象都落在 `items` 表，用 `type` 区分 `event`、`task`、`note`、`diary`、`ledger` |
| 事件图结构 | 单次日程是 leaf event；重复和多节点日程由 `event_collections` 组织多个 leaf |
| 明确字段语义 | 待办不再把日期塞进分类；账本以 `amount_cents` 为统计主字段；日记同一天可多篇 |
| 统一校验入口 | CLI、Web API、Bundle 导入都应通过 `utils/validators.py` 的归一化逻辑 |
| 多用户隔离 | 所有数据读写都以 `owner_id` 隔离 |
| 聊天 + Web 双入口 | 聊天端负责快速命令和提醒，Web 端负责高密度 CRUD、统计和迁移 |

## 一、日程管理 (Event)

### 添加日程

```text
/pendo event add <自然语言描述>
```

| 场景 | 命令 |
|---|---|
| 单次日程 | `/pendo event add 3月8日下午两点，国自然基金申请截止，提前一周和一天提醒` |
| 重复日程 | `/pendo event add 每月18号上午十点，公积金提取，重复7个月` |
| 多节点日程 | `/pendo event add 5月1日9点出发、14点入住、5月3日18点返程，杭州团建，提前2小时提醒` |
| 简单添加 | `/pendo event add 明天9点开会` |

当前事件结构：

```json
{
  "item": {
    "type": "event",
    "event_role": "single | multi_node_child | recurring_occurrence",
    "event_collection_id": "collection id 或 null",
    "event_collection_kind": "multi_node | recurring 或 null",
    "title": "节点或单次标题",
    "start_time": "2026-05-01T09:00:00",
    "end_time": "2026-05-01T10:00:00",
    "location": "会议室A",
    "notes": "补充说明",
    "reminder_rules": [{ "offset_seconds": 1800 }],
    "remind_times": ["2026-05-01T08:30:00"]
  },
  "collection": {
    "kind": "multi_node | recurring",
    "title": "整体标题",
    "rrule": "FREQ=MONTHLY;COUNT=7"
  }
}
```

单次日程只有一条 leaf。重复日程和多节点日程有一个 `event_collections` 集合头，每个 occurrence 或节点仍是可独立查看、编辑、删除、设置提醒的 leaf event。

### 查看日程

```text
/pendo event list [范围] [cat:分类] [#标签]
/pendo event view <id>
```

| 范围 | 示例 |
|---|---|
| 今天/明天/本周/本月/今年 | `/pendo event list today`、`/pendo event list week` |
| 指定月份 | `/pendo event list 2026-05` |
| 日期范围 | `/pendo event list 2026-05-01..2026-05-07` |
| 分类和标签 | `/pendo event list week cat:工作 #会议` |

### 编辑、删除和提醒

```text
/pendo event edit <collection_id> 标题改为FAST会议行程
/pendo event edit <leaf_id> 改到4月22日12:43
/pendo event edit <leaf_id> 备注从北京南坐G123去会场
/pendo event delete <id>

/pendo event reminders <id>
/pendo event reminders list week
/pendo event reminders set <id> 提前1天和2小时提醒
/pendo event reminders delete <id> all
/pendo event reminders confirm <id> today
```

`collection_id` 操作重复或多节点整体；`leaf_id` 操作某一次重复实例或某个多节点节点。删除集合会级联删除子节点和未发送提醒；删除单个 leaf 只影响该节点。

## 二、待办管理 (Todo / Task)

### 添加待办

```text
/pendo todo add <内容> [plan:YYYY-MM-DD] [deadline:YYYY-MM-DDTHH:MM] [remind:YYYY-MM-DDTHH:MM[,YYYY-MM-DDTHH:MM]] [cat:分类] [p:1-5] [#标签]
```

| 字段 | 说明 |
|---|---|
| `plan:` | 计划处理日期，决定今日/未来/收件箱视图 |
| `deadline:` | 硬截止时间，只在确实有截止时填写 |
| `remind:` | 明确提醒时间，可逗号分隔多个 |
| `cat:` | 文字分类，如 `工作`、`生活` |
| `p:` | 优先级 1-5，默认 3 |

```text
/pendo todo add 写项目周报 cat:工作 p:2 plan:2026-05-01 deadline:2026-05-01T18:00 #周报
/pendo todo add 交材料 remind:2026-05-01T09:00,2026-05-01T17:00
```

### 查看和管理

```text
/pendo todo list
/pendo todo list today
/pendo todo list 工作 done
/pendo todo list cancelled
/pendo todo view <id>
/pendo todo done <id>
/pendo todo cancel <id>
/pendo todo undone <id>
/pendo todo edit <id> <内容> [plan:/deadline:/remind:/cat:/p:/#标签]
/pendo todo delete <id|cat:分类>
```

状态只包含 `open`、`done`、`cancelled`。历史字段如 `due_time`、`estimate`、`subtasks`、`dependencies`、`progress` 只属于迁移输入，不属于当前运行时模型。

## 三、笔记管理 (Note)

### 添加笔记

```text
/pendo note add <内容> [cat:分类] [#标签] [ref:条目ID]
/pendo note add title:<标题> content <正文> [cat:分类] [#标签]
```

多行笔记也支持标题后换行正文：

```text
/pendo note add title:会议纪要
1. 确认 Bundle 导入策略
2. 下周补齐回归脚本
cat:工作 #记录
```

笔记支持：

- `tags` 和 `category`
- `references`: 结构化引用，可指向其他条目或外部描述
- `related_items`: 由引用或显式链接维护的关联条目 ID

### 查看和管理

```text
/pendo note list
/pendo note list 工作
/pendo note list cat:工作 #文章 since:last30d
/pendo note view <id>
/pendo note edit <id> title:新标题 content 新正文 cat:工作 #复盘
/pendo note append <id> 补充一条结论
/pendo note tag <id> #论文 #想法
/pendo note untag <id> #想法
/pendo note link <id> <关联条目ID>
/pendo note delete <id|cat:分类>
```

不支持按标签批量删除，避免误删。

## 四、日记管理 (Diary)

### 写日记

```text
/pendo diary add [日期] <内容> [weather:天气] [location:地点] [mood:happy] [score:1-10] [tags:a,b] [favorite:true]
```

```text
/pendo diary add 今天跑步5公里 mood:happy score:8 tags:运动,复盘 favorite:true
/pendo diary add 2026-05-01 今天跑步5公里 weather:晴 location:操场 mood:happy score:8
```

每条 `diary` 是一篇独立日记。同一天可以写多篇，系统用 `diary_date` 归到同一天，用 `entry_time` 排序；模板回答保存在 `template_answers`。

### 模板和查看

```text
/pendo diary template
/pendo diary template 1
/pendo diary template mood
/pendo diary view 2026-05-01
/pendo diary view <id>
/pendo diary list month
/pendo diary list 2026-05 mood:happy
/pendo diary delete <日期|ID>
```

同一天有多篇日记时，按日期删除会提示具体 ID，避免误删。

## 五、账本管理 (Ledger)

### 快速记账

```text
/pendo ledger quick <金额> <描述> [cat:分类] [in|out|transfer|type:expense/income/transfer] [account:账户] [to:账户] [merchant:商户] [date:YYYY-MM-DD] [remark:备注]
```

```text
/pendo ledger quick 35.5 午饭 cat:餐饮 account:微信 merchant:食堂
/pendo ledger quick 5000 工资 cat:工资 in account:招行
/pendo ledger quick 1000 还款 transfer account:微信 to:招行 date:2026-05-01
```

账本以 `amount_cents` 为统计主字段，`amount` 只是展示镜像。`transaction_type` 支持 `expense`、`income`、`transfer`；转账不计入收入或支出汇总。

### 录入、列表和编辑

```text
/pendo ledger add 28 午饭 cat:餐饮 account:微信
/pendo ledger add
/pendo ledger list
/pendo ledger list 2026-03 type:expense cat:餐饮 amount:20..100 ex
/pendo ledger list month account:微信 page:2
/pendo ledger view <id>
/pendo ledger edit <id> amount:50 cat:交通 account:微信
/pendo ledger delete <id>
/pendo ledger summary 2026-03
```

`ledger add` 支持一条消息完成录入；无参数时会先提示发送同样的一条记录。未写 `cat`、`account`、`date` 时默认使用其他分类、现金账户和今天，避免在 QQ 里依赖空消息选择默认值。

## 六、搜索、提醒和设置

### 搜索

```text
/pendo search <关键词>
/pendo search 组会 type=event range=last30d
/pendo search 午饭 type=ledger transaction_type=expense account=微信
/pendo search 周报 type=task status=open category=工作
```

搜索覆盖标题、正文、备注、分类、标签、账本字段和事件集合文本。常用筛选包括 `type`、`range`、`status`、`category`、`transaction_type`、`account`、`merchant`。

### 提醒

```text
/pendo confirm <id>
/pendo snooze <id> 10m
/pendo snooze <id> 19:00
/pendo event reminders set/delete/confirm <id> ...
```

`/pendo confirm` 处理刚发送的提醒；未来提醒批量管理应使用 `/pendo event reminders ...`。

### 设置

```text
/pendo settings
/pendo settings reminder on
/pendo settings timezone Asia/Shanghai
/pendo settings quiet_hours 23:00-07:00
/pendo settings daily_report 08:30
/pendo settings diary_remind 21:30
/pendo settings privacy on
```

用户偏好存储在 `user_settings` 表中。

## 七、导出、导入与迁移

### 聊天端 Markdown 档案导出

```text
/pendo export 我的档案
/pendo export 工作回顾 last30d event,todo
/pendo export 账本快照 2026-03 ledger
/pendo export 本月随笔 month note,diary
```

- 命令格式：`/pendo export <文件名> [范围] [类型]`
- 范围支持：`all`、`today`、`week`、`month`、`YYYY-MM`、`last7d`、`start..end`
- 类型支持：`event`、`todo/task`、`note`、`ledger`、`diary`
- 导出结果通过 OneBot 私聊文件消息发送给当前用户

聊天端不再提供 Markdown 导入。跨设备迁移、恢复、预览和冲突处理走 Web 端 Bundle 流程。

### Web 端 Pendo Bundle

Web 控制台的迁移页支持 `.pendo.zip` 数据包：

- 导出预览和下载
- 导入 inspect、samples、execute
- 冲突策略：跳过、覆盖、复制
- duplicate 导入时重写条目 ID 和跨条目关系
- `transfer_logs` 与 `imported_bundles` 记录审计和幂等信息

支持类型包括 `event`、`task/todo`、`note`、`diary`、`ledger`，以及多节点/重复日程的 `event_collection`。

### 一次性迁移脚本

当前重构迁移入口：

```text
plugins/pendo/scripts/migrate_pendo_redesign.py
plugins/pendo/scripts/migrate_event_graph.py
```

迁移脚本负责把旧 event/note/diary/task/ledger 数据转换成当前结构。运行时代码不应继续依赖旧字段语义。

## 八、Web 控制台和 Scriptable

### Web 控制台

```text
/pendo web start
/pendo web status
/pendo web stop
/pendo web token
/pendo web widget-token
```

默认地址是 `http://127.0.0.1:8765`。可通过环境变量调整：

```text
PENDO_WEB_HOST=127.0.0.1
PENDO_WEB_PORT=8766
PENDO_WEB_DEMO_ENABLED=1
PENDO_WEB_TOKEN_SECRET=<stable-secret>
```

Web 后端是 `plugins/pendo/web/server.py` 中的 FastAPI + uvicorn 服务，API 前缀为 `/api`，静态前端在 `plugins/pendo/web/static`。页面包括 Dashboard、events、tasks、notes、diary、ledger、search、stats、settings、transfer。

### Scriptable 小组件

- 摘要接口：`GET /api/widget/summary`
- 参数：`section=tasks|ledger|notes|auto`
- 鉴权：`Authorization: Bearer <widget_token>`
- 脚本路径：`plugins/pendo/web/scriptable/pendo_widget.js`

widget token 只能访问 `/api/widget/*` 的 `GET` 请求，适合把未来日程、待办、财务和笔记摘要放到 iPhone 主屏。

## 九、数据架构

| 类型 | `type` 值 | 关键字段 |
|---|---|---|
| 日程 | `event` | `start_time`、`end_time`、`event_role`、`event_collection_id`、`reminder_rules`、`remind_times` |
| 待办 | `task` | `plan_date`、`deadline_at`、`priority`、`status`、`completed_at`、`cancelled_at` |
| 笔记 | `note` | `tags`、`category`、`references`、`related_items` |
| 日记 | `diary` | `diary_date`、`entry_time`、`mood`、`mood_score`、`template_answers`、`is_favorite` |
| 账本 | `ledger` | `amount_cents`、`transaction_type`、`ledger_category`、`ledger_date`、`account_name`、`counter_account_name` |

主要数据库表：

- `items`: 统一条目表
- `event_collections`: 重复/多节点日程集合
- `items_fts`: FTS5 全文搜索索引
- `reminder_logs`: 提醒发送/确认日志
- `operation_logs`: create/update/delete/export/import 审计
- `user_settings`: 用户设置
- `transfer_logs`: Web Bundle 操作日志
- `imported_bundles`: Bundle 幂等导入记录

## 十、定时任务

| `plugin.json` 任务 | 入口 | 说明 |
|---|---|---|
| `pendo_reminders` | `scheduled` | 每分钟检查日程/待办提醒 |
| `pendo_daily_briefing` | `scheduled_daily_briefing` | 每分钟检查是否到用户本地日报时间 |
| `pendo_diary_reminder` | `scheduled_diary_reminder` | 每分钟检查是否到用户本地日记提醒时间 |
| `pendo_migrate_todos` | `scheduled_migrate_todos` | 每天 00:05 顺延昨日仍 open 的计划待办 |
| `pendo_weekly_finance_summary` | `scheduled_weekly_finance_summary` | 每周日 21:00 财务总结 |
| `pendo_month_end_finance_summary` | `scheduled_month_end_finance_summary` | 每月最后一天 21:00 财务总结 |
| `pendo_cleanup_demo_data` | `scheduled_cleanup_demo_data` | 每 6 小时清理过期 Web demo 数据 |

## 附录：命令速查

```text
/pendo help <event|todo|note|diary|ledger|search|reminder|export|import|settings|web>
/pendo event add 明天9点开会
/pendo todo add 写报告 cat:工作 p:2
/pendo note add 记录想法 #标签
/pendo diary add 今天发生了好事 mood:happy score:8
/pendo ledger quick 35 午饭 cat:餐饮 account:微信
/pendo search 关键词
/pendo export 我的档案
/pendo web token
```
