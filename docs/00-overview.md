# 00 - 项目概览

## XiaoQing 是什么？

XiaoQing 是一个基于 **Python 异步**（asyncio）和 **OneBot 协议** 的轻量级 QQ 机器人框架。

### 一句话描述

> 接收 QQ 消息 → 解析命令 → 调用插件处理 → 返回响应

### 核心价值

| 特性 | 说明 |
|------|------|
| **插件化** | 每个功能独立成插件，可热重载，互不干扰 |
| **异步优先** | 100% 异步设计，高效处理并发消息 |
| **协议标准** | 基于 OneBot 协议，兼容多种 QQ 客户端实现 |
| **开发友好** | 清晰的 API，完善的日志，易于调试 |

---

## 核心概念

在开始之前，先了解几个核心概念：

### 1. OneBot 协议

[OneBot](https://onebot.dev/) 是一个聊天机器人的标准协议。它定义了：

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

## 系统架构简图

```
┌─────────────────────────────────────────────────────────────┐
│                      XiaoQingApp (app.py)                       │
│                  应用入口，管理所有组件                      │
└───────────────────────────┬─────────────────────────────────┘
                            │
         ┌──────────────────┼──────────────────┐
         │                  │                  │
         ▼                  ▼                  ▼
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
```

---

## 消息处理流程

一条消息从收到到响应，经历以下步骤：

```
1. 收到消息
   │
   ▼
2. Dispatcher 解析
   - 提取文本、user_id、group_id
   - 判断是否需要处理（群聊需要 @机器人 或命令前缀）
   │
   ▼
3. 检查会话
   - 用户是否有进行中的多轮对话？
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

## 目录结构

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
│   └── logging_config.py   # 日志配置
│
├── plugins/                # 插件目录（28 个插件）
│   ├── bot_core/           # 核心命令（help、reload）
│   ├── xiaoqing_chat/      # 智能对话插件（向量记忆、情绪系统）
│   ├── pendo/              # 个人时间与信息管理中枢
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
│   ├── memo/               # 笔记管理
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
│   ├── test_framework.py
│   ├── test_message.py
│   └── ...
│
└── docs/                   # 文档（你正在看的）
```

---

## 设计原则

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
- 互不干扰，可单独热重载

---

## 与其他框架对比

| 特性 | XiaoQing | NoneBot2 | Koishi |
|------|------|----------|--------|
| 语言 | Python | Python | TypeScript |
| 复杂度 | 简单 | 中等 | 中等 |
| 学习曲线 | 低 | 中 | 中 |
| 插件生态 | 小 | 大 | 大 |
| 适合场景 | 个人/小型 | 通用 | 通用 |

**XiaoQing 适合**：
- 想要简单、直接的框架
- 快速开发个人机器人
- 学习机器人开发原理

---

## 下一步

准备好了吗？前往 [01-getting-started.md](01-getting-started.md) 开始安装和配置！
