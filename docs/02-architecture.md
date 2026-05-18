# 🏗️ 02 - 系统架构

本章把 XiaoQing 的内部架构和工作原理拆开说明。

> [!NOTE]
> 本章偏向框架内部实现。只写普通插件时，可以先阅读 [03-plugin-development.md](03-plugin-development.md)。

---

## 🔭 架构总览

XiaoQing 的核心架构分成三层。

1. **协议接入层**：`server.py` 和 `onebot.py` 负责接收 OneBot 事件、维护 WebSocket 连接和发送 OneBot API 请求。
2. **框架调度层**：`app.py`、`dispatcher.py`、`router.py`、`plugin_manager.py`、`session.py`、`scheduler.py` 负责生命周期、消息分发、命令匹配、插件加载、多轮会话和定时任务。
3. **插件业务层**：`plugins/` 内的插件实现具体能力。轻量插件通常只需要 `plugin.json + main.py`；大型插件如 `xiaoqing_chat`、`pendo` 和 `codex` 拥有自己的服务层、状态层、Web/API、LLM 子系统或后台任务队列。

核心框架不直接理解 Pendo 的账本模型，也不直接生成 xiaoqing_chat 的拟人回复，也不调度 Codex CLI 的内部任务队列。它提供统一的事件、上下文、路由和发送能力；业务插件在这个边界内自行组织更复杂的内部架构。

```
                              ┌─────────────────┐
                              │   QQ 服务器     │
                              └────────┬────────┘
                                       │
                              ┌────────▼────────┐
                              │  OneBot 实现    │
                              │  (NapCat等)     │
                              └────────┬────────┘
                                       │
              ┌────────────────────────┼────────────────────────┐
              │                        │                        │
              ▼                        ▼                        ▼
    ┌─────────────────┐    ┌─────────────────┐    ┌─────────────────┐
    │   HTTP POST     │    │   WebSocket     │    │   HTTP API      │
    │  (事件推送)     │    │  (双向通信)     │    │  (发送消息)     │
    └────────┬────────┘    └────────┬────────┘    └────────▲────────┘
             │                      │                      │
             │                      │                      │
┌────────────┼──────────────────────┼──────────────────────┼────────────┐
│            │         XiaoQing 框架    │                      │            │
│            ▼                      ▼                      │            │
│  ┌─────────────────┐    ┌─────────────────┐             │            │
│  │ InboundServer   │    │ OneBotWsClient  │             │            │
│  │ (server.py)     │    │ (onebot.py)     │             │            │
│  └────────┬────────┘    └────────┬────────┘             │            │
│           │                      │                      │            │
│           └──────────┬───────────┘                      │            │
│                      │ 事件                             │            │
│                      ▼                                  │            │
│           ┌─────────────────────────────────────────────┤            │
│           │         Dispatcher (dispatcher.py)          │            │
│           │  • 消息解析                                 │            │
│           │  • 触发条件判断                             │            │
│           │  • 会话管理                                 │            │
│           │  • 命令/闲聊路由                           │            │
│           └────────────────┬────────────────────────────┘            │
│                            │                                         │
│                            ▼                                         │
│           ┌─────────────────────────────────────────────┐            │
│           │            Router (router.py)               │            │
│           │  • 命令触发词匹配                           │            │
│           │  • 优先级排序                               │            │
│           └────────────────┬────────────────────────────┘            │
│                            │                                         │
│                            ▼                                         │
│           ┌─────────────────────────────────────────────┐            │
│           │       PluginManager (plugin_manager.py)      │            │
│           │  • 插件加载/卸载                            │            │
│           │  • 热重载监控                               │            │
│           │  • Context 构建                             │            │
│           └────────────────┬────────────────────────────┘            │
│                            │                                         │
│                            ▼                                         │
│           ┌─────────────────────────────────────────────┐            │
│           │           Plugin.handle()                    │            │
│           │           你的插件代码                       │            │
│           └────────────────┬────────────────────────────┘            │
│                            │                                         │
│                            │ 消息段                                  │
│                            ▼                                         │
│           ┌─────────────────────────────────────────────┐            │
│           │        OneBotHttpSender (onebot.py)         ├────────────┘
│           │           发送响应消息                       │
│           └─────────────────────────────────────────────┘
│
│  ┌─────────────────┐    ┌─────────────────┐    ┌─────────────────┐
│  │ SessionManager  │    │ SchedulerManager│    │ ConfigManager   │
│  │ (session.py)    │    │ (scheduler.py)  │    │ (config.py)     │
│  │ 多轮对话管理    │    │ 定时任务管理    │    │ 配置热重载      │
│  └─────────────────┘    └─────────────────┘    └─────────────────┘
│
└──────────────────────────────────────────────────────────────────────┘
```

