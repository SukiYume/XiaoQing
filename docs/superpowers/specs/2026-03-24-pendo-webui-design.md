# Pendo Web UI 设计文档

**日期**: 2026-03-24
**状态**: 已确认

## 概述

为 pendo 插件设计一个美观的 Web UI，实现数据展示、数据管理和可视化分析三大功能。

**核心决策**：
- 技术栈：FastAPI + 原生 HTML/CSS/JS + Chart.js（无构建工具）
- 风格：彩色活泼，每个模块有自己的主色调
- 认证：Token 认证（聊天端生成，Web 端登录）
- 启动：配置项控制自动启动 + 聊天命令手动启停
- 未来：架构为迁移 Vue/Vite 预留，API 层 100% 可复用

---

## 1. 整体架构

### 目录结构

```
plugins/pendo/web/
├── server.py              # FastAPI 应用，挂载 API + 静态文件
├── auth.py                # Token 生成/验证（JWT）
├── api/
│   ├── __init__.py        # APIRouter 汇总
│   ├── events.py          # GET/POST/PUT/DELETE /api/events
│   ├── tasks.py           # GET/POST/PUT/DELETE /api/tasks
│   ├── ledger.py          # GET/POST/PUT/DELETE /api/ledger
│   ├── notes.py           # GET/POST/PUT/DELETE /api/notes
│   ├── diary.py           # GET/POST/PUT/DELETE /api/diary
│   ├── stats.py           # GET /api/stats/{type} 统计聚合
│   └── dashboard.py       # GET /api/dashboard 首页数据聚合
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
    │       └── chart.min.js
    └── assets/
        └── favicon.ico
```

### API 统一规范

```
认证：Authorization: Bearer <token>
响应格式：{ "ok": true, "data": ..., "message": "..." }
分页：?page=1&page_size=20
筛选：?range=2026-03-01..2026-03-31&category=餐饮
排序：?sort=created_at&order=desc
```

**CRUD 路由模式**（所有模块一致）：

| 方法   | 路径              | 说明     |
|--------|-------------------|----------|
| GET    | /api/{module}     | 列表     |
| GET    | /api/{module}/:id | 详情     |
| POST   | /api/{module}     | 新建     |
| PUT    | /api/{module}/:id | 更新     |
| DELETE | /api/{module}/:id | 软删除   |

**聚合接口**：

| 路径                | 说明                                     |
|---------------------|------------------------------------------|
| /api/dashboard      | 今日日程 + 未完成待办 + 本月消费趋势       |
| /api/stats/ledger   | 月度收支对比、分类饼图、日消费折线         |
| /api/stats/tasks    | 完成率趋势、分类分布、优先级分布           |
| /api/stats/events   | 每周忙碌度、时间段分布                     |

### Token 认证流程

1. 用户在聊天中发送 `/pendo web token`
2. 生成 JWT token（包含 owner_id，24h 过期）
3. 用户打开 Web UI，输入 token 登录
4. 前端存储 token 到 localStorage，每次请求带上
5. API 层验证 token，提取 owner_id，查询对应数据

SECRET_KEY 随进程启动生成，重启后旧 token 失效。

### 权限控制

| 命令                | 权限     |
|---------------------|----------|
| /pendo web start    | 仅管理员 |
| /pendo web stop     | 仅管理员 |
| /pendo web status   | 仅管理员 |
| /pendo web token    | 所有用户 |

### 启动配置

```python
# config.py 新增
WEB_ENABLED = True
WEB_HOST = "0.0.0.0"
WEB_PORT = 8765
```

Web 服务在后台线程启动（daemon thread），不阻塞插件主线程。

---

## 2. 页面布局与导航

### 整体布局

```
┌──────────────────────────────────────────────────┐
│  Header（Logo + 页面标题 + 用户信息 + 退出）        │
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
│ 📈 统计 │                                         │
│ ──── │                                         │
│ ⚙️ 设置 │                                         │
├────────┴─────────────────────────────────────────┤
│  ＋ 快捷添加浮动按钮（右下角 FAB）                   │
└──────────────────────────────────────────────────┘
```

### 模块色彩方案

