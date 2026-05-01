# 🗺️ 00 - 项目概览

> [!NOTE]
> 本章是入门概览，适合所有读者。读完后可以接着看 [01-getting-started.md](01-getting-started.md) 完成安装。

## 🤖 XiaoQing 定位

XiaoQing 是一个基于 **Python 异步**（asyncio）和 **OneBot 协议** 的轻量级 QQ 机器人框架。

它在当前仓库中同时承担两层角色。

1. **机器人框架**：`core/` 提供 OneBot 接入、消息分发、命令路由、插件生命周期、多轮会话、调度任务、配置热重载、运行指标和错误处理。
2. **成品机器人应用**：`plugins/` 提供可直接加载的功能插件，覆盖拟人聊天、个人时间管理、天文工具、远程运维、代码执行、群娱乐、外部信息抓取和自动化任务。

项目默认适合长期运行在个人机器、服务器或 NAS 上。QQ 客户端侧由 NapCatQQ 等 OneBot 实现负责，XiaoQing 专注于事件处理、业务插件和响应生成。

## ⚡ 三分钟理解

初次阅读时先记住三点，后面的配置和源码阅读会轻松很多。

1. XiaoQing 的核心是 `core/`，负责消息接入、命令路由、会话管理和调度。
2. 大部分能力都来自 `plugins/`，以带 `plugin.json` 的目录作为真正的插件单元。
3. 项目既支持聊天型插件，也支持像 `pendo` 这种带 Web 控制台的复合插件。

### 一句话描述

> 接收 QQ 消息 → 解析命令 → 调用插件处理 → 返回响应

### 核心特性

| 特性 | 说明 |
|------|------|
| **插件化** | 每个功能独立成插件，可手动重载；配置文件默认自动监控，插件文件 watcher 可按需开启 |
| **异步优先** | 基于 asyncio 处理并发消息 |
| **协议标准** | 基于 OneBot 协议，兼容多种 QQ 客户端实现 |
| **开发接口** | 提供插件 API、日志和运行指标统计 |
| **Web 控制台** | pendo 插件内置 FastAPI + 原生 JS SPA，支持浏览器管理个人数据、统计和迁移 |

---

## 🌟 当前项目能力版图

| 领域 | 插件 | 能力 |
|------|------|------|
| 拟人聊天 | `xiaoqing_chat` | 多模态群聊/私聊、图片和表情上下文、记忆、PFC 行为规划、reply checker、表达学习 |
| 个人管理 | `pendo` | 日程、待办、笔记、日记、账本、提醒、搜索、统计、Web 控制台、Scriptable 小组件 |
| 核心管理 | `bot_core` | `/help`、`/reload`、`/plugins`、静音、配置管理、metrics |
| 基础聊天 | `smalltalk`, `chat`, `voice` | 简单闲聊、Coze 对话、TTS 和内部 STT 工具 |
| 运维与执行 | `qingssh`, `shell`, `jupyter`, `minecraft` | SSH、受限 shell、Jupyter REPL、Minecraft RCON |
| 科研/天文 | `apod`, `arxiv_filter`, `ads_paper`, `astro_tools`, `dict`, `chime` | 天文图、论文筛选、ADS、天文计算、词典、FRB 监测 |
| 外部信息 | `github`, `earthquake`, `twitter`, `url_parser`, `signin`, `adnmb` | Trending、地震、图片抓取、链接预览、签到、A 岛 |
| 娱乐工具 | `choice`, `guess_number`, `qingpet`, `color`, `wolframalpha`, `echo` | 抽奖、猜数字、群宠物、颜色、计算、示例回显 |

完整插件命令和配置在 [09-plugins.md](09-plugins.md)。

---

## 💡 核心概念

后续文档会反复使用以下核心概念。

### 1. OneBot 协议