---

## ⚙️ 核心组件

在当前项目中，`core/` 的职责边界保持稳定。它处理所有插件共享的通用问题，不把某个业务插件的规则写进核心。`smalltalk_provider = "xiaoqing_chat"` 时，核心先调用插件的 `observe_message()` 观察消息；只有通过 dispatcher 门控并落到 smalltalk 回落时，才由聊天插件判断是否实际回复。

### 1. XiaoQingApp（app.py）

**职责**：应用入口，管理所有组件的生命周期。

```python
class XiaoQingApp:
    def __init__(self, root: Path):
        # 初始化配置
        self.config_manager = ConfigManager(...)
        
        # 初始化各组件
        self.router = CommandRouter()
        self.plugin_manager = PluginManager(...)
        self.scheduler = SchedulerManager(...)
        self.session_manager = SessionManager(...)
        self.dispatcher = Dispatcher(...)
        
    async def start(self):
        # 1. 初始化并发控制
        concurrency = self.config.get("max_concurrency", 5)
        self.dispatcher.semaphore = asyncio.Semaphore(concurrency)

        # 2. 创建 HTTP 会话
        self.http_session = aiohttp.ClientSession()
        
        # 3. 加载所有插件
        self.plugin_manager.load_all()
        
        # 4. 启动通信服务
        if enable_ws_client:
            self.ws_client.connect_and_listen(...)
        if enable_inbound_server:
            self.inbound_server.start()
            
    async def stop(self):
        # 优雅关闭所有组件
        if self.ws_client:
            await self.ws_client.stop()
        # ...
```

**关键属性**：
- `config` - 配置字典
- `secrets` - 敏感配置
- `is_admin(user_id)` - 判断是否管理员

---

### 2. Dispatcher（dispatcher.py）

**职责**：消息分发的核心。Dispatcher 使用单个线性流程处理消息，处理状态保存在 `MessageContext` 与局部控制流中。

```python
class Dispatcher:
    async def _process_event(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        ctx = self.parser.parse(event)
        if ctx is None:
            return []

        await self._observe_message(ctx)

        if ctx.is_url_only:
            return await self._invoke_url_parser(ctx, ctx.clean_text.strip()) or []

        should_process = (
            ctx.is_private
            or not config.get("require_bot_name_in_group", True)
            or ctx.has_prefix
            or has_active_session(ctx)
        )
        if not should_process:
            return []

        if ctx.is_only_bot_name:
            return await self._handle_bot_name_only(ctx)

        resolved = self.router.resolve(ctx.clean_text)
        if resolved:
            return await self._execute_command(resolved, ctx) or []

        if ctx.has_command_prefix:
            return unknown_command_hint(ctx)

        if ctx.cached_session:
            return await self._try_handle_session(ctx) or []

        if self.is_muted(ctx.group_id):
            return []

        return await self._handle_smalltalk(ctx)
```

**关键解析信号**：

- `has_prefix` 表示消息以命令前缀（默认 `/`）开头，或包含 `bot_name`（任意位置），或包含 @机器人（任意位置）。
- `has_command_prefix` 单独标识严格以命令前缀开头。未知命令提示只看这个字段。
- URL 处理改用 `ctx.is_url_only`：仅当 `clean_text` strip 后整体匹配 `^https?://\S+$` 时调度到 `url_parser`。文本中夹带 URL 不会触发 URL 短路。

