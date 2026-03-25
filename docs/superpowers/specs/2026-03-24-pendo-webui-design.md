# Pendo Web UI 设计文档

**日期**: 2026-03-25
**状态**: 已确认（修订版，整合 review 反馈与 design-another 文档）

## 概述

为 pendo 插件设计一个美观的 Web UI，实现数据展示、数据管理和可视化分析三大功能。

**核心决策**：
- 技术栈：FastAPI + 原生 HTML/CSS/JS + Chart.js（无构建工具）
- 风格：彩色活泼但不压迫，大圆角卡片 + 轻阴影 + 微边框，每模块独立主色
- 认证：Token 认证（聊天端生成，Web 端登录）
- 启动：配置项控制自动启动 + 聊天命令手动启停
- 未来：架构为迁移 Vue/Vite 预留，API 层 100% 可复用

---

## 1. 整体架构

### 目录结构

```
plugins/pendo/web/
├── __init__.py
├── server.py              # FastAPI 应用，挂载 API + 静态文件
├── auth.py                # Token 生成/验证（JWT）
├── api/
│   ├── __init__.py        # APIRouter 汇总
│   ├── items.py           # 统一 CRUD: /api/items?type=event
│   ├── stats.py           # GET /api/stats/{type} 统计聚合
│   ├── dashboard.py       # GET /api/dashboard 首页数据聚合
│   ├── search.py          # GET /api/search 全文搜索
│   └── settings.py        # GET/PUT /api/settings
├── deps.py                # 依赖注入（db 实例、当前用户）
└── static/
    ├── index.html         # SPA 入口
    ├── css/
    │   ├── app.css        # 全局样式 + CSS 变量
    │   └── charts.css     # 图表容器样式
    ├── js/
    │   ├── app.js         # SPA 入口 + 初始化
    │   ├── api.js         # fetch 封装（带 Token）
    │   ├── router.js      # hash 路由
    │   ├── store.js       # 全局状态管理
    │   ├── pages/
    │   │   ├── dashboard.js
    │   │   ├── events.js
    │   │   ├── tasks.js
    │   │   ├── ledger.js
    │   │   ├── notes.js
    │   │   ├── diary.js
    │   │   ├── search.js
    │   │   ├── stats.js
    │   │   └── settings.js
    │   ├── components/
    │   │   ├── sidebar.js
    │   │   ├── header.js
    │   │   ├── modal.js
    │   │   ├── toast.js
    │   │   ├── calendar.js
    │   │   └── form.js
    │   └── lib/
    │       └── chart.min.js  # 离线优先，不依赖 CDN
    └── assets/
        └── favicon.ico
```

### API 设计

#### 统一规范

```
认证：Authorization: Bearer <token>
响应格式（成功）：{ "ok": true, "data": ..., "total": N, "message": "" }
响应格式（失败）：{ "ok": false, "message": "错误描述", "error_code": "INVALID_TOKEN" }
分页：?page=1&page_size=20  （响应中包含 total 字段供前端渲染分页控件）
筛选：?range=2026-03-01..2026-03-31&category=餐饮
排序：?sort=created_at&order=desc
```

#### HTTP 状态码

| 状态码 | 含义                     |
|--------|--------------------------|
| 200    | 成功                     |
| 201    | 创建成功                 |
| 401    | Token 无效或已过期       |
| 403    | 无权限（如访问他人数据） |
| 404    | 资源不存在               |
| 422    | 请求参数校验失败         |

#### 统一条目接口（减少样板代码）

采用统一 items 路由，通过 `type` 参数区分模块：

| 方法   | 路径              | 说明                               |
|--------|-------------------|------------------------------------|
| GET    | /api/items        | 列表，?type=task&status=todo&page=1 |
| GET    | /api/items/:id    | 详情                               |
| POST   | /api/items        | 新建（body 中包含 type）           |
| PUT    | /api/items/:id    | 更新                               |
| DELETE | /api/items/:id    | 软删除                             |

#### 其他接口

