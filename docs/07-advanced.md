# 07 - 高级主题

本章涵盖多轮对话、定时任务、URL 解析、闲聊等高级功能。

---

## 多轮对话

多轮对话允许机器人与用户进行持续的交互，记住对话状态。

### 工作原理

```
1. 用户发送命令（如 /猜数字）
   │
   ▼
2. 插件调用 context.create_session() 创建会话
   │
   ▼
3. 会话存储在 SessionManager 中
   │
   ▼
4. 用户后续消息被路由到 handle_session()
   │
   ├─ 继续对话 → 更新会话数据
   │
   └─ 结束对话 → context.end_session()
```

### 完整示例：猜数字游戏

**plugin.json**：
```json
{
  "name": "guess_number",
  "version": "1.0.0",
  "entry": "main.py",
  "commands": [{
    "name": "guess",
    "triggers": ["猜数字", "guess"],
    "help": "猜数字游戏"
  }]
}
```

**main.py**：
```python
import random
from typing import Any, Dict, List
from core.plugin_base import segments

# 游戏配置
MIN_NUM = 1
MAX_NUM = 100
MAX_ATTEMPTS = 7
TIMEOUT = 180.0  # 3 分钟

async def handle(command: str, args: str, event: Dict, context) -> List:
    """处理初始命令，创建会话"""
    # 检查是否已有游戏
    existing = await context.get_session()
    if existing:
        return segments(
            f"你已经有一个游戏在进行中！\n"
            f"当前范围: {existing.get('min')}-{existing.get('max')}\n"
            f"剩余次数: {existing.get('remaining')}\n"
            f"发送数字继续猜测，或发送「退出」放弃"
        )
    
    # 生成目标数字
    target = random.randint(MIN_NUM, MAX_NUM)
    
    # 创建会话
    await context.create_session(
        initial_data={
            "target": target,
            "min": MIN_NUM,
            "max": MAX_NUM,
            "attempts": 0,
            "remaining": MAX_ATTEMPTS,
            "history": []
        },
        timeout=TIMEOUT
    )
    
    context.logger.info(f"Game started: target={target}")
    
    return segments(
        f"🎮 猜数字游戏开始！\n"
        f"我想了一个 {MIN_NUM} 到 {MAX_NUM} 之间的数字\n"
        f"你有 {MAX_ATTEMPTS} 次机会\n"
        f"请发送一个数字开始猜测！"
    )


async def handle_session(text: str, event: Dict, context, session) -> List:
    """处理会话中的消息"""
    # 获取会话数据
    target = session.get("target")
    remaining = session.get("remaining")
    history = session.get("history", [])
    
    # 解析输入
    try:
        guess = int(text.strip())
    except ValueError:
        return segments("请输入一个数字！")
    
    # 验证范围
    min_num = session.get("min")
    max_num = session.get("max")
    if guess < min_num or guess > max_num:
        return segments(f"请输入 {min_num} 到 {max_num} 之间的数字！")
    
    # 更新尝试
    remaining -= 1
    history.append(guess)
    session.set("remaining", remaining)
    session.set("history", history)
    
    # 判断结果
    if guess == target:
        # 猜对了
        await context.end_session()
        return segments(
            f"🎉 恭喜你猜对了！答案是 {target}\n"
            f"用了 {len(history)} 次猜测\n"
            f"历史: {' → '.join(map(str, history))}"
        )
    
    if remaining <= 0:
        # 次数用尽
        await context.end_session()
        return segments(
            f"💔 游戏结束！答案是 {target}\n"
            f"你的猜测: {' → '.join(map(str, history))}"
        )
    
    # 给出提示
    if guess < target:
        hint = "太小了！"
        session.set("min", max(min_num, guess + 1))
    else:
        hint = "太大了！"
        session.set("max", min(max_num, guess - 1))
    
    return segments(f"{hint} 剩余 {remaining} 次机会")
```

### 退出会话

用户发送退出关键字时，Dispatcher 会自动结束会话：

