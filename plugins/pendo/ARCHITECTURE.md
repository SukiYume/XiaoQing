# Pendo 插件架构说明

## 设计理念

Pendo（个人时间与信息管理中枢）采用模块化、分层的架构设计，遵循以下原则：

1. **统一数据模型**: 所有条目类型（日程、待办、笔记、日记）共享统一的Item表结构
2. **功能分离**: 不同功能模块独立，通过handlers层隔离业务逻辑
3. **服务复用**: 核心服务（数据库、AI、提醒等）可被多个handler复用
4. **按需AI**: 仅日程使用AI解析，其他模块采用规则解析提高效率
5. **安全优先**: 多用户数据隔离，所有操作验证权限
6. **可维护性**: 统一错误处理，操作日志，消息格式化工具

## 目录结构

```
plugins/pendo/
├── main.py                 # 插件入口，命令路由和服务管理
├── plugin.json             # 插件配置清单
├── config.py               # 集中配置管理
├── ARCHITECTURE.md          # 本文档
├── README.md               # 用户使用文档
│
├── core/                   # 核心组件
│   ├── router.py           # 命令路由器
│   └── exceptions.py       # 异常定义
│
├── models/                 # 数据模型
│   ├── item.py             # 条目模型定义
│   ├── types.py            # 通用类型定义
│   └── constants.py        # 常量定义
│
├── handlers/               # 业务处理器
│   ├── event.py            # 日程处理（使用AI）
│   ├── task.py             # 待办处理（规则解析）
│   ├── note.py             # 笔记处理（规则解析）
│   ├── diary.py            # 日记处理（模板式）
│   └── search.py           # 搜索处理（FTS5）
│
├── services/               # 核心服务
│   ├── db.py               # 数据库服务
│   ├── ai_parser.py        # AI自然语言解析
│   ├── reminder.py         # 提醒服务
│   ├── exporter.py         # 导入导出服务
│   ├── rule_parser.py      # 规则解析器
│   └── llm_client.py       # LLM客户端
│
├── commands/               # 命令处理模块
│   ├── settings.py         # 设置命令
│   ├── operations.py       # 操作命令（确认/延后/撤销）
│   ├── session.py          # 会话处理
│   └── scheduled.py        # 定时任务
│
├── utils/                  # 工具模块
│   ├── time_utils.py       # 时间解析工具
│   ├── db_ops.py           # 数据库操作混合类
│   ├── error_handlers.py   # 错误处理装饰器
│   └── formatters.py       # 消息格式化工具
│
└── data/                   # 数据目录
    └── pendo.db            # SQLite数据库（运行时生成）
```

## 架构分层

```
┌─────────────────────────────────────────┐
│          XiaoQing Bot Framework         │
└─────────────────┬───────────────────────┘
                  │
                  ▼
┌─────────────────────────────────────────┐
│         main.py (插件入口)               │
│  • 插件生命周期管理 (init/cleanup)       │
│  • 命令路由 (CommandRouter)              │
│  • 服务实例管理 (缓存机制)               │
│  • 定时任务入口 (scheduled_*)            │
└─────────────────┬───────────────────────┘
                  │
        ┌─────────┴──────────┐
        ▼                    ▼
┌──────────────┐      ┌──────────────┐
│   Handlers   │      │   Services   │
│   (处理器层)  │◄────►│   (服务层)   │
└──────────────┘      └──────────────┘
        │                    │
        └─────────┬──────────┘
                  ▼
          ┌──────────────┐
          │    Models    │
          │  (数据模型)   │
          └──────────────┘
                  │
                  ▼
          ┌──────────────┐
          │   Database   │
          │  (SQLite)    │
          └──────────────┘
```

## 核心组件详解

### 1. main.py (插件入口)

**职责**:
- 插件生命周期管理（`init`/`cleanup`）
- 命令分发与路由
- 服务实例管理（带缓存）
- 定时任务入口

