# Pendo - 个人时间与信息管理中枢

> 在聊天场景里完成记录、查询、提醒与复盘，把日程、待办、笔记、日记汇总到同一套体系里。

[![Python](https://img.shields.io/badge/python-3.8+-blue.svg)](https://python.org)

## 🎉 最新更新 (V2.0)

**大规模重构完成！**

- ✅ **模块化架构** - commands/handlers/services分离，代码更清晰
- ✅ **按需AI解析** - 仅日程使用AI，待办/笔记/日记采用规则解析
- ✅ **待办分类管理** - 支持 `cat:日期` 或 `cat:自定义分类` 进行分组
- ✅ **优先级系统** - 待办支持 `p:1` (紧急) 到 `p:4` (低) 四级优先级
- ✅ **笔记标签** - 支持 `#标签` 和 `cat:分类` 语法
- ✅ **日记模板** - 多轮对话式模板写日记
- ✅ **代码质量** - 统一错误处理、消息格式化工具、完善文档

## 📑 目录

- [快速开始](#快速开始)
- [命令速览](#命令速览)
- [日程管理](#日程管理)
- [待办管理](#待办管理)
- [笔记管理](#笔记管理)
- [日记管理](#日记管理)
- [搜索](#搜索)
- [提醒操作](#提醒操作)
- [导入导出](#导入导出)
- [设置](#设置)
- [常见问题](#常见问题)

## 快速开始

```bash
/pendo help                 # 查看帮助
```

### 命令速览

| 模块 | 常用命令 |
|------|----------|
| 日程 | `/pendo event add 明天9点开会` |
| 待办 | `/pendo todo add 写报告 cat:工作 p:2` |
| 笔记 | `/pendo note add 今天学到的知识 #学习` |
| 日记 | `/pendo diary` |
| 搜索 | `/pendo search 关键词` |

### 核心特性

- 📅 **日程管理** - AI解析自然语言，智能识别时间、地点、提醒
- ✅ **待办管理** - 分类和优先级管理，无需AI
- 📝 **笔记管理** - 标签和分类支持，无需AI
- 📔 **日记管理** - 模板式记录，情绪分析
- 🔍 **全文搜索** - 快速找到所有信息
- 🤖 **AI增强** - 可选AI解析，提升理解准确度
- 🔄 **重复事件** - 支持复杂的重复规则
- 📅 **每日简报** - 自动推送今日日程和待办

## 日程管理

### 创建日程

**自然语言创建（推荐）**:

```
/pendo event add 明天9点开会
/pendo event add 明天14:00-16:00 产品评审会 @会议室A
/pendo event add 每周一早上9点站会
/pendo event add 每月18号下午3点例会，重复12次
/pendo event add 明天9点开会，提前1小时和15分钟提醒
```

**智能识别**:
- 时间: 明天、下周三、每周一、每月18号
- 时间范围: 9点-11点、14:00-16:00
- 地点: @会议室A、地点A
- 重复规则: 每天、每周、每月、重复N次
- 提醒时间: 提前30分钟、提前1小时和1天

### 查看日程

```
/pendo event today          # 今天的日程
/pendo event tomorrow       # 明天的日程
/pendo event week           # 本周的日程
/pendo event month          # 本月的日程
/pendo event 2026-02        # 指定月份
/pendo event 2026-02-01..2026-02-14  # 指定日期范围
```

### 编辑/删除日程

```
/pendo event edit <id> 改到明天10点
/pendo event delete <id>    # 删除日程（5分钟内可撤销）
```

## 待办管理

### 创建待办

**语法**: `/pendo todo add <内容> [cat:分类] [p:1-4] [#标签]`

```
/pendo todo add 写报告 cat:工作 p:2
/pendo todo add 买牛奶 cat:生活 p:4
/pendo todo add 提交报销 #财务 p:1
```

**优先级**:
- `p:1` - 🔴紧急
- `p:2` - 🟠高
- `p:3` - 🟡中（默认）
- `p:4` - 🟢低

**分类**:
- 默认添加到当天分类（如 `cat:2026-02-03`）
- 可使用自定义分类（如 `cat:工作`、`cat:学习`）

### 查看待办

```
/pendo todo                  # 列出所有分类
/pendo todo today           # 今日待办快捷方式
/pendo todo list 2026-02-03  # 查看指定日期
/pendo todo list 工作 done   # 工作分类已完成
/pendo todo list 生活 undone # 生活分类未完成
```

### 管理

```
/pendo todo done <id>        # 完成待办
/pendo todo undone <id>      # 重开待办
/pendo todo delete <id>      # 删除单个待办
/pendo todo delete cat:工作  # 删除整个分类
/pendo todo edit <id> 新内容  # 编辑待办
```

## 笔记管理

### 创建笔记

**语法**: `/pendo note add <内容> [cat:分类] [#标签]`

```
/pendo note add 直接折叠找脉冲星 cat:工作 #文章
/pendo note add 会议记录：今天讨论了三个要点 #会议
```

### 查看笔记

```
/pendo note list             # 查看所有笔记
/pendo note list cat:工作    # 按分类筛选
/pendo note view <id>        # 查看详情
```

### 删除笔记

```
/pendo note delete <id>      # 删除单个笔记
/pendo note delete cat:工作  # 删除整个分类下的笔记
```

## 日记管理

### 写日记

**直接写日记**:
```
/pendo diary add 今天天气很好，心情不错...
```

**使用模板**:
```
/pendo diary                 # 显示模板列表
/pendo diary three_good      # 使用"三件好事"模板
```

**内置模板**:
- `default` - 自由日记
- `three_good` - 三件好事
- `summary` - 今日总结
- `mood` - 情绪记录

### 查看日记

```
/pendo diary view             # 查看今天的日记
/pendo diary view 2026-01-31  # 查看指定日期
/pendo diary list             # 最近30天日记
/pendo diary list month       # 本月日记
```

### 删除日记

```
/pendo diary delete 2026-01-31  # 删除指定日期的日记
```

## 搜索

### 全文搜索

```
/pendo search 报销
/pendo search 会议
/pendo search 项目方案
```

### 高级搜索

**按类型**:
```
/pendo search 会议 type=event
/pendo search 报销 type=task
/pendo search 知识 type=note
```

**按时间范围**:
```
/pendo search 项目 range=last7d
/pendo search 日记 range=2026-01
/pendo search 记录 range=2026-01-01..2026-01-31
```

**组合搜索**:
```
/pendo search 会议 type=event range=last7d
```

## 提醒操作

### 确认提醒

```
/pendo confirm <id>          # 确认已收到提醒
```

### 延后提醒

```
/pendo snooze <id> 10m       # 延后10分钟
/pendo snooze <id> 1h        # 延后1小时
/pendo snooze <id> 19:00     # 延后到19:00
```

### 撤销删除

```
/pendo undo                  # 撤销最近5分钟内的删除
/pendo undo 10               # 撤销10分钟内的删除
```

## 导入导出

### 导出Markdown

```
/pendo export md                              # 导出所有数据
/pendo export md range=last30d                # 导出最近30天
/pendo export md range=2026-01-01..2026-01-31 # 指定范围
/pendo export md format=by_type               # 按类型分文件
```

导出文件保存到 `plugins/pendo/data/exports/<user_id>/`

### 导入Markdown

```
/pendo import md              # 进入导入模式
/pendo import md preview      # 预览导入内容
```

## 设置

### 查看设置

```
/pendo settings               # 查看当前所有设置
```

### 修改设置

```
/pendo settings reminder on/off          # 开关提醒
/pendo settings timezone Asia/Shanghai   # 设置时区
/pendo settings quiet_hours 23:00-07:00 # 设置静默时段
/pendo settings privacy on/off          # 开关隐私模式
```

## 安装配置

### 1. 安装依赖

```bash
cd plugins/pendo
pip install -r requirements.txt
```

### 2. 必需依赖

```txt
jieba>=0.42.1
PyYAML>=6.0
python-dateutil>=2.8.2
```

### 3. 可选AI功能

如需使用AI自然语言解析，在 `config/secrets.json` 中配置：

```json
{
  "plugins": {
    "pendo": {
      "api_base": "https://api.openai.com/v1",
      "api_key": "your-api-key",
      "model": "gpt-3.5-turbo"
    }
  }
}
```

**注意**: AI功能是可选的，不配置也能正常使用规则解析。

## 定时任务

插件会自动执行以下定时任务（在 `plugin.json` 中配置）：

| 任务 | 时间 | 说明 |
|------|------|------|
| pendo_reminders | 每分钟 | 检查并发送提醒 |
| pendo_daily_briefing | 每天8:00 | 推送每日简报 |
| pendo_diary_reminder | 每天21:30 | 提醒写日记 |

## 常见问题

**Q: 如何修改已创建的条目？**
- 日程: `/pendo event edit <id> <修改内容>`
- 待办: `/pendo todo edit <id> <新内容>`

**Q: 提醒没有收到？**
- 检查提醒是否开启: `/pendo settings`
- 检查是否在静默时段
- 确认条目设置了提醒时间

**Q: 如何备份数据？**
- 使用导出功能: `/pendo export md`
- 或直接复制 `data/pendo.db` 文件

**Q: 支持多用户吗？**
- 支持，每个用户的数据完全隔离

**Q: 群聊中如何保护隐私？**
- 默认长消息自动转私聊
- 可通过 `/pendo settings privacy on` 强制私聊

## 技术架构

### 目录结构

```
plugins/pendo/
├── main.py             # 插件入口
├── config.py           # 配置管理
├── core/               # 核心组件（路由器、异常）
├── models/             # 数据模型
├── handlers/           # 业务处理器
├── services/           # 核心服务
├── commands/           # 命令处理
├── utils/              # 工具模块
└── data/               # 数据存储
```

### 设计特点

1. **统一数据模型** - 所有条目共享Item表结构
2. **按需AI解析** - 仅日程使用AI，其他模块规则解析
3. **多用户隔离** - 所有查询基于owner_id
4. **软删除支持** - 5分钟撤销窗口
5. **全文搜索** - SQLite FTS5

详细架构说明请参考 [ARCHITECTURE.md](ARCHITECTURE.md)

## 更新日志

### V2.0 (2026-02-03)

- 模块化重构（commands/handlers/services分离）
- CommandRouter命令路由
- 统一配置管理
- 消息格式化工具
- 待办分类管理
- 优先级系统
- 笔记标签支持
- 日记模板多轮对话
- 代码质量改进

### V1.1 (2026-01-29)

- 多用户数据隔离
- 统一错误处理
- JSON字段容错解析
- 操作日志审计

### V1.0 (2026-01-25)

- 初始版本发布
- 支持日程、待办、笔记、日记管理
- 自然语言解析、智能提醒、全文搜索

## 许可证

MIT License

---

**Pendo - 让时间管理更简单** 🎯