**线性处理顺序**：

```
Step A: URL short-circuit（clean_text 单 URL → url_parser；mute 不影响）
Step B: 处理门控（私聊、require_bot_name_in_group=False、has_prefix、活跃 session）
Step C: is_only_bot_name → 默认回应 / call_bot_name_only
Step D: router 命中 → 执行命令
Step E: has_command_prefix 且命令未命中 → 未知命令提示
Step F: 活跃 session → 转 session 插件
Step G: 回落 smalltalk provider（mute 仅在此步阻塞）
```

`xiaoqing_chat` 仍然通过 `observe_message()` 观察已解析消息，是否实际回复由插件内部的 attention gate、硬频控、普通群聊插话概率、heartflow 和 PFC planner 决定。

---

### 3. Router（router.py）

**职责**：根据触发词匹配命令。

```python
@dataclass
class CommandSpec:
    plugin: str       # 所属插件名
    name: str         # 命令名
    triggers: List[str]  # 触发词列表
    help_text: str    # 帮助文本
    admin_only: bool  # 是否仅管理员
    handler: Handler  # 处理函数
    priority: int     # 优先级

class CommandRouter:
    def register(self, spec: CommandSpec):
        """注册命令"""
        self._commands.append(spec)
        
    def resolve(self, text: str) -> Optional[Tuple[CommandSpec, str]]:
        """解析命令"""
        # 按优先级和触发词长度排序（长的优先）
        for spec in sorted_commands:
            for trigger in spec.triggers:
                if text.startswith(trigger):
                    args = text[len(trigger):].strip()
                    return spec, args
        return None
```

**优先级规则**：
1. `priority` 数值越大越优先
2. 同优先级时，触发词越长越优先（避免 `help` 抢走 `helpme` 的匹配）

---

### 4. PluginManager（plugin_manager.py）

**职责**：管理插件的加载、卸载和热重载。

```python
class PluginManager:
    def load_all(self):
        """加载 plugins/ 下所有插件"""
        for plugin_dir in self.plugins_dir.iterdir():
            if self._is_plugin_dir(plugin_dir):
                self.load_plugin(plugin_dir)
    
    def load_plugin(self, plugin_dir: Path):
        """加载单个插件"""
        # 1. 读取 plugin.json
        definition = self._load_definition(plugin_dir)
        
        # 2. 导入 main.py 模块
        module = self._load_module(plugin_dir, definition)
        
        # 3. 注册命令到 Router
        self._register_commands(definition, module)
        
        # 4. 调用 init() 钩子（如果存在）
        #    若返回协程，会被纳入 init task 跟踪并等待完成
        if hasattr(module, "init"):
            result = module.init()
            if asyncio.iscoroutine(result):
                ...
    
    async def reload_plugin(self, name: str):
        """热重载插件"""
        await self.unload_plugin(name)
        self.load_plugin(self.plugins_dir / name)
        await self.wait_inits()
    
    async def watch(self):
        """监控插件文件变化，自动重载"""
        while True:
            await asyncio.sleep(self._poll_interval)
            # 检查 mtime，如有变化则重载
```

> 说明：应用启动时会自动创建配置 watcher；插件 watcher 仅在 `config.json` 里启用 `enable_plugin_watcher` 后才会启动。插件异步 `init()` 在重载路径上也会被等待；如果初始化失败，半加载插件会被立即卸载，避免继续接流量。

**插件加载流程**：

```
plugins/echo/
    │
    ├── plugin.json ──> PluginDefinition
    │                   (name, version, commands, schedule...)
    │
    └── main.py ──────> Module
                        (handle, init, shutdown...)
                             │
                             ▼
                      Router.register(CommandSpec)
```

---

### 5. SessionManager（session.py）

**职责**：管理多轮对话的会话状态。

