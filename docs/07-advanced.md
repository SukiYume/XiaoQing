# 🧪 07 - 高级主题

本章收纳多轮对话、定时任务、URL 解析、闲聊等进阶用法。

---

## 私有 API 依赖登记

下表集中记录运行时为兼容第三方库或跨平台清理而使用的私有 API。每一处都必须保留能力检测、异常兜底和升级回归测试；依赖升级时逐行复核，不把私有属性当作稳定契约。

| 位置 | 私有 API | 使用目的 | 保护措施 |
|---|---|---|---|
| `core/plugin_manager_support.py` | `importlib._bootstrap._get_module_lock` | 在插件热加载期间复用 CPython 模块锁 | `getattr`/异常兜底；仅作为锁获取失败时的兼容路径 |
| `core/scheduler_compat.py` | APScheduler 的内部 job store、executor、timer 和 pending-future 属性 | 在调度器重载与关闭时保留现有任务语义 | 版本探针先验证属性和调用约定；缺失属性回退到安全停机路径 |
| `plugins/jupyter/jupyter_manager.py` | `_invoke_cleanup` 的回调签名反射、Jupyter 私有清理/终止方法 | 兼容不同 Jupyter 版本的异步清理和内核终止 | 只接受受限签名；每个私有候选均有异常隔离和清理报告 |
| `plugins/codex/runner.py` | `process._transport.get_pipe_transport` | `communicate()` 失败后的跨平台管道关闭兜底 | `getattr`、类型检查和异常保护；仅在正常清理路径失败后使用 |

---

## 💬 多轮对话

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
        next_min = max(min_num, guess + 1)
        next_max = max_num
    else:
        hint = "太大了！"
        next_min = min_num
        next_max = min(max_num, guess - 1)

    def persist_attempt(working):
        working.set("remaining", remaining)
        working.set("history", history)
        working.set("min", next_min)
        working.set("max", next_max)

    await context.update_session(persist_attempt)
    
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

# 读取快照；持久修改必须走原子 callback
value = session.get("key", default)
await context.update_session(lambda working: working.set("key", value))

# 检查状态
if session.is_expired():
    ...

