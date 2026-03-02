# XiaoQing - Python 异步 QQ 机器人框架

基于 **OneBot 协议** 和 **Python 3.10+ 异步** 的轻量级 QQ 机器人框架。

**核心特性**：
- ✅ **快速上手** - 5 分钟完成配置启动
- ✅ **智能对话** - 内置 **向量记忆** 与 **行为规划**，拒绝机械回复
- ✅ **插件化** - 每个功能独立开发，支持热重载，生态丰富
- ✅ **多轮对话** - 内置会话管理支持复杂交互（如游戏、多步骤）
- ✅ **定时任务** - APScheduler 支持 cron 表达式
- ✅ **安全可靠** - Token 鉴权、管理员权限、并发控制
- 📚 **文档完善** - [完整文档](docs/README.md) 覆盖架构到高级功能
- ✅ **异步优先** - 100% 异步设计，高效并发处理

**🎉 最新更新 (V3.1.0)**:
- ✨ **qingpet 插件上线** - 完整的 QQ 群宠物养成系统，支持领养、喂养、多维互动和内置经济系统
- ✨ **xiaoqing_chat 插件升级** - 向量记忆检索、情绪系统、表达学习
- ✨ **pendo 插件上线** - 个人时间与信息管理中枢（日程、待办、笔记、日记）
- 📚 **29 个插件完整文档** - 每个插件都有详细的使用说明和配置指南
- 🔧 **qingssh v1.3** - 支持用户名指定、多服务器并发连接
- 📊 **ads_paper 插件** - NASA ADS 论文管理，支持 BibTeX 导出和 AI 摘要

**文档索引**：
- [快速入门](docs/01-getting-started.md)
- [插件开发指南](docs/03-plugin-development.md)
- [核心架构](docs/02-architecture.md)
- [消息流程](docs/08-message-flow.md)
- [插件列表](docs/09-plugins.md)

---

## 快速开始（5 分钟）