```python
@dataclass
class Session:
    user_id: int
    group_id: Optional[int]  # None = 私聊
    plugin_name: str         # 所属插件
    data: Dict[str, Any]     # 会话数据
    timeout: float           # 超时时间
    
    def get(self, key, default=None): ...
    def set(self, key, value): ...
    def is_expired(self) -> bool: ...

class SessionManager:
    # 会话存储：(user_id, group_id) -> Session
    _sessions: Dict[tuple, Session]
    
    async def create(self, user_id, group_id, plugin_name, initial_data, timeout):
        """创建新会话"""
        
    async def get(self, user_id, group_id) -> Optional[Session]:
        """获取会话（自动清理过期）"""
        
    async def delete(self, user_id, group_id) -> bool:
        """删除会话"""
```

**会话生命周期**：

```
1. 用户发送命令（如 /猜数字）
       │
       ▼
2. 插件调用 context.create_session()
       │
       ▼
3. 会话创建，存储初始数据
       │
       ▼
4. 用户后续消息被路由到 handle_session()
       │
       ▼
5. 插件更新会话数据 session.set()
       │
       ├─ 继续对话 ──> 回到步骤 4
       │
       └─ 对话结束 ──> context.end_session()
                           │
                           ▼
                      会话被删除
```

---

### 6. SchedulerManager（scheduler.py）

**职责**：管理定时任务。

```python
class SchedulerManager:
    def __init__(self, timezone: str):
        self.scheduler = AsyncIOScheduler(timezone=timezone)
        self.scheduler.start()
    
    def add_job(self, job_id: str, func, cron: Dict):
        """添加定时任务"""
        self.scheduler.add_job(func, trigger="cron", id=job_id, **cron)
    
    def remove_job(self, job_id: str):
        """移除任务"""
        
    def clear_prefix(self, prefix: str):
        """移除某前缀的所有任务（用于插件卸载）"""
```

**Cron 表达式示例**：

```python
# 每天 8:00
{"hour": 8, "minute": 0}

# 每 2 小时
{"hour": "*/2"}

# 工作日 9:00
{"day_of_week": "mon-fri", "hour": 9}

# 每月 1 号 0:00
{"day": 1, "hour": 0, "minute": 0}
```

---

### 7. OneBot 通信（onebot.py + server.py）

**两种通信方式**：

#### OneBotHttpSender - 发送消息

```python
class OneBotHttpSender:
    async def send_action(self, action: Dict):
        """发送 OneBot Action"""
        url = f"{self.http_base}/{action['action']}"
        await self.session.post(url, json=action['params'], headers=headers)
```

#### OneBotWsClient - WebSocket 双向通信

```python
class OneBotWsClient:
    async def connect_and_listen(self, handler):
        """连接并持续监听"""
        async with websockets.connect(self.ws_uri) as ws:
            async for message in ws:
                event = json.loads(message)
                await handler(event)
    
    async def send_action(self, action: Dict):
        """通过 WS 发送"""
        await self._ws.send(json.dumps(action))
```

#### InboundServer - 被动接收

```python
class InboundServer:
    """HTTP 服务器，接收 OneBot 推送"""
    
    async def post_event(self, request):
        """POST /event - 接收事件"""
        payload = await request.json()
        actions = await self.handler(payload)
        return web.json_response({"actions": actions})
    
    async def ws_handler(self, request):
        """WebSocket 端点"""
        # 持久连接处理
```

---

## 🔄 数据流详解

### 完整请求流程