**关键函数**:
```python
async def handle(command, args, event, context)  # 主入口
async def scheduled(context)                      # 每分钟提醒检查
async def scheduled_daily_briefing(context)       # 每日简报
async def scheduled_diary_reminder(context)       # 日记提醒
```

### 2. core/router.py (命令路由器)

**职责**:
- 解析子命令
- 路由到对应handler
- 处理命令别名
- 提供帮助信息

**支持的路由**:
- `event` / `e` / `日程` → EventHandler
- `todo` / `task` / `t` / `待办` → TaskHandler
- `note` / `n` / `笔记` → NoteHandler
- `diary` / `d` / `日记` → DiaryHandler
- `search` / `s` / `搜索` → SearchHandler
- `confirm` / `snooze` / `undo` → 操作命令
- `export` / `import` / `settings` → 辅助命令

### 3. config.py (配置管理)

**职责**:
- 集中管理所有可配置项
- 避免硬编码
- 提供配置验证

**配置分类**:
| 分类 | 配置项 |
|------|--------|
| 数据库 | DB_FILENAME, FTS_LANGUAGE |
| 提醒 | REMINDER_CHECK_WINDOW_SECONDS, REMINDER_POLICIES |
| AI | AI_PARSE_TIMEOUT, AI_PARSE_TEMPERATURE |
| 日志 | LOG_OPERATION, LOG_OPERATION_RETENTION_DAYS |
| 消息 | MESSAGE_LONG_THRESHOLD, MESSAGE_PRIVACY_MODE_DEFAULT |
| 分页 | LIST_PAGE_SIZE, DEFAULT_SEARCH_LIMIT |
| 日记 | DIARY_TEMPLATES, MOOD_ANALYSIS_CONFIG |

### 4. Models (数据模型层)

**文件**: `models/item.py`, `models/constants.py`

**类型定义**:
```python
class ItemType(Enum):
    EVENT = 'event'     # 日程
    TASK = 'task'       # 待办
    NOTE = 'note'       # 笔记
    DIARY = 'diary'     # 日记

class TaskStatus(Enum):
    TODO = 'todo'               # 待办
    IN_PROGRESS = 'in_progress' # 进行中
    DONE = 'done'               # 已完成
    CANCELLED = 'cancelled'     # 已取消

class Priority(Enum):
    URGENT = 1    # 紧急
    HIGH = 2      # 高
    MEDIUM = 3    # 中
    LOW = 4       # 低
```

**字段常量** (`ItemFields`):
- 通用字段: ID, TYPE, TITLE, CONTENT, TAGS, CATEGORY
- 时间字段: START_TIME, END_TIME, DUE_TIME
- 用户字段: OWNER_ID, CONTEXT
- 日记字段: DIARY_DATE, MOOD, MOOD_SCORE

### 5. Handlers (处理器层)

每个handler负责一类功能的业务逻辑，继承自 `DbOpsMixin` 获得数据库操作能力。

#### 5.1 EventHandler (handlers/event.py)

**特点**: 使用AI解析自然语言

**功能**:
- 创建日程（AI解析时间/地点/提醒）
- 查看/编辑/删除日程
- 重复日程支持（RRULE）
- 冲突检测

**AI解析示例**:
```
"明天9点开会，地点A，提前30分钟提醒"
↓ 解析为
{
  "title": "开会",
  "start_time": "2026-02-03T09:00:00",
  "location": "地点A",
  "remind_times": ["2026-02-03T08:30:00"]
}
```

#### 5.2 TaskHandler (handlers/task.py)

**特点**: 纯规则解析，无需AI

**功能**:
- 添加待办（支持 cat:分类 p:优先级 #标签）
- 按分类查看待办
- 完成/重开/删除待办
- 批量删除分类

**语法示例**:
```
/pendo todo add 写报告 cat:工作 p:2 #项目
→ 分类: 工作, 优先级: 高, 标签: 项目
```

#### 5.3 NoteHandler (handlers/note.py)

**特点**: 纯规则解析，无需AI

