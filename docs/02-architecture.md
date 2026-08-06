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

### 1. XiaoQingApp（app.py 与 app_* 模块）

**职责**：`app.py` 只保留应用入口、组件装配、启动/停机与配置发布；
`app_plugin_watch.py`、`app_delivery.py`、`app_ingress.py`、`app_scheduling.py`
分别拥有插件 watcher 监督、OneBot 投递、入站端点重协商和排程发布。`app_identity.py`
中的 `AppIdentityService` 独立拥有管理员集合与 principal authority；`app_support.py`
保留无状态解析和生命周期记录。`XiaoQingApp` 负责装配并委托这些职责，对外类名和
调用契约不变。

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
Step A: 处理门控（私聊、require_bot_name_in_group=False、has_prefix、活跃 session）
        · 先 router.resolve；再按敏感类别决定是否 observe_message
Step B: is_url_only → url_parser（在门控与静音之后；静音时跳过）
Step C: is_only_bot_name → 默认回应 / call_bot_name_only
Step D: 活跃 session → 转 session 插件
Step E: router 命中 → 执行命令
Step F: has_command_prefix 且命令未命中且首字母为字母 → 未知命令提示
Step G: 回落 smalltalk provider（mute 仅在此步及普通群闲聊阻塞）
```

`xiaoqing_chat` 在命令解析之后、按敏感类别通过 `observe_message()` 观察消息（命令、URL、活跃会话和命令前缀输入不进入观察），是否实际回复由插件内部的 attention gate、硬频控、普通群聊插话概率、heartflow 和 PFC planner 决定。

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

### 4. PluginManager（plugin_manager.py 与 plugin_* 模块）

**职责**：`plugin_manager.py` 是装配与命名空间所有权门面；导入屏障和共享事务记录位于
`plugin_manager_support.py`，生命周期/代际发布位于 `plugin_generation.py`，目录扫描与
watcher 位于 `plugin_watcher.py`，执行 gate/服务绑定位于 `plugin_runtime.py`，外置数据
目录位于 `plugin_data.py`。所有模块共享同一组状态和事务，没有第二套加载或回滚路径。

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
        
        # 3. 构造命令规范；候选插件通过全部校验后再原子发布到 Router
        command_specs = self._build_command_specs(definition, module)
        self.router.replace_plugin(definition.name, command_specs)
        
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

> 说明：应用启动时会自动创建配置 watcher；插件 watcher 仅在 `config.json` 里启用
> `enable_plugin_watcher` 且当前解释器通过模块导入屏障行为探针后才会启动。探针失败
> 不影响进程启动和首次插件加载，但插件变更只能通过重启生效；手动 reload 也会明确
> 报告 restart-only 模式。插件异步 `init()` 在重载路径上会被等待；如果初始化失败，
> 半加载插件会被立即卸载，避免继续接流量。

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
    session_id: str          # create/replace 时生成，普通 update 保留
    data: Dict[str, SessionValue]  # 有界、字符串键的 JSON-like 值树
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
        """获取隔离快照、刷新空闲超时（自动清理过期）"""

    async def update(self, user_id, group_id, callback):
        """在受控值树克隆的工作副本上执行一次可回滚事务"""
        
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
5. Dispatcher 在 SessionManager.update() 工作副本中调用插件；插件使用 session.set()
       │
       ├─ 继续对话 ──> 回到步骤 4
       │
       └─ 对话结束 ──> context.end_session()
                           │
                           ▼
                      会话被删除
```

**关键不变量**：正式会话从不直接暴露给插件；读取返回隔离快照，更新只在每键事务锁内
提交一次。值树限制深度、节点数和内建类型，显式拒绝引用环及自定义
`__deepcopy__`，这是防止插件状态破坏回滚或造成无界资源消耗的安全边界。事务 callback
被取消时，管理器会先取消并 drain 唯一的实际 Future，再决定回滚，避免请求返回后仍有
后台 callback 提交“幽灵状态”。这些约束属于正确性契约，不应为简化代码而移除。

---

### 6. SchedulerManager（scheduler.py）

**职责**：管理定时任务。

```python
class SchedulerManager:
    async def shutdown_async(self, *, wait: bool = True):
        """停止接纳，取消协程任务并有界等待真实 Future 收敛"""
    
    def replace_prefix(self, prefix: str, specs: Iterable[ScheduledJobSpec]):
        """验证整批声明后事务式替换；失败时恢复原快照"""
    
    async def reset_async(self, timezone: str | None = None):
        """旧代完整关闭后才发布新时区 scheduler"""
```

APScheduler 的公开关闭 API 无法证明抵抗取消的协程或线程任务已经真正结束。为保持
“旧代未排空就不得发布新代”的所有权不变量，`scheduler_compat.py` 把 3.x 的私有锁、
Future 注册表、timer 和事件字段隔离在一个适配器内，并在每个真实 scheduler 上先探测
完整布局。探测通过时继续执行可重试的精确 drain；探测失败时记录版本与缺失能力，
降级为 APScheduler 公开 shutdown，不会在模块导入期拒绝启动。依赖范围是
`apscheduler>=3.11,<4`；升级时必须运行能力探针、公开降级、取消、线程任务 drain、
清理失败重试和事务回滚测试。

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
        """按认证代际连接，并在断开后执行有界退避。"""
        while self._running:
            connected_seconds = await self._connect_once(handler)
            await self._wait_for_reconnect(self._next_backoff(connected_seconds))
    
    async def send_action(self, action: Dict):
        """通过 WS 发送"""
        await self._ws.send(json.dumps(action))
