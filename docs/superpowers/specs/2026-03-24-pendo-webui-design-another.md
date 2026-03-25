# Pendo Web UI 设计文档（修订版）

**日期**: 2026-03-24  
**状态**: 已修订，待实现/同步代码

## 概述

为 `plugins/pendo` 设计并实现一个美观、轻量、可维护的 Web UI，用于展示和管理该插件已有的五类个人数据：**日程、待办、笔记、日记、记账**，并覆盖 **Dashboard 总览、搜索、设置、快捷录入** 等核心场景。

本次修订的关键点：

- **不引入 FastAPI / Node / Vue / Vite**。当前仓库没有现成前端工程或 FastAPI 基础设施，但已经有 `aiohttp` 服务模式，因此 WebUI 应直接复用 Python + `aiohttp.web`。
- **不做“另起炉灶”的产品**。WebUI 必须围绕现有 `plugins/pendo` 数据模型、数据库接口、命令语义和隐私约束来构建。
- **保留扩展空间**。后续若需要迁移到更重的前端栈，API 和静态资源组织方式应支持平滑演进。

---

## 1. 设计目标

### 用户目标

用户应能在浏览器中完成以下高频操作：

1. 一眼看到今天的核心信息：今日日程、未完成待办、最近账目、最近笔记/日记。
2. 快速新增数据，而不是只能依赖聊天命令。
3. 对已有条目进行筛选、搜索、查看详情、编辑、删除。
4. 调整时区、静默时段、提醒、默认视图等设置。
5. 在移动端和桌面端都能获得清晰、舒服的阅读与录入体验。

### 工程目标

1. 复用 `services/db.py` 的既有读写能力。
2. 避免把聊天消息格式化逻辑直接搬到 Web 端。
3. 后端保持薄层：做认证、字段校验、聚合与接口编排，不重写核心业务存储。
4. 静态资源采用无构建方案，方便插件内自包含分发。

---

## 2. 约束与现实情况

### 已有基础

- 插件入口：`plugins/pendo/main.py`
- 数据层：`plugins/pendo/services/db.py`
- 数据模型：`plugins/pendo/models/item.py`
- 用户设置：`db.get_user_settings()` / `db.update_user_settings()`
- 搜索能力：`db.search_items()`
- 日期范围与总览能力：`db.get_events_for_range()`、`db.get_briefing_items()`、`db.query_items_by_date_range()`

### 当前缺失

- 没有 Web 服务器挂载点
- 没有浏览器认证机制
- 没有 REST/JSON 接口
- 没有共享前端组件系统
- 没有插件现成 WebUI 可直接复用

### 设计结论

因此本方案采用：

- **`aiohttp.web` 独立挂载 Web 服务**
- **插件内部静态资源目录**
- **token 登录 + 本地会话存储**
- **统一 JSON API**
- **原生 HTML/CSS/JS + ES Modules**

---

## 3. 信息架构

### 一级导航

1. **总览**：Dashboard，展示今天的关键事项与近期动态
2. **日程**：时间线 + 列表
3. **待办**：分状态列表
4. **记账**：汇总卡片 + 账目流水
5. **笔记**：卡片式列表
6. **日记**：时间线式列表
7. **搜索**：跨类型全文搜索
8. **设置**：用户级设置、登录 token 提示、主题说明

### 首页 Dashboard

Dashboard 采用“概览优先”而不是图表优先：

- 顶部欢迎区：今日日期、时区、快捷操作按钮
- 统计卡片：今日日程数、未完成待办数、最近 7 天账目笔数、最近 30 天日记数
- 今日日程时间线
- 待办重点区（高优先级/逾期优先）
- 最近账目区（显示收入、支出、净额）
- 最近记录区（笔记/日记混合流）


---

## 4. 视觉设计

### 视觉关键词

- 清爽、柔和、信息密度高但不压迫
- 卡片化布局
- 模块分色，但整体基于统一中性色背景
- 强调时间、优先级、状态这三类信息

### 色彩系统

- 背景：暖灰白 / 轻雾蓝灰
- 主强调色：靛蓝
- 日程：琥珀
- 待办：翠绿
- 记账：玫红偏红
- 笔记：天蓝
- 日记：紫粉
- 危险操作：红色

### 组件风格