```python
# dispatcher.py 中的处理
exit_commands = {"退出", "取消", "exit", "quit", "q"}
if text.strip().lower() in exit_commands:
    await self.session_manager.delete(user_id, group_id)
    return [{"type": "text", "data": {"text": "已退出当前对话"}}]
```

### Session API

```python
# 创建会话
session = await context.create_session(
    initial_data={"key": "value"},
    timeout=300.0
)

# 获取会话
session = await context.get_session()

# 读写数据
value = session.get("key", default)
session.set("key", value)
session.clear()

# 检查状态
if session.is_expired():
    ...

# 结束会话
await context.end_session()
```

---

## 定时任务

使用 APScheduler 执行定时任务。

### 配置方式

在 `plugin.json` 中声明：

```json
{
  "name": "daily",
  "entry": "main.py",
  "schedule": [
    {
      "id": "morning_greeting",
      "handler": "send_morning",
      "cron": {"hour": 8, "minute": 0},
      "group_ids": [123456, 789012]
    },
    {
      "id": "weekly_report",
      "handler": "send_weekly",
      "cron": {"day_of_week": "mon", "hour": 9, "minute": 0}
    }
  ]
}
```

### 处理函数

```python
async def send_morning(context) -> List[Dict]:
    """每天 8:00 发送"""
    return segments("☀️ 早上好！新的一天开始了")

async def send_weekly(context) -> List[Dict]:
    """每周一 9:00 发送"""
    # 生成报告
    report = await generate_report()
    return segments(f"📊 周报\n{report}")
```

### Cron 表达式

支持 APScheduler 的所有 cron 字段：

| 字段 | 说明 | 示例 |
|------|------|------|
| `year` | 年 | `2026` |
| `month` | 月 | `1-12` 或 `jan-dec` |
| `day` | 日 | `1-31` |
| `week` | 周数 | `1-53` |
| `day_of_week` | 星期 | `0-6` 或 `mon-sun` |
| `hour` | 时 | `0-23` |
| `minute` | 分 | `0-59` |
| `second` | 秒 | `0-59` |

**示例**：

```json
// 每天 8:00
{"hour": 8, "minute": 0}

// 每 2 小时整点
{"hour": "*/2", "minute": 0}

// 工作日 9:00
{"day_of_week": "mon-fri", "hour": 9, "minute": 0}

// 每月 1 日 0:00
{"day": 1, "hour": 0, "minute": 0}

// 每分钟（调试用）
{"minute": "*"}

// 每 30 分钟
{"minute": "*/30"}
```

### 指定发送目标

```json
{
  "schedule": [{
    "id": "task1",
    "handler": "func",
    "cron": {...},
    "group_ids": [123456]  // 指定群
  }]
}
```

如果不指定 `group_ids`，使用 `config.json` 中的 `default_group_ids`。

### 动态定时任务

在插件中动态添加任务：

```python
# 需要访问 app.scheduler
async def handle(command: str, args: str, event: Dict, context) -> List:
    # 获取 scheduler（需要通过 app）
    scheduler = context.app.scheduler
    
    # 添加任务
    scheduler.add_job(
        job_id="dynamic_task",
        func=my_task_func,
        cron={"hour": 12, "minute": 0}
    )
    
    # 移除任务
    scheduler.remove_job("dynamic_task")
```

---

## URL 自动解析

当消息包含 URL 时，自动调用 URL 解析插件。

### 实现方式

在插件中实现 `handle_url()` 函数：

```python
async def handle_url(url: str, event: Dict, context) -> List[Dict]:
    """
    自动解析 URL
    
    Args:
        url: 提取到的 URL
        event: 原始事件
        context: 插件上下文
    
    Returns:
        消息段列表，或 None/[] 表示不处理
    """
    context.logger.info(f"Parsing URL: {url}")
    
    # 只处理特定域名
    if "bilibili.com" not in url:
        return None
    
    try:
        # 获取视频信息
        async with context.http_session.get(url) as resp:
            html = await resp.text()
        
        title = extract_title(html)
        return segments(f"🎬 B站视频: {title}")
        
    except Exception as e:
        context.logger.warning(f"URL parsing failed: {e}")
        return None
```