```

连接器在启动和热更新后的下一次连接前检查 `websockets.connect` 的真实签名与参数类型：新版通过 `additional_headers`、旧版通过 `extra_headers` 发送 Bearer token；无法证明认证头能作为关键字参数传递时直接 fail closed。正常关闭与异常关闭共用带连续抖动的 5–60 秒指数退避，稳定运行 30 秒才复位，地址/token 代际变化会同时唤醒退避和连接阶段。每个 socket、close task 与 connection attempt 都按对象/代际独立拥有；旧代拒绝取消或关闭失败时进入隔离集合，不会阻塞新代连接，并在停机的单一绝对期限内并发回收。

认证状态还包含“凭据来源可信”位。VALID secrets 中缺省或明确的空字符串表示操作者选择匿名 OneBot；secrets 为 MISSING/INVALID/UNAVAILABLE/INCONSISTENT，或 token 不是精确字符串时则表示撤权，HTTP 不发请求、WebSocket 不调用 `connect`，直到新的 VALID revision 恢复。同步安全发布会验证每个 holder 确实执行了 endpoint/token/trust 更新；仅返回成功但未改变状态的旧实现会立即被摘除或隔离。Inbound token 与管理员列表也只读取同一个可信 secrets 视图，来源异常时分别进入全拒绝和空权限状态。

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
   └─ Step A 处理门控通过（has_prefix=True）
   └─ ctx.is_url_only=False，跳过 Step B URL 解析
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
   └─ Step D 发现活跃会话
   └─ 调用 guess.handle_session("50", event, context, session)
   └─ 返回 ["太大了！"]

3. 用户猜测正确 "42"
   └─ Step D 会话处理
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
        ├── PendoRuntimeService（数据库、配置订阅、Web 生命周期所有权）
        └── 插件初始化或 /pendo web start
                └── FastAPI Web Server（uvicorn）
                        ├── /api/*  # REST API（JWT 鉴权、CRUD、统计、Bundle、widget）
                        └── /*      # 静态 SPA 文件
```

**特点**：
- Web Server 在独立后台线程中运行，不阻塞消息处理
- `PendoRuntimeService` 是内部 service boundary：数据库、配置订阅和自动 Web 生命周期按同一插件代获取与释放，`main.py` 只做插件钩子编排
- 插件初始化会尝试自动启动；也可以通过 `/pendo web start` 手动重试，通过 `/pendo web stop` 关闭
- 应用退出、插件卸载或 `Ctrl+C` 时，会先请求 Pendo Web 优雅停止，再清理数据库和运行时状态
- 支持通过 nginx 在子路径（如 `/pendo/`）下反向代理访问
- Pendo Web 与聊天命令共用 `plugins/pendo/services/db.py`、`utils/validators.py` 和事件图/提醒服务，避免 Web 与 CLI 各自维护一套字段语义

这仍是单进程部署边界：Bot 重启会重启 Pendo Web。内部 service boundary 用来隔离所有权、回滚和测试，不宣称进程故障隔离；若未来确实需要独立扩容或发布，可在保持共享服务契约的前提下再拆独立进程。

另一类独立服务是 **codex** 插件的后台队列。它不使用 `SessionManager` 捕获用户后续消息，而是在插件内部维护 `label -> session/thread/queue`：

- `/codex create <label> [cwd:<path>]` 创建业务会话标签
- `/codex <label> <任务>` 将任务放入该标签队列，handler 立即返回“已收到”
- 同一标签内任务串行执行，不同标签受 `max_parallel_jobs` 限制并行执行
- 任务完成后通过 `context.send_action()` 主动发送文字和图片结果，底层仍走统一 OneBot 发送链路
- 会话索引保存在 `data/codex/sessions.json`，每个标签的记录、图片和任务 artifacts 保存在 `data/codex/session/<label>/`，删除会话时旧目录会归档到 `data/codex/deleted_sessions/`

`arxiv_filter` 的每日摘要就是这个模式的业务化用法：筛选插件先返回论文列表，再通过后台侧路把所有 positive arXiv 链接投递到 Codex `astro-ph` 会话。`astro-ph` 首次没有 Codex thread 时会先执行静默初始化任务，之后摘要任务复用同一 thread 和工作目录中的 `arxiv-summary-methodology.md`。历史结果和在途任务只有在 arXiv 源列表日期及规范化论文集合都相同时才会复用。

这种方式适合耗时较长但不应占用 bot 多轮会话的后台工作。

---

## ➡️ 下一步

- 插件开发见 [03-plugin-development.md](03-plugin-development.md)
- 核心模块源码见 [04-core-modules.md](04-core-modules.md)
- 消息处理流程见 [08-message-flow.md](08-message-flow.md)