**功能**:
- 快速记录笔记
- 分类和标签管理
- 查看笔记详情
- 删除笔记

#### 5.4 DiaryHandler (handlers/diary.py)

**特点**: 模板式记录，无需AI

**功能**:
- 自由写日记
- 模板写日记（三件好事/今日总结/情绪记录）
- 情绪分析
- 多轮对话式记录

**内置模板**:
- `default` - 自由日记
- `three_good` - 三件好事
- `summary` - 今日总结
- `mood` - 情绪记录

#### 5.5 SearchHandler (handlers/search.py)

**特点**: 基于SQLite FTS5全文搜索

**功能**:
- 全文搜索
- 按类型筛选
- 按时间范围筛选
- 结果分组显示

### 6. Services (服务层)

#### 6.1 Database (services/db.py)

**职责**:
- SQLite数据库封装
- CRUD操作（带权限验证）
- 全文搜索（FTS5）
- 用户设置管理
- 操作日志记录

**关键特性**:
- 统一的表结构，支持所有条目类型
- JSON字段存储复杂数据
- 多个索引优化查询性能
- 软删除支持（5分钟撤销窗口）
- 多用户数据隔离

**数据库表**:
```sql
-- 主表: items (所有条目)
-- 全文搜索: items_fts (FTS5虚拟表)
-- 提醒日志: reminder_logs
-- 操作日志: operation_logs
-- 用户设置: user_settings
```

#### 6.2 AIParser (services/ai_parser.py)

**职责**:
- 自然语言时间解析
- 事件信息提取
- LLM集成（可选）

**解析能力**:
- 相对时间: "明天9点"、"下周三下午2点"
- 时间范围: "9点-11点"、"14:00-16:00"
- 重复规则: "每天"、"每周一"、"每月18号"
- 地点识别: "@会议室A"
- 提醒表达: "提前30分钟提醒"

**可靠性改进**:
- AI失败时自动回退到规则解析
- 完整的错误处理和输入验证
- 中文数字解析（支持"二十"、"一百"等）

#### 6.3 ReminderService (services/reminder.py)

**职责**:
- 计算提醒时间
- 检查并发送提醒
- 日程冲突检测
- 提醒日志记录

**提醒策略**:
```python
REMINDER_POLICIES = {
    'meeting': [...],   # 会议: 1天前 + 2小时前 + 15分钟前
    'deadline': [...],  # 截止: 7天前 + 3天前 + 1天前 + 2小时前
    'travel': [...],    # 出行: 1天前 + 3小时前 + 1小时前
    'habit': [...]      # 习惯: 准时提醒
}
```

#### 6.4 ExporterService (services/exporter.py)

**职责**:
- 导出为Markdown
- 导入Markdown
- Front Matter解析
- 数据验证

**导出格式**:
```markdown
---
type: event
created_at: 2026-02-01T09:00:00
tags: []
category: 未分类
start_time: 2026-02-01T09:00:00
---

# 开周会

会议内容...
```

### 7. Commands (命令处理模块)

#### 7.1 commands/settings.py

**职责**:
- 查看用户设置
- 修改用户设置
- 设置验证

**支持的设置**:
- `timezone` - 时区
- `quiet_hours` - 静默时段
- `daily_report_time` - 每日简报时间
- `diary_remind_time` - 日记提醒时间
- `privacy_mode` - 隐私模式

#### 7.2 commands/operations.py

**职责**:
- 确认提醒
- 延后提醒
- 撤销删除

#### 7.3 commands/session.py

**职责**:
- 多轮对话处理
- 日记模板会话
- 日程冲突确认会话

#### 7.4 commands/scheduled.py

**职责**:
- 提醒检查
- 每日简报生成
- 日记提醒发送
- 定时任务执行

### 8. Utils (工具模块)

#### 8.1 time_utils.py

**职责**:
- 时间解析（parse_event_time_range, parse_date_optional）
- 时区处理（TimezoneHelper）
- 时间格式化
- 中文时间解析

#### 8.2 db_ops.py