- 大圆角卡片
- 轻阴影 + 微边框
- 图标 emoji 与纯色标记并存
- 按钮分为主按钮、次按钮、幽灵按钮
- 表单输入统一高亮 focus ring

---

## 5. 页面与交互

### 5.1 总览页

- 四张摘要卡片
- 今日日程时间线（按开始时间排序）
- 逾期/高优先级待办列表
- 最近账目列表与净额提示
- 最近笔记 / 日记列表
- “快速添加”按钮组：新日程 / 新待办 / 记一笔 / 新笔记 / 写日记

### 5.2 日程页

- 顶部筛选：today / tomorrow / week / month / 自定义
- 列表卡片显示：标题、时间范围、地点、提醒、重复规则
- 详情抽屉或 Modal：显示备注、里程碑、提醒时间
- 支持新建、编辑、删除

### 5.3 待办页

- 顶部状态筛选：全部 / 待办 / 进行中 / 已完成 / 已取消
- 顶部分类筛选与搜索框
- 列表项显示：优先级、分类、截止时间、状态、标签
- 行内快捷操作：完成、重开、编辑、删除

### 5.4 记账页

- 顶部三张汇总卡：收入 / 支出 / 净额
- 快速记账条：金额、摘要、分类、方向
- 列表按日期倒序
- 支持范围筛选：today / week / month / last30d

### 5.5 笔记页

- 卡片网格布局
- 支持分类与标签筛选
- 卡片展示标题、摘要、标签、更新时间
- 点击打开详情/编辑弹窗

### 5.6 日记页

- 时间线布局
- 展示日期、心情、天气、地点、摘要
- 支持按月份筛选
- 支持新建/编辑/删除

### 5.7 搜索页

- 跨类型统一搜索框
- 支持类型过滤：event/task/note/diary/ledger
- 支持结果卡片展示类型、标题、摘要、时间信息

### 5.8 设置页

- 时区
- 静默时段
- 每日简报时间
- 日记提醒时间
- 提醒开关 / 每日简报开关 / 隐私模式
- Web 登录说明与 token 使用指引

---

## 6. 技术方案

### 后端

新增目录建议：

```text
plugins/pendo/web/
├── __init__.py
├── auth.py          # token 生成/校验
├── server.py        # aiohttp app / lifecycle / routes
├── views.py         # 聚合与序列化辅助
...
```

### 认证

- 用户通过聊天命令生成短期登录 token
- 浏览器首次进入后输入 token 登录
- token 写入 `localStorage`
- 后续请求通过 `Authorization: Bearer <token>`
- token payload 至少包含：`user_id`、`exp`
- token secret 随进程启动生成；重启后旧 token 失效

### 服务启动

- 在 `plugins/pendo/main.py:init()` 中按配置决定是否启动 Web 服务
- 使用后台线程 + 独立事件循环运行 `aiohttp.web`
- 在 `cleanup()` 中优雅停止服务

### 配置项

在 `plugins/pendo/config.py` 增加：

- `WEB_ENABLED = True`
- `WEB_HOST = "127.0.0.1"`
- `WEB_PORT = 8765`
- `WEB_TOKEN_EXPIRE_HOURS = 24`

默认绑定 `127.0.0.1`，避免把个人数据面板意外暴露到公网。

---

## 7. API 设计

统一响应格式：

```json
{ "ok": true, "data": {}, "message": "" }
```

### 认证相关

- `POST /pendo/api/auth/verify`：验证 token 是否有效

### Dashboard

- `GET /pendo/api/dashboard`

返回：

- 今日摘要
- 今日日程
- 重点待办
- 最近账目
- 最近笔记/日记

### 通用条目接口

- `GET /pendo/api/items?type=task&status=todo&limit=50`
- `GET /pendo/api/items/{id}`
- `POST /pendo/api/items`
- `PATCH /pendo/api/items/{id}`
- `DELETE /pendo/api/items/{id}`

以统一接口为主，减少后端样板代码；前端根据 `type` 决定展示方式。

### 搜索

- `GET /pendo/api/search?q=关键词&type=note`

### 设置

- `GET /pendo/api/settings`
- `PUT /pendo/api/settings`

---

## 8. 聊天命令集成

新增 `/pendo web` 子命令：

- `/pendo web`：显示帮助
- `/pendo web token`：生成登录 token 和 URL
- `/pendo web status`：查看 Web 服务状态与地址