| 路径                   | 方法     | 说明                                       |
|------------------------|----------|--------------------------------------------|
| /api/auth/verify       | POST     | 校验 token 有效性，返回用户信息            |
| /api/dashboard         | GET      | 首页聚合：今日日程 + 未完成待办 + 消费趋势 |
| /api/search            | GET      | 全文搜索，?q=关键词&type=note              |
| /api/stats/ledger      | GET      | 月度收支对比、分类饼图、日消费折线         |
| /api/stats/tasks       | GET      | 完成率趋势、分类分布、优先级分布           |
| /api/stats/events      | GET      | 每周忙碌度、时间段分布                     |
| /api/settings          | GET/PUT  | 用户设置读写                               |
| /api/config/categories | GET      | 返回各模块可用分类列表（从 config.py 读取）|
| /api/diary/templates   | GET      | 返回可用日记模板及其 prompts               |

### Token 认证流程

1. 用户在聊天中发送 `/pendo web token`
2. 生成 JWT token（包含 owner_id，24h 过期）
3. 用户打开 Web UI，输入 token 登录
4. 前端调用 `POST /api/auth/verify` 验证 token 有效性
5. 验证通过后存入 localStorage，每次请求通过 `Authorization: Bearer <token>` 携带
6. API 层验证 token，提取 owner_id，查询对应数据

SECRET_KEY 随进程启动生成，重启后旧 token 失效。

### 权限控制

| 命令                | 权限     |
|---------------------|----------|
| /pendo web start    | 仅管理员 |
| /pendo web stop     | 仅管理员 |
| /pendo web status   | 仅管理员 |
| /pendo web token    | 所有用户 |

需在 `core/router.py` 的 `COMMAND_META` 中注册 `web` 命令，新建 `handlers/web.py` 处理。

### 启动配置

```python
# config.py 新增
WEB_ENABLED = True
WEB_HOST = "127.0.0.1"       # 默认仅本地，避免意外暴露到公网
WEB_PORT = 8765
WEB_TOKEN_EXPIRE_HOURS = 24
```

### 线程安全策略

Web 服务在后台 daemon thread 中通过 uvicorn 运行。关于数据库访问：

- 现有 `Database` 类使用 `threading.local()` 管理连接，每个线程自动获得独立连接，天然线程安全
- FastAPI 路由函数使用 **`def`（同步函数）** 而非 `async def`，让 FastAPI 自动将其调度到线程池执行，这样每次请求在独立线程中运行，`Database` 的 per-thread 连接机制正好生效
- 不新建 Database 实例，复用插件现有的单例

---

## 2. 页面布局与导航

### 整体布局

```
┌──────────────────────────────────────────────────┐
│  Header（Logo + 页面标题 + 搜索框 + 用户信息）      │
├────────┬─────────────────────────────────────────┤
│        │                                         │
│ Side   │         Main Content Area               │
│ bar    │                                         │
│        │  （根据当前路由渲染对应页面）                │
│ 📊 总览 │                                         │
│ 🗓️ 日程 │                                         │
│ ✅ 待办 │                                         │
│ 💰 记账 │                                         │
│ 📝 笔记 │                                         │
│ 📔 日记 │                                         │
│ 🔍 搜索 │                                         │
│ 📈 统计 │                                         │
│ ──── │                                         │
│ ⚙️ 设置 │                                         │
├────────┴─────────────────────────────────────────┤
│  ＋ 快捷添加浮动按钮（右下角 FAB）                   │
└──────────────────────────────────────────────────┘
```

### 视觉风格

- 背景：暖灰白（`#F9FAFB`），信息密度高但不压迫
- 卡片：大圆角（12px）+ 轻阴影（`0 1px 3px rgba(0,0,0,0.1)`）+ 微边框（`1px solid #E5E7EB`）
- 按钮：主按钮（模块色填充）、次按钮（边框）、幽灵按钮（纯文字）
- 表单：统一 focus ring（模块色）
- 图标：emoji 与纯色标记并存

### 模块色彩方案