**职责**:
- DbOpsMixin - 数据库操作混合类
- 提供统一的CRUD方法
- 自动权限验证
- 操作日志记录

#### 8.3 error_handlers.py

**职责**:
- 统一错误处理装饰器
- 异常捕获和日志记录
- 友好的错误消息返回

#### 8.4 formatters.py

**职责**:
- 消息格式化工具类
- 统一的图标和标签定义
- 避免重复的格式化代码

**核心类**:
```python
class ItemFormatter:
    - format_priority()      # 格式化优先级
    - format_status_icon()   # 格式化状态图标
    - format_datetime()      # 格式化时间
    - format_tags()          # 格式化标签
    - format_time_range()    # 格式化时间范围
    - truncate_content()     # 截断内容

class MessageBuilder:
    # 流式构建多行消息
    - add_line() / add_header() / add_item() / add_info()
    - build()
```

## 数据流

### 1. 创建条目的典型流程

```
用户输入 "明天9点开会"
  │
  ▼
main.handle() → CommandRouter.route()
  │
  ▼
EventHandler.add_event()
  │
  ├─ AIParser.parse_natural_language_with_ai()
  │   └─ 提取: title, start_time, location, remind_times
  │
  ├─ ReminderService.detect_conflict()
  │   └─ 检查时间冲突
  │
  ▼
Database.items.insert_item()
  │
  ├─ 写入主表 (items)
  ├─ 更新FTS索引 (items_fts)
  ├─ 记录操作日志 (operation_logs)
  │
  ▼
返回格式化消息 (使用 ItemFormatter)
```

### 2. 搜索的流程

```
用户输入 "/pendo search 会议"
  │
  ▼
SearchHandler.search()
  │
  ├─ _parse_search_query()
  │   └─ 提取关键词和筛选条件
  │
  ▼
Database.items.search_items()
  │
  ├─ FTS5全文搜索 (MATCH AGAINST)
  ├─ 结构化筛选 (WHERE type = ?, ...)
  │
  ▼
格式化结果 (ItemFormatter, TYPE_NAMES)
  │
  ▼
返回给用户
```

### 3. 提醒的流程

```
定时任务触发 (每分钟)
  │
  ▼
commands.scheduled.check_reminders()
  │
  ▼
ReminderService.check_and_send_reminders()
  │
  ├─ 获取所有带提醒的条目
  ├─ 检查时间窗口 (±2分钟)
  ├─ 检查提醒日志 (防重复)
  ├─ 检查静默时段
  │
  ▼
构建提醒消息 (ItemFormatter.format_datetime())
  │
  ▼
发送给用户
```

## 数据库设计

### 主表: items

**设计思路**: 使用单表存储所有类型的条目，通过type字段区分。

**优点**:
- 统一查询接口
- 易于全文搜索
- 支持跨类型关联
- 简化多用户隔离

```sql
CREATE TABLE items (
    -- 通用字段
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,                    -- event/task/note/diary
    title TEXT,
    content TEXT,
    tags TEXT,                             -- JSON数组
    category TEXT DEFAULT '未分类',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    owner_id TEXT NOT NULL,                -- 🔒 用户ID（多用户隔离）
    context TEXT,                          -- JSON对象（来源信息）
    deleted INTEGER DEFAULT 0,
    deleted_at TEXT,

    -- Event特有字段
    start_time TEXT,
    end_time TEXT,
    location TEXT,
    rrule TEXT,                            -- 重复规则
    remind_times TEXT,                     -- JSON数组
    parent_id TEXT,

    -- Task特有字段
    priority INTEGER,                      -- 1-4
    status TEXT,                          -- todo/in_progress/done
    completed_at TEXT,

    -- Diary特有字段
    diary_date TEXT,                       -- YYYY-MM-DD
    mood TEXT,
    mood_score INTEGER
);

-- 核心索引
CREATE INDEX idx_owner_type ON items(owner_id, type, deleted);
CREATE INDEX idx_start_time ON items(start_time) WHERE type='event';
CREATE INDEX idx_diary_date ON items(diary_date) WHERE type='diary';
```