# 结束会话
await context.end_session()
```

---

## 后台任务队列

多轮会话不是所有“持续工作”的唯一方案。对于 Codex 这类可能执行几分钟甚至更久的任务，更合适的模式是插件自己维护业务会话和队列，然后在完成时主动发送结果。

`plugins/codex` 的设计可以作为参考：

- `/codex create <label> [cwd:<path>]` 创建一个业务会话标签，不创建框架 Session。
- `/codex <label> <任务>` 将任务加入标签队列，handler 立即返回“已收到”。
- 同一标签内任务串行运行，避免同一 Codex thread 被并发 resume；不同标签之间按 `max_parallel_jobs` 并行。
- 任务完成、失败、超时或取消后，插件用 `context.send_action()` 主动回发 `[codex:<label> #<job_id>]` 消息；如果任务生成图片，会随文字一起发送 QQ image 段。
- 会话索引保存在 `data/codex/sessions.json`，对话记录、图片副本和任务 artifacts 保存在 `data/codex/session/<label>/`，删除归档保存在 `data/codex/deleted_sessions/`，重启后可以继续知道 label、cwd 和 Codex thread id。
- 业务插件可以把自己的长任务作为侧路投递给 Codex，例如 `arxiv_filter` 在发送论文列表后，把所有 positive arXiv 链接交给固定 `astro-ph` 会话；摘要成功、失败或历史重发都不影响论文列表消息。

这种模式不会占用框架活跃会话，因此用户可以继续使用其他命令或闲聊。

---

## ⏰ 定时任务

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

公开的 `PluginContext` 不暴露 `app` 或 scheduler，也不能从回调闭包提取它。普通插件应把
所有定时任务声明在 `plugin.json` 的 `schedule` 字段中。确需动态调度时，应先在核心中定义
参数受限、可撤销并按插件授权的类型化 capability，再由 `XiaoQingApp` 显式注入；不要保存
应用实例或直接访问调度器。

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

- 私聊：命令未匹配、没有活跃会话消费时进入 smalltalk 回落
- 群聊：消息带命令前缀、包含 bot_name、@ 机器人，或 `require_bot_name_in_group=false` 时进入处理流程
- 群聊静音：只跳过 smalltalk 回落，不影响 URL-only、命令、只喊名字和活跃会话

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

## Dispatcher 消息分发顺序

Dispatcher 使用固定线性流程处理消息。插件通过约定函数接入，无需注册自定义处理链。

### 线性流程

```
消息到达 Dispatcher
    ↓
解析 MessageContext
    ↓
处理门控（私聊 / require_bot_name_in_group=false / has_prefix / 活跃会话）
    ↓
URL-only → url_parser（门控与静音之后，静音时跳过）
    ↓
只喊机器人名字或只 @ 机器人
    ↓
活跃会话并调用 handle_session()
    ↓
命令匹配并调用 handle()
    ↓
未知命令提示（仅严格命令前缀）
    ↓
smalltalk 回落并调用 handle_smalltalk()
```

### 短路规则

某个步骤返回结果后，后续步骤不会继续执行。

**示例**：用户发送 `/help`

```
1. URL-only：不是完整单 URL，跳过
2. 处理门控：has_prefix=True，放行
3. 只喊名字：否
4. 命令匹配：匹配 /help
5. 调用 help.handle() 并返回帮助信息
6. 直接返回，不进入会话或 smalltalk 回落
```

### 扩展边界

插件应优先使用现有接入点：

- `handle()`：命令处理
- `handle_session()`：多轮会话
- `handle_smalltalk()`：smalltalk 回落
- `call_bot_name_only()`：只喊机器人名字或只 @ 机器人
- `observe_message()`：观察消息、维护上下文，不直接决定 dispatcher 是否回复

如果需要新增全局消息处理能力，应在 `core/dispatcher.py` 的线性流程中显式设计步骤和优先级，并同步更新架构文档与回归测试。

### 调试

启用 DEBUG 日志可以查看 dispatcher 的关键分发决策：

```python
# config.json
{
  "log_level": "DEBUG"
}
```

日志只包含消息类型/长度/不可逆指纹、命令匹配结果、URL 主机类别、会话状态和群聊静音等分发元数据；不会包含消息正文、命令参数、URL/path、Prompt、模型响应或认证信息。`DEBUG` 也是普通日志，不能用来绕过这条边界。

---

## xiaoqing_chat 智能对话

xiaoqing_chat 提供基于 LLM 的智能对话能力。

### 核心特性

#### 1. 长期记忆 + 人物资料

`xiaoqing_chat` 会同时维护会话记忆、人物资料和摘要记忆。当前回复阶段会把最近对话、相关长期记忆、当前说话人的已知事实一起送入 prompt。

**工作原理**：
```
用户发送消息
    ↓
写入会话历史
    ↓
提取人物事实 / 主题摘要
    ↓
查询相关长期记忆
    ↓
构建回复 prompt（记忆 + 人设 + 表达习惯 + 当前目标）
    ↓
调用 LLM 生成回复并写回会话
```

相关配置主要在 `plugins/xiaoqing_chat/config/xiaoqing_config.json` 的 `memory` / `summarizer` / `goal` 段。

#### 2. 拟人化表达系统

拟人化由多层机制共同实现，单句 system prompt 只承担一部分约束。

1. `personality.identity` 和随机 `states`
2. 从对话里学到的表达方式
3. 黑话/梗解释
4. 人设提示、pre-heuristic 和 reply checker，把回复压回“短、口语、像真人”
5. PFC / goal / brain chat，让它知道这轮为什么要说话；私聊深度对话时还能切到更高 think level

#### 3. 图片与表情包进入正常对话流

启用媒体能力后，用户发来的图片、NapCat `mface`、QQ 原生 `face` 会作为有效消息进入解析。

**工作原理**：
```
用户发送图片 / 表情包
    ↓
解析 OneBot 消息段
    ↓
必要时下载或回收真实图片
    ↓
视觉模型分析成 [图片：...] / [表情包：...] / [QQ表情：...]
    ↓
按原始 segment 顺序拼回有效用户输入
    ↓
进入和纯文本相同的频率控制、记忆、回复检查链路
```

识别成表情包的图片会进入 `data/xiaoqing_chat/media/library/`，后续可作为本地表情包回复素材。

回复阶段由主 LLM 直接决定是否带出站媒体。模型可以在自然文本里附一个 `[想发表情:hint]`、`[想发QQ表情:hint]` 或 `[想发图片:hint]` marker。插件会剥离这个控制 marker，再按 hint 到本地表情包库、图片目录或 QQ face 目录里解析成实际发送段。旧图库里缺失的元数据会在后台补修，不会卡住当前回复。

#### 4. 深度对话模式与 think level

`xiaoqing_chat` 的普通回复和私聊深度对话使用不同思考强度。

- 普通模式下，`planner.think_mode = "dynamic"` 会按近期上下文长度自动映射到不同 think level
- 私聊启用 `brain_chat` 后，会优先使用 `brain_think_level`
- 如果 `private_planner_always_on = true`，深度对话模式下即使普通 planner 关闭，也会保持 planner 链开启

因此普通短对话会更轻快，私聊深聊和长上下文场景会更容易得到更深入的回复。

### 智能回复控制

xiaoqing_chat 内部通过多层门控实现智能回复频率控制。

**控制策略**：

1. **attention gate**：先判断这条消息是不是冲小青来的。`/xc`、私聊、被 `@`、直接叫名字、只喊名字后的追问、reply 引用小青、以及近期上下文锚定小青的“她/ta”共指召唤，都会跳过普通概率门。
2. **硬频控**：普通群聊参与前先检查最小回复间隔、每分钟上限、连续回复冷却。
3. **活跃话题**：只用上一轮目标判断是否活跃，再按 `active_topic_reply_probability` 和 `active_topic_min_reply_interval` 控制参与；当前消息不会先抬高自己的回复概率。
4. **soft gate**：普通群聊插话概率 `reply_probability_base` 叠加 heartflow 和连续未回复补偿。

配置边界：`reply_probability_private`、`heartflow.threshold`、`heartflow.enable_random_gate`、`heartflow.weight_mentioned`、`heartflow.weight_private`、`heartflow.weight_rate_limit`、`heartflow.weight_cooldown`、`heartflow.weight_interval` 不属于当前回复主路径。私聊、点名和共指由 attention gate 处理；速率限制由硬频控处理。

**示例**：
```python
async def handle_smalltalk(text: str, event: Dict, context) -> List:
    """示意：xiaoqing_chat 自己决定是否回复"""

    runtime = load_xiaoqing_runtime(context)
    state = get_state()
    chat_id = resolve_chat_id(event)

    attention = await decide_attention(text, event, state, chat_id)
    if not attention.forced and not await should_reply(runtime, state, chat_id, text):
        await state.heartflow.on_no_reply_async(chat_id=chat_id)
        return []

    draft = await generate_reply_draft(text, event, context)
    await save_reply_to_memory(chat_id, draft)
    return draft.parts
```

### 扩展 xiaoqing_chat

扩展 xiaoqing_chat 时，可以在现有链路上增加后处理或外部数据源。

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
    if "天气" in text:
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

> [!NOTE]
> 扩展图片或表情相关行为时，优先阅读 `plugins/xiaoqing_chat/media/event_media.py`、`emoji_library.py`、`marker_resolver.py`。当前设计是主回复模型输出至多一个出站媒体 marker，解析与落盘路径由 `marker_resolver.py` 统一处理。

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

### 声明式服务与 capability

`PluginContext` 没有 `app` 或 `plugin_manager` 属性，插件也不能按字符串获取任意模块函数。
框架只允许核心预先登记的固定服务契约：提供方在 `plugin.json` 声明服务，核心校验允许的
owner、caller 和 required capability，再向指定调用方注入类型化的
`context.capabilities.<service>`。例如 smalltalk 只能调用 `voice_synthesis.synthesize_text()` 与
`chat_reply.reply()`；arxiv_filter 只能在获授权身份下调用
`codex_arxiv_summary.enqueue_or_replay()`。新增跨插件调用必须同时扩展 manifest 模型、核心
绑定、能力协议和授权测试，不能通过反射或应用后门实现。

### 共享数据

`context.data_dir` 属于当前插件；不要通过 `parent` 越界读写另一个插件目录。需要共享数据时，
由核心定义拥有者明确、输入有界的服务 capability，或使用独立配置且具备访问控制的外部存储。

---

## 错误处理实践

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

# 阻塞操作经当前插件的有界 bulkhead 提交
result = await run_sync(blocking_function, arg1, arg2)
```

`run_sync()` 受 `plugin_execution.sync_parallel_limit`、`sync_queue_limit` 和全局队列上限约束，并按插件公平调度；过载会快速失败。不要直接使用 `asyncio.to_thread()` 或默认 executor 绕过配额和卸载隔离。只有底层组件自带专用、有界 executor，且在 `init()`/`shutdown()` 中停止接纳并有界 drain 时，才可以自行管理 worker。调用方取消只能阻止尚未启动的任务，已经运行的 Python 线程仍由框架跟踪至真实结束。

---

## 调试技巧

### 1. 启用 DEBUG 日志

```json
{"log_level": "DEBUG"}
```

### 2. 在插件中打印详细信息

```python
from core.sensitive_audit import summarize_sensitive

async def handle(command: str, args: str, event: Dict, context) -> List:
    payload = summarize_sensitive(args)
    context.logger.debug(
        "plugin request command=%s payload_length=%d payload_bytes=%d "
        "payload_fingerprint=%s",
        command,
        payload.length,
        payload.byte_length,
        payload.fingerprint,
    )

    # 处理逻辑
    result = ...

    context.logger.debug("plugin request completed command=%s", command)
    return result
```

不要打印 `event`、`args`、`result` 的原文或任意截断前缀；它们可能包含聊天内容、管理员命令、密码、Token、路径或模型生成内容。需要关联一次执行时使用 `request_id` 和 `summarize_sensitive()` 的进程内 HMAC 指纹。

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

```bash
docker build -t xiaoqing .
docker run -d --name xiaoqing -v ./config:/app/config xiaoqing
```

Dockerfile 使用 CPython 3.13，并直接安装根目录 `requirements.txt`。构建前请确认
工作区中没有不应进入镜像的本地文件，并检查 `.dockerignore`。

### 3. 使用 PM2（Node.js 进程管理器）

```bash
pm2 start main.py --interpreter python3 --name xiaoqing
pm2 save
pm2 startup
```

---

## 结语

完成 00-09 文档后，读者应能掌握 XiaoQing 的运行方式、配置路径、插件开发入口和常见排障流程。

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

后续问题可通过 Issue 跟踪。
