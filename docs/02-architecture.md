# 02 - 系统架构

本章详细介绍 XiaoQing 的内部架构和工作原理。

---

## 架构总览

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

## 核心组件

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

**职责**：消息分发的核心，采用 Handler 链式处理模式。

```python
class Dispatcher:
    def __init__(self, ...):
        # Handler 链：按优先级依次尝试处理
        self._handlers: tuple[MessageHandler, ...] = (
            BotNameHandler(self),      # 1. 处理仅提及机器人名字
            CommandHandler(self),       # 2. 命令匹配
            SessionHandler(self),       # 3. 活跃会话
            SmalltalkHandler(self),    # 4. 闲聊
        )
    
    async def handle_event(self, event: Dict) -> List[Dict]:
        # 1. 并发控制
        async with self.semaphore:
            return await self._handle_event(event)
    
    async def _handle_event(self, event: Dict) -> List[Dict]:
        # 2. 解析消息
        text, user_id, group_id = normalize_message(event)
        
        # 3. 决策判断
        decision = self._make_decision(text, user_id, group_id)
        if not decision.should_process:
            return []
        
        # 4. URL 检测（全局监听）
        if url_match and not has_prefix:
            result = await url_plugin.handle_url(url, event, context)
            if result:
                return result
        
        # 5. Handler 链式处理
        for handler in self._handlers:
            result = await handler.handle(text, event, context)
            if result is not None:
                return result
        
        return []
```

**Handler 链工作原理**：

每个 Handler 实现相同的接口，按顺序尝试处理：

```python
class MessageHandler(ABC):
    @abstractmethod
    async def handle(self, text: str, event: Dict, context) -> Optional[List[Dict]]:
        """处理消息，返回消息段列表或 None（表示不处理）"""
        pass
```

**短路机制**：一旦某个 Handler 返回非 `None` 结果，后续 Handler 不会执行。

**消息处理决策树**：

```
收到消息
    │
    ├─ 私聊？─────────────────────────────────────> 进入 Handler 链
    │
    └─ 群聊？
         │
         ├─ 有命令前缀（如 /help）？─────────────> 进入 Handler 链（命令优先）
         │
         ├─ 包含机器人名字（如"小青"）？─────────> 进入 Handler 链（可闲聊）
         │
         ├─ 群被静音？─────────────────────────> 不处理（命令除外）
         │
         ├─ 活跃会话？─────────────────────────> 进入 Handler 链（会话优先）
         │
         ├─ 随机触发（random_reply_rate）？────> 进入 Handler 链（闲聊模式）
         │
         └─ 否则 ──────────────────────────────> 不处理

Handler 链处理流程：
    │
    ├─ BotNameHandler：仅机器人名字？───────────> 处理并返回
    │       │
    │       └─ 否 ────────────────────────────────> 继续下一个 Handler
    │
    ├─ CommandHandler：命令匹配成功？───────────> 处理并返回
    │       │
    │       └─ 否 ────────────────────────────────> 继续下一个 Handler
    │
    ├─ SessionHandler：活跃会话存在？───────────> 处理并返回
    │       │
    │       └─ 否 ────────────────────────────────> 继续下一个 Handler
    │
    └─ SmalltalkHandler：smalltalk_mode=True？──> 处理并返回
            │
            └─ 否 ────────────────────────────────> 返回空列表
```

**xiaoqing_chat 特殊处理**：

当 `smalltalk_provider` 设置为 `xiaoqing_chat` 时，决策逻辑特殊：
- 所有群聊消息都返回 `should_process=True` 和 `smalltalk_mode=True`
- `random_reply_rate` 配置失效
- `xiaoqing_chat` 插件内部有自己的频率控制和回复概率判断

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
        if hasattr(module, "init"):
            module.init()
    
    async def reload_plugin(self, name: str):
        """热重载插件"""
        await self.unload_plugin(name)
        self.load_plugin(self.plugins_dir / name)
    
    async def watch(self):
        """监控插件文件变化，自动重载"""
        while True:
            await asyncio.sleep(self._poll_interval)
            # 检查 mtime，如有变化则重载
```

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

## 数据流详解

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
   └─ normalize_message() 提取 text="echo hello", user_id=789, group_id=123456
   └─ _make_decision() 判断需要处理（有命令前缀）
   └─ URL 检测（无 URL，跳过）
   └─ Handler 链处理：
       ├─ BotNameHandler：不是仅机器人名字 → None
       ├─ CommandHandler：匹配成功！
       │   └─ router.resolve("echo hello") 得到 (echo插件, "hello")
       │   └─ 权限检查通过
       │   └─ 构建 context
       │   └─ 调用 echo.handle("echo", "hello", event, context)
       └─ （短路，后续 Handler 不执行）

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
   └─ Handler 链：
       ├─ BotNameHandler：None
       ├─ CommandHandler：无命令匹配 → None
       ├─ SessionHandler：发现活跃会话！
       │   └─ 调用 guess.handle_session("50", event, context, session)
       │   └─ 返回 ["太大了！"]
       └─ （短路）

3. 用户猜测正确 "42"
   └─ SessionHandler 处理
   └─ guess.handle_session() 判断正确
   └─ context.end_session() 删除会话
   └─ 返回 ["恭喜你猜对了！"]
```

---

## 并发控制

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

## 下一步

- 想开发插件？→ [03-plugin-development.md](03-plugin-development.md)
- 想了解各模块源码？→ [04-core-modules.md](04-core-modules.md)
- 想了解消息处理流程？→ [08-message-flow.md](08-message-flow.md)
