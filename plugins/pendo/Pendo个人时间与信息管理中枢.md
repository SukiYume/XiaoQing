# Pendo 个人时间与信息管理中枢

> 集成化个人管理插件 - 日程、待办、笔记、日记，一应俱全

## 快速开始

```
# 添加日程
/pendo event add 明天下午2点开会

# 添加待办
/pendo todo add 提交周报 cat:工作 p:2

# 查看今日
/pendo event list today
/pendo todo list today
```

---

## 设计原则

| 原则 | 说明 |
|------|------|
| **统一数据模型** | 所有条目类型共享 `Item` 表结构 |
| **按需AI解析** | 仅日程使用AI解析，待办/笔记/日记采用规则解析 |
| **多用户隔离** | 所有数据按 `user_id` 完全隔离 |
| **软删除支持** | 5分钟撤销窗口，防止误操作 |

---

# 一、日程管理 (Event)

## 1.1 添加日程

### 命令格式
```
/pendo event add <自然语言描述>
```

### 示例

| 场景 | 命令 |
|------|------|
| 单次日程 | `/pendo event add 3月8日下午两点，国自然基金申请截止，提前一周和一天提醒` |
| 重复日程 | `/pendo event add 每月18号上午十点，公积金提取，重复7个月` |
| 简单添加 | `/pendo event add 明天9点开会` |

### 数据结构
```json
{
  "type": "event",
  "title": "会议标题",
  "content": "详细备注",
  "start_time": "2026-03-08T14:00:00",
  "end_time": "2026-03-08T16:00:00",
  "location": "会议室A",
  "remind_times": ["2026-03-08T13:00:00"],
  "rrule": "FREQ=MONTHLY;BYMONTHDAY=8",
  "parent_id": "abc12345"
}
```

## 1.2 查看日程

```
/pendo event list [范围]
```

### 支持的范围

| 参数 | 说明 | 示例 |
|------|------|------|
| `today` | 今天 | `/pendo event list today` |
| `tomorrow` | 明天 | `/pendo event list tomorrow` |
| `week` | 本周 | `/pendo event list week` |
| `month` | 本月 | `/pendo event list month` |
| `year` | 今年 | `/pendo event list year` |
| `YYYY-MM` | 指定月份 | `/pendo event list 2026-02` |
| `last7d` | 最近7天 | `/pendo event list last7d` |
| `start..end` | 日期范围 | `/pendo event list 2026-02-01..2026-02-14` |

## 1.3 编辑/删除日程

| 操作 | 命令 | 说明 |
|------|------|------|
| 删除单次 | `/pendo event delete <id>` | 删除该条目 |
| 删除重复 | `/pendo event delete <parent_id>` | 删除所有子事件 |
| 删除单次重复 | `/pendo event delete <child_id>` | 仅删除该次 (如 `abc12345_20260308`) |
| 编辑 | `/pendo event edit <id> <修改内容>` | AI重新解析 |

## 1.4 提醒操作

```
/pendo confirm <id>        # 确认提醒
/pendo snooze <id> 10m     # 延后10分钟
/pendo snooze <id> 19:00   # 延后到19:00
```

---

# 二、待办管理 (Todo)

## 2.1 添加待办

```
/pendo todo add <内容> [cat:分类] [p:1-4] [#标签]
```

### 优先级说明

| 级别 | 命令 | 图标 | 说明 |
|------|------|------|------|
| 紧急 | `p:1` | 🔴 | 最高优先级 |
| 高 | `p:2` | 🟠 | 重要但不紧急 |
| 中 | `p:3` | 🟡 | 默认优先级 |
| 低 | `p:4` | 🟢 | 可后续处理 |

### 示例

```
/pendo todo add 提交周报 cat:工作 p:2
/pendo todo add 买牛奶 cat:生活 p:4
/pendo todo add 紧急事项 #财务 p:1
```

### 分类说明

- **日期分类**: `cat:2026-02-02` - 当天的待办
- **自定义分类**: `cat:工作`、`cat:学习` 等

## 2.2 查看待办

```
/pendo todo list [分类] [done/undone]
```

| 命令 | 说明 |
|------|------|
| `/pendo todo list` | 列出所有分类 |
| `/pendo todo list today` | 查看今天的分类 |
| `/pendo todo list 2026-02-03` | 查看指定日期 |
| `/pendo todo list 工作` | 查看工作分类 |
| `/pendo todo list 工作 done` | 查看已完成 |

> 显示排序：按优先级排序，同级按创建时间排序

## 2.3 待办操作

```
/pendo todo done <id>      # 完成待办
/pendo todo undone <id>    # 重开待办
/pendo todo delete <id>    # 删除待办
/pendo todo delete cat:分类  # 删除整个分类
```

---

# 三、笔记管理 (Note)

## 3.1 添加笔记

```
/pendo note add <内容> [cat:分类] [#标签]
```

### 示例

```
/pendo note add 直接折叠找脉冲星 cat:工作 #文章
/pendo note add 会议记录：今天讨论了三个要点 #会议
```