```
1. OneBot 推送事件
   POST http://127.0.0.1:12000/event
   {
     "post_type": "message",
     "message_type": "group",
     "group_id": 123456,
     "user_id": 789,
     "message": [{"type": "text", "data": {"text": "/echo hello"}}]
   }

2. InboundServer 接收
   └─ 验证 Authorization Token
   └─ 解析 JSON
   └─ 调用 handler(event)

3. Dispatcher 处理
   └─ MessageParser.parse() 构建 MessageContext
   └─ ctx.has_command_prefix=True，ctx.clean_text="echo hello"
   └─ ctx.is_url_only=False，跳过 URL 短路
   └─ 处理门控通过
   └─ router.resolve("echo hello") 得到 (echo插件, "hello")
   └─ 权限检查通过
   └─ 构建 context
   └─ 调用 echo.handle("echo", "hello", event, context)

4. 插件处理
   └─ 返回 [{"type": "text", "data": {"text": "hello"}}]

5. 构建响应
   └─ build_action(segs, user_id, group_id)
   └─ {
        "action": "send_group_msg",
        "params": {
          "group_id": 123456,
          "message": [{"type": "text", "data": {"text": "hello"}}]
        }
      }

6. 返回给 OneBot
   └─ InboundServer 返回 {"actions": [...]}
   └─ OneBot 执行 action，发送消息到 QQ
```

### 会话处理流程示例

```
1. 用户发送 /guess 启动猜数字游戏
   └─ guess.handle() 创建会话
   └─ context.create_session(initial_data={"target": 42})

2. 用户后续消息 "50"
   └─ Dispatcher 处理
   └─ 命令未命中
   └─ Step F 发现活跃会话
   └─ 调用 guess.handle_session("50", event, context, session)
   └─ 返回 ["太大了！"]

3. 用户猜测正确 "42"
   └─ Step F 会话处理
   └─ guess.handle_session() 判断正确
   └─ context.end_session() 删除会话
   └─ 返回 ["恭喜你猜对了！"]
```

---

## ⚡ 并发控制

XiaoQing 使用 `asyncio.Semaphore` 控制并发：

```python
# app.py
concurrency = int(config.get("max_concurrency", 5))
self.dispatcher = Dispatcher(..., semaphore=asyncio.Semaphore(concurrency))

# dispatcher.py
async def handle_event(self, event):
    async with self.semaphore:  # 最多同时处理 5 条消息
        return await self._handle_event(event)
```

---

## 🧩 插件内嵌服务

部分插件可以在框架之外独立运行附加服务。典型案例是 **pendo** 插件：

```
XiaoQing 主进程
├── 正常消息处理流程（Dispatcher → Plugin）
└── pendo 插件（main.py）
        └── 插件初始化或 /pendo web start
                └── FastAPI Web Server（uvicorn）
                        ├── /api/*  # REST API（JWT 鉴权、CRUD、统计、Bundle、widget）
                        └── /*      # 静态 SPA 文件
```

**特点**：
- Web Server 在独立后台线程中运行，不阻塞消息处理
- 插件初始化会尝试自动启动；也可以通过 `/pendo web start` 手动重试，通过 `/pendo web stop` 关闭
- 应用退出、插件卸载或 `Ctrl+C` 时，会先请求 Pendo Web 优雅停止，再清理数据库和运行时状态
- 支持通过 nginx 在子路径（如 `/pendo/`）下反向代理访问
- Pendo Web 与聊天命令共用 `plugins/pendo/services/db.py`、`utils/validators.py` 和事件图/提醒服务，避免 Web 与 CLI 各自维护一套字段语义

另一类独立服务是 **codex** 插件的后台队列。它不使用 `SessionManager` 捕获用户后续消息，而是在插件内部维护 `label -> session/thread/queue`：

- `/codex create <label> [cwd:<path>]` 创建业务会话标签
- `/codex <label> <任务>` 将任务放入该标签队列，handler 立即返回“已收到”
- 同一标签内任务串行执行，不同标签受 `max_parallel_jobs` 限制并行执行
- 任务完成后通过 `context.send_action()` 主动发送文字和图片结果，底层仍走统一 OneBot 发送链路
- 会话索引保存在 `plugins/codex/data/sessions.json`，每个标签的记录、图片和任务 artifacts 保存在 `plugins/codex/data/session/<label>/`

这种方式适合耗时较长但不应占用 bot 多轮会话的后台工作。

---

## ➡️ 下一步

- 插件开发见 [03-plugin-development.md](03-plugin-development.md)
- 核心模块源码见 [04-core-modules.md](04-core-modules.md)
- 消息处理流程见 [08-message-flow.md](08-message-flow.md)
