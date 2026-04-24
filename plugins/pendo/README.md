# Pendo - 个人时间与信息管理中枢

> 在聊天场景里完成记录、查询、提醒与复盘，把日程、待办、笔记、日记汇总到同一套体系里。

[![Python](https://img.shields.io/badge/python-3.8+-blue.svg)](https://python.org)

## 🎉 最新更新 (V3.0)

**Web UI 全面上线！**

- ✅ **Web 控制台** - 基于 FastAPI + 原生 JS 的单页应用，支持浏览器访问
- ✅ **JWT 鉴权** - 安全的 Token 登录机制，支持多用户会话
- ✅ **可视化看板** - Dashboard 汇总待办、事件、账本、笔记核心数据
- ✅ **账本管理** - 支持收支分类、筛选、分页、快速录入
- ✅ **统计图表** - Chart.js 可视化事件/任务/账本趋势
- ✅ **全模块页面** - 任务、事件、日记、笔记、搜索、设置均有独立页面
- ✅ **数据迁移** - Web 端支持 `.pendo.zip` Bundle 格式的高级导入导出与操作审计
- ✅ **聊天命令集成** - `/pendo web start|stop|status` 在聊天中管理 Web 服务
- ✅ **iPhone 小组件** - 提供只读 widget API、Scriptable 脚本和专用 widget token

## 🎉 V2.0 历史更新

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
- [Web 控制台](#web-控制台)
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
/pendo                      # 查看完整帮助总览（按模块分组）
/pendo event                # 直达 event 模块帮助
/pendo help                 # 查看帮助
```

> `/pendo` 现在会按模块显示带 emoji 的导航式帮助；输入 `/pendo <模块>` 可以只看对应模块的命令。

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

## Web 控制台

### 启动/停止服务

```
/pendo web start             # 启动 Web 服务（默认端口 8765）
/pendo web stop              # 停止服务
/pendo web status            # 查看运行状态和访问地址
/pendo web token             # 生成网页登录 token
/pendo web widget-token      # 生成 Scriptable 小组件 token
```

> 提示：`pendo` 插件初始化时会尝试自动拉起 Web 服务；如果当时启动失败，后续仍可手动执行 `/pendo web start` 重试。

### 登录访问

默认本地地址是 `http://127.0.0.1:8765`。如果你做了反向代理，也可以通过自己的外网地址访问（例如 `https://example.com/pendo/`）。

如需修改监听地址或端口，请在启动主进程前设置环境变量：

```text
# PowerShell
$env:PENDO_WEB_HOST="127.0.0.1"
$env:PENDO_WEB_PORT="8766"
python main.py

# bash
PENDO_WEB_HOST=127.0.0.1
PENDO_WEB_PORT=8766
python main.py
```

在 Windows 上，如果 `/pendo web start` 提示端口绑定失败，但 `netstat -ano` 看不到 `8765` 被占用，通常不是已有进程监听，而是系统拒绝绑定该端口（例如保留端口范围、Hyper-V / WSL / Docker 或安全策略影响）。这种情况下优先换一个端口，例如 `PENDO_WEB_PORT=8766`。

公开 demo 会话默认关闭。只有在显式设置 `PENDO_WEB_DEMO_ENABLED=1` 时，才会开放临时演示空间。

网页登录流程：

1. 执行 `/pendo web start`
2. 打开浏览器访问 Web 地址
3. 执行 `/pendo web token`
4. 将 token 粘贴到登录页完成登录

### iPhone / Scriptable 小组件

Pendo 提供了专用的只读 widget 摘要接口：

- `GET /api/widget/summary`
- 支持 `section=tasks|ledger|notes|auto`
- `auto` 会按小时轮换 `tasks -> ledger -> notes`
- widget token 只能访问 `/api/widget/*` 的 `GET` 请求

生成小组件 token：

```text
/pendo web widget-token
```

Scriptable 脚本位于：

- `plugins/pendo/web/scriptable/pendo_widget.js`

脚本仓库版本不再包含真实地址和 token，默认是安全占位值。导入 Scriptable 后，请先把文件头部的：

- `BASE_URL`
- `TOKEN`

替换成你自己的 Pendo Web 地址和 `/pendo web widget-token` 生成的只读 token。

配套说明见：

- `docs/pendo-scriptable-widget.md`

当前脚本的摘要行为：

- 左侧日程显示未来 30 天内的事件，最多 5 条
- 右侧显示待办 / 财务 / 笔记摘要，最多 5 条
- 支持 `small` / `medium` / `large` 三种 iOS 小组件尺寸
- `medium` 与 `large` 共用同一套视觉语言，`large` 显示更多细节

### 优雅关闭

- `Ctrl+C` 停止 `main.py` 时，XiaoQing 会走插件卸载流程，Pendo Web 会先请求 uvicorn 优雅退出，再清理数据库和运行时状态
- 手动执行 `/pendo web stop` 也会走同一套停止逻辑

### 页面功能

| 页面 | 功能 |
|------|------|
| Dashboard | 核心数据汇总（待办、事件、账本余额、最近笔记） |
| 任务 | Kanban 看板，按优先级拖拽管理待办 |
| 事件 | 日历视图，查看/添加日程 |
| 账本 | 收支记录、分类筛选、余额统计 |
| 日记 | 时间线视图，按日期浏览日记 |
| 笔记 | 卡片网格，按分类/标签浏览笔记 |
| 搜索 | 跨模块全文搜索 |
| 统计 | Chart.js 可视化图表（事件/任务/账本趋势） |
| 设置 | 在线修改配置、**高级数据迁移（导入/导出 Bundle）** |

### Web 依赖

```bash
pip install fastapi uvicorn PyJWT passlib[bcrypt]
```

如果你是从仓库根目录安装 `requirements.txt`，这些依赖已经包含在内，通常不需要额外安装。

## 日程管理

### 创建日程

**自然语言创建（推荐）**:

```
/pendo event add 明天9点开会
/pendo event add 明天14:00-16:00 产品评审会 @会议室A
/pendo event add 每周一早上9点站会
/pendo event add 每月18号下午3点例会，重复12次
/pendo event add 明天9点开会，提前1小时和15分钟提醒
/pendo event add 4月6日注册截止，4月22日会议开始，4月26日会议结束
```

**智能识别**:
- 时间: 明天、下周三、每周一、每月18号
- 时间范围: 9点-11点、14:00-16:00
- 地点: @会议室A、地点A
- 重复规则: 每天、每周、每月、重复N次
- 多节点: AI 会把多个具名时间点创建为一个事件集合，每个节点都是可独立查看、编辑、删除、设置提醒的 leaf 日程
- 提醒时间: 提前30分钟、提前1小时和1天

**事件结构**:

- 单次日程: 一条 `event` leaf。
- 重复日程: 一个 `event_collections(kind=recurring)` 集合，加若干 `recurring_occurrence` leaf。
- 多节点日程: 一个 `event_collections(kind=multi_node)` 集合，加若干 `multi_node_child` leaf。
- 提醒规则保存在 `reminder_rules`，`remind_times` 是按 leaf 开始时间计算出的发送缓存。

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
/pendo event edit <collection_id> 标题改为星团会议
/pendo event edit <leaf_id> 改到4月22日12:43
/pendo event edit <leaf_id> 备注从北京南坐G123去会场
/pendo event delete <id>    # 删除日程（5分钟内可撤销）
```

`collection_id` 用于编辑/删除重复或多节点集合整体；`leaf_id` 用于操作某一次重复实例或某个多节点节点。删除集合会级联删除子节点，删除单个 leaf 只影响该节点。

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
/pendo todo view <id>       # 查看待办详情
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
**显式标题语法**:
- `/pendo note add title:<标题> content <正文> [cat:分类] [#标签]`
- `/pendo note add title:<标题>` 后直接换行写正文，最后一行可接 `cat:分类 #标签`

```
/pendo note add 直接折叠找脉冲星 cat:工作 #文章
/pendo note add title:我的标题 content 这里是详细的长篇正文内容... cat:工作 #学习
/pendo note add title:会议纪要
1. 事项A
2. 事项B
cat:其他 #记录
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
/pendo diary view 82d34407    # 按ID查看某篇日记
/pendo diary delete 82d34407  # 按ID删除日记
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

### 确认 / 提前确认提醒

```
/pendo confirm <id>          # 确认刚收到的那一条提醒
/pendo event reminders confirm <id> today    # 提前确认今天未发送的提醒
/pendo event reminders confirm <id> future   # 提前确认未来全部未发送提醒
/pendo event reminders confirm <id> all      # 提前确认全部未发送提醒
/pendo event reminders confirm <id> 04-18 13:50  # 提前确认某一条指定提醒
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

Pendo 现在将聊天端导出收敛为单文件 Markdown 档案，导入能力保留在 Web 端 Bundle 流程中。

### 聊天端 Markdown 导出

```
/pendo export 我的档案
/pendo export 工作回顾 last30d event,todo
/pendo export 账本快照 2026-03 ledger
/pendo export 本月随笔 month note,diary
```

- 命令格式：`/pendo export <文件名> [范围] [类型]`
- 范围支持：`all`、`today`、`week`、`month`、`YYYY-MM`、`last7d`、`start..end`
- 类型支持：`event`、`todo`、`note`、`ledger`、`diary`，可用逗号组合
- 导出结果会写入 `plugins/pendo/data/exports/<user_id>/`，并通过 OneBot 私聊文件消息发送给当前 QQ 用户
- 导出的 Markdown 采用单文件档案格式，带摘要、目录、分类型章节和结构化元信息，适合归档与分享

### Web 端数据迁移 (Pendo Bundle)

Web 控制台支持跨设备的 `.pendo.zip` 数据包安全迁移：
- **安全校验**: 支持预览与完整的数据格式校验。
- **冲突策略**: 支持自定义冲突处理（跳过现有记录、强制覆盖、创建副本保留双份）。
- **审计记录**: 在数据库中留存完整的导入与导出操作日志（含条数结果）。

### 历史数据转换工具

提供内置 Python 脚本，可将旧版传统纯文本格式导出件（如普通文本备份）转换为标准 `.pendo.zip` 以通过 Web 端导入：

```bash
python plugins/pendo/scripts/convert_text_export_to_pendo_bundle.py 你的文本备份.txt -o my_data.pendo.zip
```

### Event Graph 数据库迁移

旧版数据库中，多节点事件存储在单条 `items.milestones` 中，重复事件通过 `parent_id` 聚合。新版统一为 `event_collections` + leaf events：

```bash
python -m plugins.pendo.scripts.migrate_event_graph plugins/pendo/data/pendo.db --dry-run
python -m plugins.pendo.scripts.migrate_event_graph plugins/pendo/data/pendo.db --apply
```

`--dry-run` 只输出计数，不写数据库。`--apply` 会先在原目录生成备份，再写入 JSON 迁移报告。迁移后，旧多节点容器会软删除，节点以 `<collection_id>_m01` 形式成为可单独 CRUD 的日程；旧重复实例会挂到 `event_collections(kind=recurring)` 下。

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
      "model": "gpt-4o-mini"
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
| pendo_daily_briefing | 每分钟检查 | 用户可自定义本地简报时间，命中后推送 |
| pendo_diary_reminder | 每分钟检查 | 用户可自定义本地提醒时间，命中后推送 |

## 常见问题

**Q: 如何修改已创建的条目？**
- 日程: `/pendo event edit <id> <修改内容>`
- 多节点事件先 `/pendo event view <collection_id>` 查看节点 id，再用 `/pendo event edit <leaf_id> <修改内容>` 改某个节点
- 待办: `/pendo todo edit <id> <新内容>`

**Q: 提醒没有收到？**
- 检查提醒是否开启: `/pendo settings`
- 检查是否在静默时段
- 确认条目设置了提醒时间

**Q: 如何让今天还没发出的提醒不要再发？**
- 用 `/pendo event reminders confirm <id> today`
- 如果要一次性跳过后续全部提醒，用 `/pendo event reminders confirm <id> future`

**Q: 如何备份数据？**
- 使用设置页面的 Web 端导出功能
- 或保存聊天端 `/pendo export <文件名>` 导出的 Markdown 档案
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
├── web/                # Web 控制台
│   ├── main.py         # FastAPI 应用入口
│   ├── auth.py         # JWT 鉴权
│   ├── deps.py         # 依赖注入
│   ├── api/            # REST API 路由
│   ├── analytics/      # 数据聚合（Dashboard/统计）
│   ├── scriptable/     # iPhone Scriptable 小组件脚本
│   └── static/         # 前端静态资源（HTML/CSS/JS）
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

### V3.0 (2026-03-27)

- Web 控制台（FastAPI + 原生 JS SPA）
- JWT 登录鉴权，多用户会话隔离
- Dashboard / 任务 / 事件 / 账本 / 日记 / 笔记 / 搜索 / 统计 / 设置 九大页面
- Chart.js 可视化统计图表
- 支持通过 Web 端完成全量 `.pendo.zip` 数据 Bundle 安全导入与导出、迁移审计
- 添加历史文本备份转换脚本 `convert_text_export_to_pendo_bundle.py`
- 优化部分底层模块设计，提升健壮性
- `/pendo web` 聊天命令集成
- Scriptable 小组件摘要接口 `/api/widget/summary`
- `/pendo web widget-token` 只读小组件令牌
- 新增 `plugins/pendo/web/scriptable/pendo_widget.js`
- 优化聊天端命令冗余调用提示机制
- 账本页 UX 重设计（筛选、排序、分页、快速录入）

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