### 1. 环境要求
- **Python 3.10+**
- **OneBot 服务**（推荐 [NapCatQQ](https://github.com/NapNeko/NapCatQQ)）

### 2. 安装

```bash
cd XiaoQing
pip install -r requirements.txt
```

### 3. 配置

复制示例配置：
```bash
cp config/config.json config/config.json.bak  # 备份
# 编辑 config/config.json，主要改这些：
```

**config/config.json** - 关键配置：
```json
{
  "bot_name": "小青",           // 机器人名称（群聊触发用）
  "command_prefixes": ["/"],    // 命令前缀
  "inbound_http_base": "http://127.0.0.1:12000",
  "inbound_ws_uri": "ws://127.0.0.1:12000/ws",
  "onebot_http_base": "http://127.0.0.1:11001",  // OneBot API 地址
  "log_level": "INFO"
}
```

**config/secrets.json** - 敏感信息：
```json
{
  "inbound_token": "your-secret-token",  // 与 OneBot 通信的密钥
  "admin_user_ids": [123456789],         // 管理员 QQ
  "plugins": {}
}
```

### 4. 连接 OneBot

**推荐：被动模式**（OneBot 主动推送消息给 XiaoQing）

在 OneBot 配置文件中添加：
```yaml
# NapCat 示例
http:
  post:
    - url: http://127.0.0.1:12000/event
      secret: your-secret-token
```

或者 **主动模式**：XiaoQing 连接 OneBot WebSocket
```json
{
  "enable_ws_client": true,
  "enable_inbound_server": false,
  "onebot_ws_uri": "ws://127.0.0.1:11000/ws"
}
```

### 5. 启动

```bash
python main.py
```

看到这些日志说明启动成功：
```
INFO - XiaoQing starting...
INFO - Loaded plugin: bot_core
INFO - Loaded plugin: echo
INFO - Inbound server started on 127.0.0.1:12000
```

---

## 基本使用

### 命令触发规则

**私聊**：所有消息都会处理

**群聊**：需满足以下之一
1. 消息以命令前缀开头 → `/help`
2. 消息包含机器人名称 → `小青 你好`  
3. 随机触发 → 按 `random_reply_rate` 概率

### 内置命令

| 命令 | 说明 |
|------|------|
| `/help` | 查看所有可用命令 |
| `/plugins` | 列出已加载插件 |
| `/reload <name>` | 重载指定插件 |
| `/echo <text>` | 测试回复 |
| `/猜数字` | 多轮对话游戏 |
| `/mute <分钟>` | 静音机器人（群聊） |

### 常用插件命令

| 插件 | 命令 | 说明 |
|------|------|------|
| xiaoqing_chat | `/xc <内容>` | 和小青智能聊天 |
| qingpet | `/宠物 help` | QQ 群宠物功能 |
| pendo | `/pendo event add` | 添加日程 |
| pendo | `/pendo todo add` | 添加待办 |
| qingssh | `/ssh <服务器>` | SSH 远程连接 |
| jupyter | `/py <代码>` | 执行 Python 代码 |
| astro_tools | `/astro <子命令>` | 天文计算工具 |
| ads_paper | `/paper search` | 搜索论文 |
| github | `/gh` | GitHub 热门项目 |

### 查看日志

```bash
tail -f logs/xiaoqing.log          # 实时日志
cat logs/xiaoqing_error.log        # 错误日志
```

---

## 插件开发（10 分钟）

### 最简单的插件

**plugins/myplugin/plugin.json**
```json
{
  "name": "myplugin",
  "version": "1.0.0",
  "entry": "main.py",
  "commands": [
    {
      "name": "hello",
      "triggers": ["hello", "你好"],
      "help": "打个招呼",
      "admin_only": false
    }
  ]
}
```

**plugins/myplugin/main.py**
```python
from typing import Any, Dict, List
from core.plugin_base import segments

# 支持相对导入（推荐）
# from .config import SETTINGS

async def handle(
    command: str,       # 命令名（如 "hello"）
    args: str,          # 命令参数（如 "世界"）
    event: Dict,        # 原始 OneBot 事件
    context             # 插件上下文
) -> List[Dict[str, Any]]:
    """处理 /hello 命令"""
    name = args.strip() or "世界"
    return segments(f"你好，{name}！")
```

重启 XiaoQing：
```bash
# 方式1：重启进程
python main.py

# 方式2：发送命令（每 2s 自动热重载，或手动重载）
/reload myplugin
```

### PluginContext 常用方法

```python
async def handle(command: str, args: str, event: Dict, context) -> List:
    # 日志
    context.logger.info(f"处理命令: {command}")
    
    # HTTP 请求
    async with context.http_session.get("https://api.example.com") as resp:
        data = await resp.json()
    
    # 配置/密钥
    api_key = context.secrets.get("myplugin", {}).get("api_key")
    
    # 文件存储（自动创建 plugins/myplugin/data/）
    data_file = context.data_dir / "data.json"
    
    # 判断管理员
    user_id = event.get("user_id")
    group_id = event.get("group_id")
    admin_ids = context.secrets.get("admin_user_ids", [])
    if user_id not in admin_ids:
        return segments("需要管理员权限")
    
    return segments("处理完成")
```

### 消息构建

```python
from core.plugin_base import text, image, image_url, segments

# 纯文本
return segments("Hello")

# 文本 + 图片
return [
    text("请看图片："),
    image_url("https://example.com/pic.jpg")
]

# 多条消息
return [
    text("第一行"),
    text("第二行")
]
```

### 多轮对话（会话）

某些交互需要记录用户状态。示例：猜数字游戏

**plugins/guess/main.py**
```python
import random
from core.plugin_base import segments

async def handle(command: str, args: str, event: Dict, context) -> List:
    """开始游戏，创建会话"""
    session = await context.create_session(
        initial_data={"target": random.randint(1, 100), "attempts": 7},
        timeout=180  # 3 分钟过期
    )
    return segments("🎮 想好了吗？我在 1-100 之间想了一个数字，你猜猜看")

async def handle_session(text: str, event: Dict, context, session) -> List:
    """处理用户在游戏中的猜测"""
    try:
        guess = int(text.strip())
    except ValueError:
        return segments("请输入一个数字")
    
    target = session.get("target")
    attempts = session.get("attempts", 1)
    
    if guess == target:
        await context.end_session()
        return segments(f"🎉 恭喜，你猜对了！用了 {8-attempts} 次")
    
    if attempts <= 1:
        await context.end_session()
        return segments(f"💔 游戏结束，答案是 {target}")
    
    session.set("attempts", attempts - 1)
    hint = "太小了" if guess < target else "太大了"
    return segments(f"{hint}，还有 {attempts-1} 次机会")
```

**plugin.json**
```json
{
  "name": "guess",
  "entry": "main.py",
  "commands": [{
    "name": "guess",
    "triggers": ["猜数字"],
    "help": "多轮对话游戏示例"
  }]
}
```

### 定时任务

**plugin.json** 声明任务：
```json
{
  "schedule": [
    {
      "id": "morning",
      "handler": "send_morning_msg",
      "cron": {"hour": 8, "minute": 0},
      "group_ids": [123456789]
    }
  ]
}
```

**main.py** 实现处理函数：
```python
async def send_morning_msg(context):
    """每天 8 点发送早安"""
    return segments("早上好呀！")
```

### 自动 URL 解析

当消息包含 URL 时，框架自动调用此函数：

```python
async def handle_url(url: str, event: Dict, context) -> List:
    """自动解析消息中的 URL"""
    context.logger.info(f"Parsing: {url}")
    return segments(f"URL: {url}")
```

### 自动闲聊回复

当消息不是命令时，尝试进行闲聊：

```python
async def handle_smalltalk(text: str, event: Dict, context) -> List:
    """处理非命令消息"""
    if "天气" in text:
        return segments("今天天气不错~")
    return None  # 返回 None 表示不处理
```

### 插件完整配置参考

**plugin.json**
```json
{
  "name": "myplugin",
  "version": "1.0.0",
  "description": "我的第一个插件",
  "entry": "main.py",
  "concurrency": "parallel",
  
  "commands": [
    {
      "name": "cmd",
      "triggers": ["cmd", "命令"],
      "help": "命令帮助",
      "admin_only": false,
      "priority": 0
    }
  ],
  
  "schedule": [
    {
      "id": "task_id",
      "handler": "task_func_name",
      "cron": {
        "hour": 9,
        "minute": 0,
        "day_of_week": "mon-fri"
      },
      "group_ids": [123456789]
    }
  ]
}
```

**字段说明**
| 字段 | 说明 |
|------|------|
| `name` | 插件唯一标识 |
| `version` | 版本号 |
| `entry` | 入口文件（通常 `main.py`） |
| `commands` | 命令列表 |
| `schedule` | 定时任务列表 |
| `concurrency` | `parallel`（默认）或 `serial` |

### 生命周期钩子

可选实现：

```python
async def init(context):
    """插件启动时调用"""
    context.logger.info("插件已启动")

async def shutdown(context):
    """插件停止时调用"""
    context.logger.info("插件已停止")
```

---

## 项目结构

```
XiaoQing/
├── main.py                  # 入口点
├── requirements.txt         # 依赖
├── config/
│   ├── config.json         # 基础配置
│   └── secrets.json        # 敏感配置
├── core/
│   ├── app.py              # 主应用
│   ├── dispatcher.py       # 消息分发
│   ├── router.py           # 命令路由
│   ├── plugin_manager.py   # 插件管理
│   ├── session.py          # 会话管理
│   ├── scheduler.py        # 定时任务
│   ├── plugin_base.py      # 插件基础工具
│   ├── onebot.py           # OneBot 通信
│   ├── server.py           # Inbound 服务
│   ├── context.py          # 插件上下文
│   └── ...
├── plugins/                # 插件目录
│   ├── bot_core/           # 核心命令（help、reload）
│   ├── xiaoqing_chat/      # 智能对话插件（向量记忆、情绪系统）
│   ├── pendo/              # 个人时间与信息管理中枢
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
├── logs/                   # 日志（自动生成）
└── tests/                  # 测试
```

---

## 常见问题

### ❓ 群聊不响应怎么办？

检查：
1. 消息是否以 `/` 开头
2. 或是否包含机器人名称（如 `小青`）
3. 查看 `log_level` 是否设为 `DEBUG`，查看 `logs/xiaoqing.log`

### ❓ 如何调试插件？

1. 设置 `log_level: "DEBUG"`
2. 在插件中添加 `context.logger.debug("...")`
3. 查看 `logs/xiaoqing.log`
4. 用 `test.ipynb` 单独测试

### ❓ 定时任务不执行？

1. 检查 `config.json` 中的 `default_group_ids` 或任务 `group_ids`
2. 检查 `timezone` 配置
3. 查看日志中是否有 "Scheduled" 的记录

### ❓ 如何热重载插件？

**方式 1**：发送命令
```
/reload myplugin
```

**方式 2**：重启进程
```bash
python main.py
```

### ❓ 如何分享我的插件？

1. 确保 `plugin.json` 格式正确
2. 插件目录包含 `main.py` 和 `plugin.json`
3. 放入 `plugins/` 目录
4. 重启或执行 `/reload <name>`

### ❓ Windows 下 torch 导入失败？

`main.py` 已处理。如仍有问题：
1. 确保 PyTorch 版本与 Python 版本匹配
2. 安装 Microsoft Visual C++ Redistributable

---

## 配置参考

### config.json 完整配置

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `bot_name` | str | `"小青"` | 机器人名称 |
| `command_prefixes` | list | `["/"]` | 命令前缀 |
| `require_bot_name_in_group` | bool | `true` | 群聊是否需要 @ |
| `random_reply_rate` | float | `0.05` | 随机回复概率（0-1） |
| `enable_ws_client` | bool | `false` | 启用 WebSocket 客户端 |
| `enable_inbound_server` | bool | `true` | 启用 Inbound 服务器 |
| `onebot_http_base` | str | `http://127.0.0.1:11001` | OneBot API |
| `inbound_http_base` | str | `http://127.0.0.1:12000` | Inbound HTTP 监听地址 |
| `inbound_ws_uri` | str | `ws://127.0.0.1:12000/ws` | Inbound WS 监听地址 |
| `ws_queue_size` | int | `200` | WS 队列上限（Inbound + OneBot） |
| `max_concurrency` | int | `5` | 最大并发消息数 |
| `session_timeout` | int | `300` | 会话超时（秒） |
| `timezone` | str | `Asia/Shanghai` | 定时任务时区 |
| `log_level` | str | `INFO` | 日志级别 |

---

## API 参考

### Inbound 服务器

当 `enable_inbound_server: true` 时：
- 若 `inbound_http_base` 非空：启动 Inbound HTTP（提供 `/event`、`/health`、`/metrics`）
- 若 `inbound_ws_uri` 非空：启动 Inbound WebSocket（仅 WS）

**POST /event** - 接收 OneBot 事件
```bash
curl -X POST http://127.0.0.1:12000/event \
  -H "Authorization: Bearer your-secret-token" \
  -H "Content-Type: application/json" \
  -d '{"post_type":"message",...}'
```

**WebSocket /ws** - 持久连接
```
ws://127.0.0.1:12000/ws
Header: Authorization: Bearer your-secret-token
```

**GET /health** - 健康检查
```bash
curl http://127.0.0.1:12000/health
# {"status":"ok","version":"1.0.0"}
```

---

## 测试

```bash
# 运行所有测试
pytest

# 运行单个测试
pytest tests/test_plugin_base.py

# 使用 test.ipynb 交互式测试
python -m jupyter notebook test.ipynb
```

---

## 贡献指南

欢迎 Pull Request！开发流程：

1. Fork 本仓库
2. 创建特性分支：`git checkout -b feature/xyz`
3. 提交代码：`git commit -m "Add feature xyz"`
4. 推送分支：`git push origin feature/xyz`
5. 开启 Pull Request

**代码风格**：
- 4 空格缩进
- `snake_case` 函数/变量，`PascalCase` 类名
- 添加类型注解和文档字符串

---

## 许可证

MIT License

---

## 致谢

- [OneBot](https://onebot.dev/) - 标准化聊天机器人协议
- [APScheduler](https://apscheduler.readthedocs.io/) - 定时任务库
- [aiohttp](https://docs.aiohttp.org/) - 异步 HTTP 库

---

**快速链接**：[快速开始](#快速开始5-分钟) | [插件开发](#插件开发10-分钟) | [配置参考](#配置参考) | [常见问题](#常见问题)