| 模块   | 主色      | 色值      |
|--------|-----------|-----------|
| 总览   | 靛蓝      | #6366F1   |
| 日程   | 琥珀      | #F59E0B   |
| 待办   | 翠绿      | #10B981   |
| 记账   | 玫红      | #EF4444   |
| 笔记   | 天蓝      | #3B82F6   |
| 日记   | 紫粉      | #EC4899   |
| 搜索   | 灰色      | #6B7280   |
| 统计   | 紫色      | #8B5CF6   |

### 路由表

| 路由            | 页面       | 核心内容                                   |
|-----------------|------------|--------------------------------------------|
| #/dashboard     | 总览（默认）| 统计卡片 + 今日日程 + 重点待办 + 消费趋势  |
| #/events        | 日程管理   | 月历/周/列表视图，添加编辑 Modal            |
| #/tasks         | 待办管理   | 看板视图（待办/进行中/已完成/已取消），可拖拽 |
| #/ledger        | 记账管理   | 列表 + 顶部汇总卡片 + 快速记账表单          |
| #/notes         | 笔记管理   | 卡片网格，按分类/标签筛选                   |
| #/diary         | 日记管理   | 时间线视图，情绪标注，模板选择              |
| #/search        | 搜索       | 跨类型全文搜索，类型过滤，结果卡片          |
| #/stats         | 统计分析   | Tab 切换：记账/待办/日程                   |
| #/settings      | 设置       | 所有用户设置 + Token 管理                  |

Hash 路由支持参数编码：`#/ledger?range=2026-03&category=餐饮`，用于图表点击跳转等场景。

### 快捷添加按钮（FAB）

右下角浮动按钮，点击展开：🗓️ 新日程 / ✅ 新待办 / 💰 记一笔 / 📝 新笔记 / 📔 写日记

### 响应式

- 桌面 >1024px：侧边栏常驻
- 平板 768-1024px：侧边栏可收起为图标
- 手机 <768px：侧边栏隐藏，hamburger 菜单 + 底部 tab

---

## 3. 数据管理交互

### 通用交互模式

- 所有添加/编辑使用 Modal 弹窗
- 每行末尾操作按钮：编辑 ✏️、删除 🗑️
- 删除确认对话框 → 软删除 + toast "已删除，可撤销"（toast 显示 5 秒，后端实际支持 5 分钟内撤销）
- 批量操作：勾选多行 → 顶部批量操作栏

### 日程表单

| 字段   | 控件                     | 必填 |
|--------|--------------------------|------|
| 标题   | 文本输入                 | *    |
| 开始   | datetime-local           | *    |
| 结束   | datetime-local           |      |
| 地点   | 文本输入                 |      |
| 分类   | 下拉选择（从 /api/config/categories 获取）|      |
| 提醒   | 多选（15分/1时/1天/自定义）|     |
| 重复   | 下拉（不重复/每天/每周/每月/自定义 RRULE）| |
| 备注   | 多行文本                 |      |

### 待办表单

| 字段   | 控件                     | 必填 |
|--------|--------------------------|------|
| 标题   | 文本输入                 | *    |
| 分类   | 下拉 + 可输入新建        |      |
| 优先级 | 4色按钮 🔴🟠🟡🟢       |      |
| 截止   | datetime-local           |      |
| 标签   | 标签输入（回车添加）      |      |
| 备注   | 多行文本                 |      |

### 记账表单

**快速模式**（内嵌列表页顶部）：

```
[支出 ▼]  ¥ [金额]  [摘要]  [分类 ▼]  [添加]
```

字段映射：方向→`direction`，金额→`amount`，摘要→`title`，分类→`ledger_category`。
日期未填时默认为今天（`ledger_date` 由 API 层补全）。

**完整模式**（Modal）额外：日期（`ledger_date`）、备注（`remark`）。

### 笔记表单

标题*（`title`）、内容*（`content`，Markdown）、分类（`category`）、标签（`tags`）。

### 日记表单

日期*（`diary_date`，默认今天）、模板（`template_id`，可选）、内容*（`content`）、心情（`mood`）、天气（`weather`）、地点（`location`）。