| 模块   | 主色      | 色值      |
|--------|-----------|-----------|
| 总览   | 靛蓝      | #6366F1   |
| 日程   | 琥珀      | #F59E0B   |
| 待办   | 翠绿      | #10B981   |
| 记账   | 红色      | #EF4444   |
| 笔记   | 蓝色      | #3B82F6   |
| 日记   | 粉色      | #EC4899   |
| 统计   | 紫色      | #8B5CF6   |

### 路由表

| 路由          | 页面       | 核心内容                               |
|---------------|------------|----------------------------------------|
| #/dashboard   | 总览（默认）| 今日日程时间线 + 未完成待办 + 消费趋势  |
| #/events      | 日程管理   | 月历/周/列表视图，添加编辑 Modal        |
| #/tasks       | 待办管理   | 看板视图（TODO/进行中/已完成），可拖拽   |
| #/ledger      | 记账管理   | 列表 + 顶部汇总卡片 + 快速记账表单      |
| #/notes       | 笔记管理   | 卡片网格，按分类/标签筛选               |
| #/diary       | 日记管理   | 时间线视图，情绪标注，模板选择          |
| #/stats       | 统计分析   | Tab 切换：记账/待办/日程               |
| #/settings    | 设置       | 时区、Token、主题                      |

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
- 删除确认对话框 → 软删除 + toast "已删除，5秒内可撤销"
- 批量操作：勾选多行 → 顶部批量操作栏

### 日程表单

| 字段   | 控件                     | 必填 |
|--------|--------------------------|------|
| 标题   | 文本输入                 | *    |
| 开始   | datetime-local           | *    |
| 结束   | datetime-local           |      |
| 地点   | 文本输入                 |      |
| 分类   | 下拉选择                 |      |
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

**完整模式**（Modal）额外：日期、备注。

### 笔记表单

标题*、内容*（Markdown）、分类、标签。

### 日记表单

日期*（默认今天）、模板（三件好事/今日总结/情绪记录/空白）、内容*、心情 emoji、天气图标、地点。

### 日程月历视图

月历网格，有日程的日期显示圆点标记。点击日期展开当日日程列表，点击空白可新建（预填日期）。

### 待办看板视图

三列看板：待办 | 进行中 | 已完成。卡片显示优先级色标 + 标题 + 分类 + 截止时间。拖拽卡片改变状态。

### 记账列表视图

顶部三张汇总卡片（收入/支出/结余），快速记账表单，列表按日期分组显示。

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

### 待办统计

| 图表             | 类型   | 说明                       |
|------------------|--------|----------------------------|
| 顶部数据卡片     | 数字   | 总任务/已完成/完成率/本周新增 |
| 完成率趋势       | 折线图 | 按周统计完成率             |
| 分类分布         | 饼图   | 各分类占比                 |
| 优先级分布       | 环形图 | 四个优先级占比             |

### 日程统计

| 图表             | 类型       | 说明                       |
|------------------|------------|----------------------------|
| 每周忙碌度       | 柱状图     | 过去 8 周，Y轴日程数量     |
| 时间段分布       | 横向柱状图 | 6个时间段忙碌占比          |
| 分类分布         | 饼图       | 各分类日程占比             |

### 图表交互

- 悬停：tooltip 显示具体数值
- 点击饼图扇区：展开该分类明细列表
- 点击柱状图：跳转对应时间段列表视图
- 图表右上角：下载为 PNG

### Dashboard 迷你图表

首页"本月消费趋势"为精简版折线图（无坐标轴标签），点击跳转完整统计页。

---

## 5. 技术实现

### 后端

- **复用 `services/db.py`**：API 层通过依赖注入获取现有 DatabaseService 实例
- **统计接口**：新写聚合 SQL（GROUP BY 月份/分类/日期等）
- **Web 服务**：uvicorn 在 daemon thread 中运行
- **JWT**：SECRET_KEY 随进程生成，PyJWT 库编解码

### 前端

- **路由**：hash 路由，`window.addEventListener('hashchange')`
- **模块化**：ES Modules，每个 page 导出 `render(container)` 和 `destroy()`
- **API 封装**：统一 fetch wrapper，自动带 Token，统一错误处理
- **图表**：Chart.js 4.x（CDN + lib/ 离线备份）
- **拖拽**：原生 Drag & Drop API（待办看板）
- **日期选择**：原生 `<input type="datetime-local">`

### 第三方依赖

仅 Chart.js，无其他依赖。无需 Node.js 或构建工具。

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

API 层 100% 复用，前端替换 static/ 目录即可。