### 触发条件

- 消息包含 `http://` 或 `https://` 开头的 URL
- 消息**不**以命令前缀开头

### 默认 URL 解析插件

框架查找名为 `url_parser` 的插件：

```python
url_plugin = self.app.plugin_manager.get("url_parser")
if url_plugin and hasattr(url_plugin.module, "handle_url"):
    result = await url_plugin.module.handle_url(url, event, context)
```

---

## 闲聊功能

当消息不是命令时，可以进行闲聊回复。

### 实现方式

实现 `handle_smalltalk()` 函数：

```python
async def handle_smalltalk(text: str, event: Dict, context) -> List[Dict]:
    """
    处理闲聊消息
    
    Args:
        text: 用户消息文本
        event: 原始事件
        context: 插件上下文
    
    Returns:
        消息段列表，或 None 表示不处理
    """
    # 关键词匹配
    if "天气" in text:
        return segments("今天天气不错呢~")
    
    if "你好" in text:
        return segments("你好呀！有什么可以帮你的？")
    
    # 调用 AI 模型
    response = await call_ai_model(text)
    if response:
        return segments(response)
    
    # 不处理
    return None
```

### 触发条件

- 私聊：无命令前缀的消息
- 群聊：包含 bot_name 但非命令
- 群聊：随机触发（`random_reply_rate`）

### 配置闲聊提供者

在 `config.json` 中：

```json
{
  "plugins": {
    "smalltalk_provider": "xiaoqing_chat"
  }
}
```

框架会优先调用指定插件的 `handle_smalltalk()`。

### 只叫机器人名字

当用户只发送机器人名字（如"小青"）时，调用 `call_bot_name_only()`：

```python
def call_bot_name_only(context) -> List[Dict]:
    """只叫机器人名字时的响应"""
    responses = ["叫我干嘛？", "嗯？", "在的~"]
    return segments(random.choice(responses))
```

---

## Handler 链式处理

框架引入了责任链模式来处理消息，更加清晰和灵活。

### Handler 链架构

```
消息到达 Dispatcher
    ↓
决策判断 (should_process)
    ↓
Handler 链依次尝试：
    ↓
┌─────────────────────────────┐
│ 1. BotNameHandler       │ ← 处理仅机器人名字（如"小青"）
│    - 返回固定回复或帮助  │
└──────────┬────────────────┘
           │ 失败（返回 None）
           ▼
┌─────────────────────────────┐
│ 2. CommandHandler        │ ← 命令匹配和执行
│    - 匹配触发词          │
│    - 检查权限           │
│    - 调用 handle()       │
└──────────┬────────────────┘
           │ 失败（无匹配命令）
           ▼
┌─────────────────────────────┐
│ 3. SessionHandler        │ ← 活跃会话处理
│    - 调用 handle_session()│
└──────────┬────────────────┘
           │ 失败（无活跃会话）
           ▼
┌─────────────────────────────┐
│ 4. SmalltalkHandler      │ ← 闲聊处理
│    - 调用 handle_smalltalk()│
└──────────┬────────────────┘
           │ 失败（不回复）
           ▼
       返回 []
```

### 短路机制

一旦某个 Handler 返回非 `None` 结果，后续 Handler 不会执行。

**示例**：用户发送 `/help`

```
1. BotNameHandler: 不是仅机器人名字 → None
2. CommandHandler: 匹配成功！
   → 调用 help.handle()
   → 返回帮助信息
   → 短路 ✅
3. SessionHandler: 不执行 ❌
4. SmalltalkHandler: 不执行 ❌
```

### 开发自定义 Handler

你可以实现自定义 Handler 来扩展消息处理逻辑。

**步骤 1**：定义 Handler 类