[OneBot](https://onebot.dev/) 是聊天机器人的标准协议。它定义事件格式、API 格式和通信方式。

- **事件格式**：QQ 消息如何表示为 JSON
- **API 格式**：如何发送消息、获取信息
- **通信方式**：HTTP、WebSocket 等

XiaoQing 不直接连接 QQ，而是通过 OneBot 协议与 **OneBot 实现**（如 NapCatQQ、go-cqhttp）通信：

```
QQ 服务器 ↔→ OneBot 实现 ↔→ XiaoQing 框架 ↔→ 你的插件
              (NapCatQQ)      (本项目)
```

### 2. 插件（Plugin）

插件是 XiaoQing 的功能单元。每个插件：

- 独立的文件夹（如 `plugins/echo/`）
- 包含 `plugin.json`（配置）和 `main.py`（代码）
- 响应特定命令或事件
- 可以被热重载

```
plugins/
├── echo/           # echo 插件
│   ├── plugin.json # 插件配置
│   └── main.py     # 插件代码
├── guess_number/   # 猜数字插件
└── ...
```

### 3. 命令（Command）

命令是用户触发插件的方式：

```
/echo hello world
 ↑     ↑
命令   参数
```

命令由 `plugin.json` 中的 `triggers` 定义：

```json
{
  "commands": [{
    "name": "echo",
    "triggers": ["echo", "复读"],  // 触发词
    "help": "复读你说的话"
  }]
}
```

### 4. 消息段（Segment）

OneBot 用"消息段"表示消息内容：

```python
# 纯文本
{"type": "text", "data": {"text": "你好"}}

# 图片
{"type": "image", "data": {"file": "https://example.com/pic.jpg"}}

# 组合消息
[
    {"type": "text", "data": {"text": "看图："}},
    {"type": "image", "data": {"file": "..."}}
]
```

XiaoQing 提供便捷函数：

```python
from core.plugin_base import text, image_url, segments

# 等价于上面的组合消息
return [text("看图："), image_url("https://example.com/pic.jpg")]

# 或者更简单
return segments("你好")  # 自动转换为消息段
```

### 5. 上下文（Context）

每次处理消息时，插件会收到一个 `context` 对象，包含：

- 配置信息（`context.config`, `context.secrets`）
- 日志工具（`context.logger`）
- HTTP 客户端（`context.http_session`）
- 会话管理（`context.create_session()`）
- 数据目录（`context.data_dir`）

---

## 🏗️ 系统架构简图

```
┌─────────────────────────────────────────────────────────────┐
│                   XiaoQingApp (app.py)                      │
│               应用入口，管理所有组件                         │
└──────┬────────────────────┬────────────────────┬────────────┘
       │                    │                    │
       ▼                    ▼                    ▼
┌─────────────┐    ┌─────────────┐    ┌─────────────────┐
│  OneBot WS  │    │  Inbound    │    │   Scheduler     │
│   Client    │    │   Server    │    │   定时任务      │
│  (主动连接) │    │  (被动接收) │    └─────────────────┘
└──────┬──────┘    └──────┬──────┘
       │                  │
       └────────┬─────────┘
                │ 消息事件
                ▼
┌─────────────────────────────────────────────────────────────┐
│                   Dispatcher (dispatcher.py)                │
│              消息分发器：解析、路由、会话管理                │
└───────────────────────────┬─────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────┐
│                    Router (router.py)                       │
│                 命令路由：匹配触发词                         │
└───────────────────────────┬─────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────┐
│               PluginManager (plugin_manager.py)             │
│            插件管理：加载、卸载、热重载                      │
└───────────────────────────┬─────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────┐
│                    Plugin (main.py)                         │
│                   你的插件代码                               │
└─────────────────────────────────────────────────────────────┘

（pendo 插件额外内置 FastAPI Web Server，独立提供 HTTP 控制台服务）
```

---

## 📨 消息处理流程

一条消息从收到到响应，经历以下步骤：

```
1. 收到消息
   │
   ▼
2. Dispatcher 解析
   - 提取文本、user_id、group_id
   - 判断是否需要处理（普通群聊通常需要 @机器人、命令前缀或随机触发；`smalltalk_provider = xiaoqing_chat` 时所有群聊消息进入插件内部判定）
   │
   ▼
3. 检查会话
   - 检查用户是否有进行中的多轮对话
   - 有 → 路由到会话插件
   │
   ▼
4. 命令路由
   - Router 匹配触发词
   - 找到对应插件和命令
   │
   ▼
5. 权限检查
   - admin_only 命令检查管理员权限
   │
   ▼
6. 执行插件
   - 调用 plugin.handle(command, args, event, context)
   - 插件返回消息段列表
   │
   ▼
7. 发送响应
   - 通过 OneBot API 发送消息
```

---

## 📁 目录结构

```
XiaoQing/
├── main.py                 # 程序入口
├── requirements.txt        # Python 依赖
├── pytest.ini              # 测试配置
│
├── config/                 # 配置文件
│   ├── config.json         # 基础配置（可提交到 Git）
│   └── secrets.json        # 敏感配置（不要提交！）
│
├── core/                   # 核心框架
│   ├── app.py              # 主应用类
│   ├── dispatcher.py       # 消息分发器
│   ├── router.py           # 命令路由
│   ├── plugin_manager.py   # 插件管理
│   ├── plugin_base.py      # 插件工具函数
│   ├── context.py          # 插件上下文
│   ├── session.py          # 会话管理（多轮对话）
│   ├── scheduler.py        # 定时任务
│   ├── onebot.py           # OneBot 通信
│   ├── server.py           # Inbound 服务器
│   ├── config.py           # 配置管理
│   ├── message.py          # 消息处理工具
│   ├── args.py             # 命令参数解析（ParsedArgs）
│   ├── metrics.py          # 运行指标统计（MetricsCollector）
│   ├── interfaces.py       # Protocol 接口定义
│   ├── exceptions.py       # 自定义异常
│   ├── models.py           # 通用数据模型
│   ├── clock.py            # 时区/时间工具
│   ├── constants.py        # 全局常量
│   └── logging_config.py   # 日志配置
│
├── plugins/                # 插件目录（可直接加载的内置插件；*_deprecated 目录默认不参与加载）
│   ├── bot_core/           # 核心命令（help、reload）
│   ├── xiaoqing_chat/      # 多模态拟人聊天（attention gate、记忆、规划、reply checker）
│   ├── pendo/              # 个人时间与信息管理中枢（日程/待办/笔记/日记/账本/提醒/Web 控制台）
│   ├── qingpet/            # QQ群宠物养成系统
│   ├── qingssh/            # SSH 远程控制
│   ├── jupyter/            # Python 代码执行
│   ├── astro_tools/        # 天文计算工具箱
│   ├── ads_paper/          # NASA ADS 论文管理
│   ├── github/             # GitHub Trending
│   ├── arxiv_filter/       # arXiv 论文筛选
│   ├── apod/               # 每日天文图
│   ├── chime/              # FRB 重复暴监测
│   ├── minecraft/          # MC 服务器通信
│   ├── smalltalk/          # 闲聊插件
│   ├── chat/               # AI 对话
│   ├── voice/              # 语音功能
│   ├── memo_deprecated/    # 已停用的 memo 兼容目录（无 plugin.json）
│   ├── choice/             # 随机选择
│   ├── wolframalpha/       # 万能计算器
│   ├── url_parser/         # 链接解析
│   ├── shell/              # 终端命令
│   ├── earthquake/         # 地震快讯
│   ├── signin/              # 自动签到
│   ├── twitter/            # Twitter 图片
│   ├── guess_number/       # 猜数字游戏
│   ├── dict/               # 天文学词典
│   ├── color/              # 颜色查询
│   ├── adnmb/              # A岛匿名版
│   └── echo/               # 回显示例
│
├── logs/                   # 日志目录（自动生成）
│   ├── xiaoqing.log            # 所有日志
│   └── xiaoqing_error.log      # 错误日志
│
├── tests/                  # 测试文件
│   ├── test_app.py
│   ├── test_dispatcher.py
│   ├── plugins/
│   ├── integration/
│   └── helpers/
│
└── docs/                   # 文档（你正在看的）
```

---

## 🎯 设计原则

> [!TIP]
> 这些原则贯穿整个代码库，理解它们有助于快速找到正确的扩展方式。

XiaoQing 的设计遵循以下原则：

### 1. 简单优先
- 最小必要的抽象
- 代码即文档
- 新手能快速上手

### 2. 约定优于配置
- 插件放 `plugins/` 目录
- 入口文件叫 `main.py`
- 配置文件叫 `plugin.json`

### 3. 异步原生
- 所有 I/O 操作异步执行
- 使用 `asyncio` 和 `aiohttp`
- 不阻塞事件循环

### 4. 插件隔离
- 每个插件独立目录
- 独立的数据存储
- 互不干扰，可单独热重载；可通过 `enable_plugin_watcher` 开启运行时 watcher 自动监控

---

## ⚖️ 与其他框架对比

| 特性 | XiaoQing | NoneBot2 | Koishi |
|------|------|----------|--------|
| 语言 | Python | Python | TypeScript |
| 复杂度 | 简单 | 中等 | 中等 |
| 学习曲线 | 低 | 中 | 中 |
| 插件生态 | 小 | 大 | 大 |
| 适合场景 | 个人/小型 | 通用 | 通用 |

**XiaoQing 适用场景**
- 想要简单、直接的框架
- 快速开发个人机器人
- 学习机器人开发原理

---

## ➡️ 下一步

下一步阅读 [01-getting-started.md](01-getting-started.md)，完成安装和配置。