选择模板后，前端从 `/api/diary/templates` 获取对应 prompts 预填到内容区。API 接收 `template_id` 存入记录。

### 日程月历视图

月历网格，有日程的日期显示圆点标记。点击日期展开当日日程列表，点击空白可新建（预填日期）。

### 待办看板视图

四列看板：**待办 | 进行中 | 已完成 | 已取消**。已取消列默认折叠，可展开。
卡片显示优先级色标 + 标题 + 分类 + 截止时间。拖拽卡片改变状态。

### 记账列表视图

顶部三张汇总卡片（收入/支出/结余），快速记账表单，列表按日期分组显示。

### 搜索页

- 顶部搜索框（与 Header 中的搜索框联动）
- 类型过滤 Tab：全部 / 日程 / 待办 / 记账 / 笔记 / 日记
- 结果卡片展示：类型图标 + 标题 + 摘要 + 时间
- 调用 `/api/search?q=关键词&type=note`，复用现有 FTS5 搜索能力

---

## 4. 可视化统计

### 统计页面结构

顶部 Tab：**记账** | **待办** | **日程**

每个 Tab 有日期范围选择器：本周 | 本月 | 本季 | 本年 | 自定义

### 记账统计

| 图表             | 类型   | 说明                       |
|------------------|--------|----------------------------|
| 月度收支对比     | 柱状图 | X轴月份，双色柱（收入/支出）|
| 支出分类分布     | 饼图   | 点击扇区展开明细           |
| 收入分类分布     | 饼图   | 同上                       |
| 日消费趋势       | 折线图 | 当月每日支出，悬停显示明细 |

关键聚合 SQL：

```sql
-- 月度收支对比
SELECT strftime('%Y-%m', ledger_date) AS month, direction, SUM(amount) AS total
FROM items WHERE type='ledger' AND owner_id=? AND deleted=0
  AND ledger_date BETWEEN ? AND ?
GROUP BY month, direction ORDER BY month

-- 分类饼图
SELECT ledger_category, direction, SUM(amount) AS total
FROM items WHERE type='ledger' AND owner_id=? AND deleted=0
  AND ledger_date BETWEEN ? AND ?
GROUP BY ledger_category, direction

-- 日消费折线
SELECT ledger_date, SUM(amount) AS total
FROM items WHERE type='ledger' AND direction='expense' AND owner_id=? AND deleted=0
  AND ledger_date BETWEEN ? AND ?
GROUP BY ledger_date ORDER BY ledger_date
```

### 待办统计

| 图表             | 类型   | 说明                         |
|------------------|--------|------------------------------|
| 顶部数据卡片     | 数字   | 总任务/已完成/完成率/本周新增 |
| 完成率趋势       | 折线图 | 按周统计完成率               |
| 分类分布         | 饼图   | 各分类占比                   |
| 优先级分布       | 环形图 | 四个优先级占比               |

关键聚合 SQL：

```sql
-- 完成率按周
SELECT strftime('%Y-W%W', created_at) AS week,
  COUNT(*) AS total,
  SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) AS done
FROM items WHERE type='task' AND owner_id=? AND deleted=0
  AND created_at BETWEEN ? AND ?
GROUP BY week ORDER BY week

-- 优先级分布
SELECT priority, COUNT(*) AS count
FROM items WHERE type='task' AND owner_id=? AND deleted=0
GROUP BY priority
```

### 日程统计

| 图表             | 类型       | 说明                       |
|------------------|------------|----------------------------|
| 每周忙碌度       | 柱状图     | 过去 8 周，Y轴日程数量     |
| 时间段分布       | 横向柱状图 | 6个时间段忙碌占比          |
| 分类分布         | 饼图       | 各分类日程占比             |

关键聚合 SQL：