```python
from core.dispatcher import MessageHandler

class CustomHandler(MessageHandler):
    """自定义消息处理器"""
    
    def __init__(self, dispatcher):
        super().__init__(dispatcher)
    
    async def handle(self, text: str, event: Dict, context) -> Optional[List]:
        """处理消息"""
        # 实现你的逻辑
        if should_handle(text, event):
            return segments("自定义处理结果")
        
        # 不处理，传递给下一个 Handler
        return None
```

**步骤 2**：在插件 init() 中注册

```python
async def init(context):
    """插件初始化"""
    # 获取 dispatcher
    dispatcher = context.app.dispatcher
    
    # 创建自定义 Handler
    custom_handler = CustomHandler(dispatcher)
    
    # 插入到 Handler 链的指定位置
    # 例如：插入到 CommandHandler 之后
    handlers_list = list(dispatcher._handlers)
    handlers_list.insert(1, custom_handler)  # 插入到索引 1
    dispatcher._handlers = tuple(handlers_list)
```

**注意事项**：
- Handler 的执行顺序很重要
- 插入位置影响处理优先级
- 建议在文档中说明自定义 Handler 的优先级

### Handler 链调试

启用 DEBUG 日志可以查看 Handler 链的执行过程：

```python
# config.json
{
  "log_level": "DEBUG"
}
```

日志输出示例：
```
2026-02-04 10:00:00 DEBUG - BotNameHandler: checking...
2026-02-04 10:00:00 DEBUG - BotNameHandler: not matched, returning None
2026-02-04 10:00:00 DEBUG - CommandHandler: checking...
2026-02-04 10:00:00 DEBUG - CommandHandler: matched /help, executing...
```

---

## xiaoqing_chat 智能对话

xiaoqing_chat 提供基于 LLM 的智能对话能力。

### 核心特性

#### 1. 长期记忆系统

使用向量数据库存储对话历史，实现长期记忆。

**工作原理**：
```
用户发送消息
    ↓
向量化文本（embedding）
    ↓
查询相似历史（向量检索）
    ↓
构建上下文（包含相关历史）
    ↓
调用 LLM 生成回复
    ↓
保存对话到向量数据库
```

**配置**：
```json
{
  "plugins": {
    "xiaoqing_chat": {
      "memory_enabled": true,
      "memory_max_entries": 1000,
      "memory_similarity_threshold": 0.7
    }
  }
}
```

#### 2. 情绪系统

机器人有自己的"心情"，会根据对话历史调整。

**情绪值范围**：-1.0（负面）到 1.0（正面）

**影响因素**：
- 用户输入的情感倾向
- 对话频率
- 话题类型

**示例**：
```python
# 情绪影响回复风格
if emotion > 0.5:
    # 高兴状态
    return "好哒！我帮你看看~ 😊"
elif emotion < -0.5:
    # 低落状态
    return "嗯...我尽力帮你吧 😔"
else:
    # 正常状态
    return "好的，让我来处理~"
```

**配置**：
```json
{
  "plugins": {
    "xiaoqing_chat": {
      "emotion_enabled": true,
      "emotion_decay_rate": 0.01,
      "emotion_impact_factor": 0.1
    }
  }
}
```

#### 3. 表情学习

从用户对话中学习使用表情符号。

**工作原理**：
```
用户: "这个功能太棒了！😍"
    ↓
分析表情使用场景
    ↓
记录到表情库
    ↓
类似场景时复用表情
```

**配置**：
```json
{
  "plugins": {
    "xiaoqing_chat": {
      "expression_learning": true,
      "expression_min_occurrences": 3
    }
  }
}
```

### 智能回复控制

xiaoqing_chat 内部实现智能回复频率控制，优于简单的 `random_reply_rate`。

**控制策略**：

1. **话题相关性**：根据用户输入的相关性决定是否回复
2. **对话连贯性**：保持对话的连贯性
3. **用户频率限制**：避免对同一用户回复过频繁
4. **上下文感知**：根据对话上下文判断