### 全文搜索表: items_fts

```sql
CREATE VIRTUAL TABLE items_fts USING fts5(
    id UNINDEXED,
    title,
    content,
    tags,
    category
);

-- 自动同步触发器
CREATE TRIGGER items_fts_insert AFTER INSERT ON items BEGIN
  INSERT INTO items_fts(id, title, content, tags, category)
  VALUES (new.id, new.title, new.content, new.tags, new.category);
END;
```

## 安全与可靠性

### 1. 多用户数据隔离

**保证**:
- ✅ 用户只能访问自己的数据（WHERE owner_id = ?）
- ✅ 用户只能修改自己的数据
- ✅ 搜索结果自动过滤
- ✅ 设置完全隔离

### 2. 统一错误处理

**装饰器**: `@handle_command_errors`
- 捕获所有异常
- 记录详细日志
- 返回友好错误消息
- 避免敏感信息泄露

### 3. 操作审计

所有重要操作自动记录到operation_logs：
- create: 记录新建条目
- update: 记录修改内容
- delete: 记录删除操作

### 4. 撤销机制

- 软删除支持（deleted_at字段）
- 5分钟撤销窗口
- `/pendo undo` 命令恢复

## 扩展指南

### 添加新的条目类型

1. 在 `models/constants.py` 中添加 `ItemType` 枚举
2. 在数据库表中添加类型特有字段（如需要）
3. 创建对应的 Handler (继承 `DbOpsMixin`)
4. 在 `main.py` 的 `_build_command_router()` 中注册

### 添加新的日记模板

在 `config.py` 的 `DIARY_TEMPLATES` 中添加：

```python
'new_template': {
    'name': '模板名称',
    'prompts': ['问题1', '问题2', ...]
}
```

### 添加新的提醒策略

在 `config.py` 的 `REMINDER_POLICIES` 中添加：

```python
'new_type': {
    'name': '策略名称',
    'reminders': [
        {'offset_days': -1, 'message': '提醒文案'},
    ]
}
```

## 性能优化

### 已实现优化

1. **数据库优化**
   - WAL模式（并发读写）
   - 复合索引（owner_id + type + deleted）
   - FTS5全文搜索
   - 线程安全的连接管理

2. **查询优化**
   - 参数化查询
   - 默认限制返回数量（50条）
   - 按需加载

3. **提醒优化**
   - 精确时间窗口（±2分钟）
   - 提醒日志去重
   - 批量查询

### 未来优化建议

1. 定期VACUUM清理
2. 分析表统计（ANALYZE）
3. 历史数据归档

## 版本历史

### V2.0 (2026-02-03) - 大规模重构
- ✅ 模块化重构（commands/handlers/services分离）
- ✅ CommandRouter命令路由
- ✅ 统一配置管理（config.py）
- ✅ 消息格式化工具（formatters.py）
- ✅ 待办分类管理（cat:语法）
- ✅ 优先级系统（p:1-4）
- ✅ 笔记标签支持（#tag语法）
- ✅ 日记模板多轮对话
- ✅ 代码质量改进（codereview.md建议）

### V1.1 (2026-01-29) - 安全与可靠性
- ✅ 多用户数据隔离
- ✅ 统一错误处理装饰器
- ✅ JSON字段容错解析
- ✅ RRULE格式验证
- ✅ 操作日志审计

### V1.0 (2026-01-25) - 初始版本
- ✅ 基础功能（日程、待办、笔记、日记）
- ✅ 自然语言解析
- ✅ 提醒系统
- ✅ 全文搜索
- ✅ Markdown导入导出

## 相关文档

- [README.md](README.md) - 用户使用文档
- [plugin.json](plugin.json) - 插件配置清单
- [config.py](config.py) - 配置管理

---

**维护者**: XiaoQing Team
**最后更新**: 2026-02-03
**当前版本**: V2.0
**生产状态**: ✅ 就绪