```sql
-- 每周忙碌度
SELECT strftime('%Y-W%W', start_time) AS week, COUNT(*) AS count
FROM items WHERE type='event' AND owner_id=? AND deleted=0
  AND start_time BETWEEN ? AND ?
GROUP BY week ORDER BY week

-- 时间段分布
SELECT CASE
  WHEN CAST(strftime('%H', start_time) AS INT) BETWEEN 6 AND 8 THEN '06-09'
  WHEN CAST(strftime('%H', start_time) AS INT) BETWEEN 9 AND 11 THEN '09-12'
  WHEN CAST(strftime('%H', start_time) AS INT) BETWEEN 12 AND 13 THEN '12-14'
  WHEN CAST(strftime('%H', start_time) AS INT) BETWEEN 14 AND 17 THEN '14-18'
  WHEN CAST(strftime('%H', start_time) AS INT) BETWEEN 18 AND 20 THEN '18-21'
  ELSE '21-24'
END AS time_slot, COUNT(*) AS count
FROM items WHERE type='event' AND owner_id=? AND deleted=0
GROUP BY time_slot
```

### 图表交互

- 悬停：tooltip 显示具体数值
- 点击饼图扇区：展开该分类明细列表
- 点击柱状图：通过 hash 参数跳转到对应时间段列表视图（如 `#/ledger?range=2026-03`）
- 图表右上角：下载为 PNG

### Dashboard 迷你图表

首页"本月消费趋势"为精简版折线图（无坐标轴标签），点击跳转完整统计页。
Dashboard 采用"概览优先"设计：统计卡片（今日日程数/未完成待办数/近7天账目笔数/近30天日记数）+ 实际数据列表，迷你图表作为补充。

---

## 5. 技术实现

### 后端

- **复用 `services/db.py`**：API 层通过依赖注入获取现有 DatabaseService 单例
- **同步路由函数**：所有 FastAPI 路由使用 `def`（非 `async def`），由 FastAPI 调度到线程池，配合 Database 的 per-thread 连接机制
- **分页支持**：`get_items()` 已支持 `limit/offset`，API 层额外执行一次 `SELECT COUNT(*)` 查询返回 `total` 字段
- **统计接口**：新写聚合 SQL（见第 4 节）
- **Web 服务**：uvicorn 在 daemon thread 中运行
- **JWT**：SECRET_KEY 随进程生成，PyJWT 库编解码

### 前端

- **路由**：hash 路由，`window.addEventListener('hashchange')`，支持参数
- **模块化**：ES Modules，每个 page 导出 `render(container)`、`destroy()`、`onRouteEnter(params)` —— 与 Vue 组件生命周期对齐，便于迁移
- **API 封装**：统一 fetch wrapper，自动带 Token，统一错误处理（401 自动跳转登录）
- **图表**：Chart.js 4.x，离线优先（从 `lib/chart.min.js` 加载，不依赖 CDN）
- **拖拽**：原生 Drag & Drop API（待办看板）
- **日期选择**：原生 `<input type="datetime-local">`
- **分类数据**：从 `/api/config/categories` 动态获取，不硬编码在前端

### 第三方依赖

仅 Chart.js + PyJWT，无其他依赖。无需 Node.js 或构建工具。

### 设置页覆盖范围

设置页应展示现有 `user_settings` 的所有字段：
- timezone（时区）
- quiet_hours_start / quiet_hours_end（静默时段）
- daily_report_time（每日简报时间）
- diary_remind_time（日记提醒时间）
- default_category（默认分类）
- reminder 开关、daily_report 开关、privacy 模式
- Web 登录说明与 token 使用指引

### CORS 说明

前端与 API 由同一个 FastAPI 服务提供（静态文件挂载），同源访问，无需 CORS 配置。开发阶段如果前端单独起服务器，可临时添加 CORSMiddleware。

### Vue 迁移路径

```
现在                          迁移后
static/js/pages/*.js       → src/views/*.vue
static/js/components/*.js  → src/components/*.vue
static/js/api.js           → src/api/index.ts
static/js/router.js        → vue-router
static/js/store.js         → pinia
web/api/                   → 完全不动
```

页面模块接口 `render/destroy/onRouteEnter` 对应 Vue 的 `mounted/unmounted/beforeRouteEnter`，迁移为机械替换。API 层 100% 复用，前端替换 static/ 目录即可。