**示例**：
```python
async def handle_smalltalk(text: str, event: Dict, context) -> List:
    """智能回复控制"""
    
    # 1. 获取用户历史
    user_id = event.get("user_id")
    history = await get_user_history(user_id, context)
    
    # 2. 分析话题相关性
    relevance = analyze_relevance(text, history)
    if relevance < 0.3:
        # 话题不相关，不回复
        return None
    
    # 3. 检查频率限制
    recent_count = get_recent_reply_count(user_id, context)
    if recent_count > 3:
        # 回复过频繁，跳过
        return None
    
    # 4. 生成回复
    response = await generate_llm_response(text, history, context)
    
    # 5. 保存到历史
    await save_to_history(user_id, text, response)
    
    return segments(response)
```

### 扩展 xiaoqing_chat

你可以在现有基础上扩展 xiaoqing_chat 的功能。

#### 添加自定义后处理

```python
async def handle_smalltalk(text: str, event: Dict, context) -> List:
    """扩展现有 xiaoqing_chat"""
    
    # 调用原始 xiaoqing_chat
    raw_response = await call_original_xiaoqing_chat(text, context)
    
    # 自定义后处理
    processed = custom_post_process(raw_response, context)
    
    return segments(processed)


def custom_post_process(text: str, context) -> str:
    """自定义后处理"""
    # 添加时间戳
    from datetime import datetime
    return f"[{datetime.now().strftime('%H:%M')}] {text}"
```

#### 集成其他数据源

```python
async def handle_smalltalk(text: str, event: Dict, context) -> List:
    """集成外部数据源"""
    
    # 检查是否需要查询外部数据
    if "天气" in text or "天气" in text:
        # 调用天气 API
        weather = await fetch_weather(text, context)
        
        # 将天气信息作为上下文
        response = await generate_llm_response(
            text=text,
            context=f"当前天气：{weather}",
            history=get_history(context)
        )
        
        return segments(response)
    
    # 正常对话
    return await call_xiaoqing_chat(text, context)
```

---

## 静音控制

管理员可以让机器人在群里静音。

### 使用命令

```
/闭嘴 30      # 静音 30 分钟
/闭嘴 1h      # 静音 1 小时
/说话         # 解除静音
```

### 静音期间的行为

- ✅ 仍然响应命令
- ❌ 不进行随机回复
- ❌ 不进行闲聊

### API

```python
# 静音群
context.mute_group(group_id, duration_minutes)

# 解除静音
context.unmute_group(group_id)

# 检查是否静音
is_muted = context.is_group_muted(group_id)

# 获取剩余时间
remaining = context.get_mute_remaining(group_id)  # 分钟
```

---

## 插件间调用

### 获取其他插件

```python
async def handle(command: str, args: str, event: Dict, context) -> List:
    # 获取插件管理器
    pm = context.app.plugin_manager
    
    # 获取另一个插件
    other_plugin = pm.get("other_plugin")
    
    if other_plugin:
        # 调用其函数
        if hasattr(other_plugin.module, "some_function"):
            result = await other_plugin.module.some_function(args)
```

### 共享数据

通过文件或数据库共享数据：

```python
# 插件 A 写入
write_json(context.data_dir.parent / "shared" / "data.json", data)

# 插件 B 读取
data = load_json(Path("plugins/shared/data.json"))
```

---

## 错误处理最佳实践

### 1. 捕获特定异常

```python
async def handle(command: str, args: str, event: Dict, context) -> List:
    try:
        result = await fetch_data(args)
        return segments(result)
    except ValueError as e:
        return segments(f"参数错误: {e}")
    except aiohttp.ClientError as e:
        context.logger.warning(f"网络错误: {e}")
        return segments("网络请求失败，请稍后重试")
    except Exception as e:
        context.logger.error(f"未知错误: {e}", exc_info=True)
        return segments("发生未知错误")
```

### 2. 超时处理

```python
import asyncio

async def handle(command: str, args: str, event: Dict, context) -> List:
    try:
        result = await asyncio.wait_for(
            slow_operation(),
            timeout=10.0
        )
        return segments(result)
    except asyncio.TimeoutError:
        return segments("操作超时，请稍后重试")
```