| 字段 | 说明 | 限制 |
|------|------|------|
| 内容 | 笔记正文 | - |
| `cat:分类` | 分类 | 只能有一个 |
| `#标签` | 标签 | 可以有多个 |

## 3.2 查看笔记

```
/pendo note list [cat:分类] [#标签]
```

| 命令 | 效果 |
|------|------|
| `/pendo note list` | 查看所有笔记 |
| `/pendo note list cat:工作` | 只看该分类 |
| `/pendo note list #会议` | 只看该标签 |
| `/pendo note list cat:工作 #会议` | 同时满足两个条件 |

## 3.3 笔记操作

```
/pendo note view <id>           # 查看详情
/pendo note delete <id>         # 按ID删除
/pendo note delete cat:分类     # 按分类删除
```

> 不允许按标签删除，避免误删

---

# 四、日记管理 (Diary)

## 4.1 写日记

```
/pendo diary add [日期] <内容>
```

| 规则 | 说明 |
|------|------|
| 不带日期 | 写今天的日记 |
| 带日期 | 写指定日期的日记 |
| 多次添加 | 同一天多次添加会追加内容 |

## 4.2 查看日记

```
/pendo diary view <日期>              # 查看某日详情
/pendo diary list [范围]              # 列表（范围参数同event）
/pendo diary template                 # 查看模板
```

## 4.3 日记模板

| 模板ID | 名称 | 说明 |
|--------|------|------|
| `default` | 自由日记 | 自由记录 |
| `three_good` | 三件好事 | 记录三件开心的事 |
| `summary` | 今日总结 | 做了什么/学到什么/改进/明天计划 |
| `mood` | 情绪记录 | 心情评分与分析 |

```
/pendo diary three_good    # 使用三件好事模板
/pendo diary summary       # 使用今日总结模板
```

## 4.4 删除日记

```
/pendo diary delete <日期>
```

---

# 五、搜索

## 5.1 基础搜索

```
/pendo search <关键词>
```

## 5.2 高级搜索

### 按类型
```
/pendo search 会议 type=event
/pendo search 报销 type=task
```

### 按时间
```
/pendo search 项目 range=last7d
/pendo search 日记 range=2026-01
```

### 组合搜索
```
/pendo search 会议 type=event range=last7d
```

---

# 六、其他操作

## 6.1 撤销删除

```
/pendo undo              # 撤销5分钟内的删除
/pendo undo 10           # 撤销10分钟内的删除
```

## 6.2 设置管理

```
/pendo settings                                  # 查看设置
/pendo settings reminder on/off                  # 开关提醒
/pendo settings timezone Asia/Shanghai           # 设置时区
/pendo settings quiet_hours 23:00-07:00          # 静默时段
/pendo settings privacy on/off                   # 隐私模式
```

## 6.3 导入导出

```
/pendo export md                                 # 导出所有
/pendo export md range=last30d                   # 最近30天
/pendo export md range=2026-01-01..2026-01-31    # 日期范围
/pendo import md                                 # 导入
/pendo import md preview                         # 预览导入
```

---

# 七、数据架构

## 7.1 数据类型

| 类型 | type值 | 特点 |
|------|--------|------|
| 日程 | `event` | 有开始/结束时间 |
| 待办 | `task` | 有优先级/状态 |
| 笔记 | `note` | 有分类/标签 |
| 日记 | `diary` | 有日期/情绪 |

## 7.2 统一数据表

所有条目存储在同一张 `items` 表中，通过 `type` 字段区分。

## 7.3 多用户隔离

- 所有查询自动附加 `WHERE owner_id = ?`
- 用户只能看到和操作自己的数据
- 设置完全隔离

## 7.4 软删除机制

1. 删除时标记 `deleted_at` 时间戳
2. 5分钟内可使用 `/pendo undo` 撤销
3. 超时后数据自动清理

## 7.5 提醒机制

| 特性 | 说明 |
|------|------|
| 检查频率 | 每分钟一次 |
| 时间窗口 | ±2分钟 |
| 静默时段 | 默认 23:00-07:00 |
| 重要事项 | 静默时段仍会提醒 |

---

# 八、定时任务

| 任务 | 时间 | 说明 |
|------|------|------|
| `check_reminders` | 每分钟 | 检查并发送提醒 |
| `daily_briefing` | 每天8:00 | 推送每日简报 |
| `diary_reminder` | 每天21:30 | 提醒写日记 |

---

# 附录：命令速查

## 日程
```
/pendo event add 明天9点开会
/pendo event list today
/pendo event delete <id>
/pendo event edit <id> <修改内容>
```

## 待办
```
/pendo todo add 写报告 cat:工作 p:2
/pendo todo list today
/pendo todo done <id>
/pendo todo undone <id>
```

## 笔记
```
/pendo note add 笔记内容 #标签
/pendo note list cat:分类
/pendo note view <id>
```

## 日记
```
/pendo diary add 今天发生了好事
/pendo diary view 2026-02-03
/pendo diary list
/pendo diary three_good
```

## 搜索与设置
```
/pendo search 关键词
/pendo search 会议 type=event
/pendo settings
/pendo undo
```