### 3. 降级处理

```python
async def handle(command: str, args: str, event: Dict, context) -> List:
    # 尝试主 API
    try:
        return segments(await primary_api())
    except Exception:
        context.logger.warning("主 API 失败")
    
    # 降级到备用 API
    try:
        return segments(await backup_api())
    except Exception:
        context.logger.error("备用 API 也失败")
    
    # 最终降级
    return segments("服务暂时不可用")
```

---

## 性能优化

### 1. 使用缓存

```python
from functools import lru_cache
import time

# 内存缓存（简单场景）
_cache = {}
_cache_time = {}
CACHE_TTL = 300  # 5 分钟

async def get_data_cached(key: str):
    now = time.time()
    if key in _cache and now - _cache_time[key] < CACHE_TTL:
        return _cache[key]
    
    data = await fetch_data(key)
    _cache[key] = data
    _cache_time[key] = now
    return data
```

### 2. 并发请求

```python
import asyncio

async def handle(command: str, args: str, event: Dict, context) -> List:
    # 并发获取多个数据
    results = await asyncio.gather(
        fetch_data_1(),
        fetch_data_2(),
        fetch_data_3(),
        return_exceptions=True
    )
    
    # 处理结果
    ...
```

### 3. 避免阻塞

```python
from core.plugin_base import run_sync

# 阻塞操作放到线程池
result = await run_sync(blocking_function, arg1, arg2)
```

---

## 调试技巧

### 1. 启用 DEBUG 日志

```json
{"log_level": "DEBUG"}
```

### 2. 在插件中打印详细信息

```python
async def handle(command: str, args: str, event: Dict, context) -> List:
    context.logger.debug(f"收到事件: {event}")
    context.logger.debug(f"命令: {command}, 参数: {args}")
    
    # 处理逻辑
    result = ...
    
    context.logger.debug(f"返回结果: {result}")
    return result
```

### 3. 使用 test.ipynb

```python
import asyncio
from core.app import XiaoQingApp
from pathlib import Path

async def test():
    app = XiaoQingApp(Path("path/to/XiaoQing"))
    await app.start()
    
    # 模拟事件
    event = {
        "post_type": "message",
        "message_type": "private",
        "user_id": 123456,
        "message": [{"type": "text", "data": {"text": "/echo test"}}]
    }
    
    result = await app.dispatcher.handle_event(event)
    print(result)
    
    await app.stop()

asyncio.run(test())
```

---

## 部署建议

### 1. 使用 systemd（Linux）

创建 `/etc/systemd/system/xiaoqing.service`：

```ini
[Unit]
Description=XiaoQing Service
After=network.target

[Service]
Type=simple
User=xiaoqing
WorkingDirectory=/opt/xiaoqing
ExecStart=/usr/bin/python3 main.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable xiaoqing
sudo systemctl start xiaoqing
```

### 2. 使用 Docker

```dockerfile
FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "main.py"]
```

```bash
docker build -t xiaoqing .
docker run -d --name xiaoqing -v ./config:/app/config xiaoqing
```

### 3. 使用 PM2（Node.js 进程管理器）

```bash
pm2 start main.py --interpreter python3 --name xiaoqing
pm2 save
pm2 startup
```

---

## 结语

恭喜你阅读完所有文档！现在你应该对 XiaoQing 有了全面的了解。

**快速回顾**：
- [00-overview.md](00-overview.md) - 项目概览
- [01-getting-started.md](01-getting-started.md) - 快速开始
- [02-architecture.md](02-architecture.md) - 系统架构
- [03-plugin-development.md](03-plugin-development.md) - 插件开发
- [04-core-modules.md](04-core-modules.md) - 核心模块
- [05-api-reference.md](05-api-reference.md) - API 参考
- [06-configuration.md](06-configuration.md) - 配置详解
- [08-message-flow.md](08-message-flow.md) - 消息处理与并发控制
- [09-plugins.md](09-plugins.md) - 内置插件说明

如有问题，欢迎提交 Issue！
