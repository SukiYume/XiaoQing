# 04 - 核心模块详解

本章深入分析 XiaoQing 核心模块的源码实现。

---

## 模块概览

| 模块 | 文件 | 职责 |
|------|------|------|
| 应用主类 | `app.py` | 生命周期管理，组件编排 |
| 消息分发 | `dispatcher.py` | 解析消息，路由到插件 |
| 命令路由 | `router.py` | 匹配触发词 |
| 插件管理 | `plugin_manager.py` | 加载/卸载/热重载 |
| 插件上下文 | `context.py` | 插件运行环境 |
| 插件工具 | `plugin_base.py` | 消息构建等工具函数 |
| 会话管理 | `session.py` | 多轮对话状态 |
| 定时任务 | `scheduler.py` | APScheduler 封装 |
| 配置管理 | `config.py` | 配置加载和热重载 |
| OneBot 通信 | `onebot.py` | HTTP/WS 客户端 |
| 服务器 | `server.py` | Inbound HTTP/WS 服务 |
| 消息处理 | `message.py` | 消息解析工具 |
| 日志配置 | `logging_config.py` | 日志系统 |

---

## app.py - 应用主类

### 核心结构

```python
class XiaoQingApp:
    """XiaoQing 主应用类"""
    
    def __init__(self, root: Path) -> None:
        self.root = root
        
        # 配置管理
        self.config_manager = ConfigManager(
            root / "config" / "config.json",
            root / "config" / "secrets.json",
        )
        
        # 日志系统
        self.log_manager = setup_logging(self.config_manager.config, ...)
        
        # HTTP 会话（所有组件共享）
        self.http_session: Optional[aiohttp.ClientSession] = None
        
        # 核心组件
        self.router = CommandRouter()
        self.plugin_manager = PluginManager(...)
        self.scheduler = SchedulerManager(...)
        self.session_manager = SessionManager(...)
        self.dispatcher = Dispatcher(...)
        
        # OneBot 通信
        self.http_sender: Optional[OneBotHttpSender] = None
        self.ws_client: Optional[OneBotWsClient] = None
        self.inbound_server: Optional[InboundServer] = None
```

### 生命周期方法

```python
async def start(self) -> None:
    """启动应用"""
    # 1. 初始化并发控制 (延迟初始化以免在无循环时报错)
    concurrency = int(self.config.get("max_concurrency", 5))
    self.dispatcher.semaphore = asyncio.Semaphore(concurrency)

    # 2. 创建共享 HTTP 会话
    self.http_session = aiohttp.ClientSession()
    
    # 3. 初始化 HTTP 发送器
    self.http_sender = OneBotHttpSender(...)
    
    # 4. 加载所有插件
    self.plugin_manager.load_all()
    self._reschedule("startup")  # 注册定时任务
    
    # 5. 启动 WS 客户端（可选）
    if self.config.get("enable_ws_client"):
        self.ws_client = OneBotWsClient(...)
        asyncio.create_task(self.ws_client.connect_and_listen(...))
    
    # 5. 启动 Inbound 服务器（可选）
    if self.config.get("enable_inbound_server"):
        # inbound_http_base 非空则启动 HTTP Inbound
        # inbound_ws_uri 非空则启动 WS Inbound（可与 HTTP 使用不同端口）
        ...

async def stop(self) -> None:
    """优雅停止"""
    # 1. 停止 WS 客户端
    if self.ws_client:
        await self.ws_client.stop()
    
    # 2. 停止定时任务
    self.scheduler.scheduler.shutdown(wait=True)
    
    # 3. 卸载所有插件（触发 shutdown 钩子）
    for name in self.plugin_manager.list_plugins():
        await self.plugin_manager.unload_plugin(name)
    
    # 4. 关闭 HTTP 会话
    if self.http_session:
        await self.http_session.close()
```

### 属性代理

供 Dispatcher 使用的便捷属性：

```python
@property
def config(self) -> Dict[str, Any]:
    return self.config_manager.config

@property
def secrets(self) -> Dict[str, Any]:
    return self.config_manager.secrets

def is_admin(self, user_id: Optional[int]) -> bool:
    """判断是否管理员"""
    admin_ids = self.secrets.get("admin_user_ids", [])
    return int(user_id) in [int(x) for x in admin_ids]
```

---

## dispatcher.py - 消息分发器

### 核心逻辑

框架引入了 Handler 链式处理模式，采用责任链模式来处理消息。

```python
class Dispatcher:
    def __init__(self, router, app, build_context, semaphore, session_manager):
        self.router = router
        self.app = app
        self.build_context = build_context
        self.semaphore = semaphore
        self.session_manager = session_manager
        self._muted_groups: Dict[int, float] = {}  # 静音管理
        
        # Handler 链：按优先级依次尝试处理
        self._handlers: tuple[MessageHandler, ...] = (
            BotNameHandler(self),      # 1. 处理仅提及机器人名字
            CommandHandler(self),       # 2. 命令匹配
            SessionHandler(self),       # 3. 活跃会话
            SmalltalkHandler(self),    # 4. 闲聊
        )
    
    async def handle_event(self, event: Dict) -> List[Dict]:
        """处理事件（带并发控制）"""
        async with self.semaphore:
            return await self._handle_event(event)
    
    async def _handle_event(self, event: Dict) -> List[Dict]:
        # 1. 仅处理消息事件
        if event.get("post_type") != "message":
            return []
        
        # 2. 解析消息
        text, user_id, group_id = normalize_message(event)
        
        # 3. 决策判断
        decision = self._make_decision(text, user_id, group_id)
        if not decision.should_process:
            return []
        
        # 4. URL 检测（全局监听）
        if url_match and not has_prefix:
            result = await self._handle_url(url, event)
            if result:
                return result
        
        # 5. Handler 链式处理
        for handler in self._handlers:
            result = await handler.handle(text, event, context)
            if result is not None:
                return result
        
        return []
```

### Handler 链式处理

框架引入了责任链模式，将消息处理逻辑分解为独立的 Handler。

#### MessageHandler 基类

```python
class MessageHandler(ABC):
    """Handler 基类"""
    
    def __init__(self, dispatcher: Dispatcher):
        self.dispatcher = dispatcher
    
    @abstractmethod
    async def handle(
        self,
        text: str,
        event: Dict[str, Any],
        context: PluginContext
    ) -> Optional[List[Dict[str, Any]]]:
        """
        处理消息
        
        返回:
            - List[Dict]: 消息段列表，表示处理成功
            - None: 不处理，传递给下一个 Handler
        """
        pass
```

#### BotNameHandler

处理仅提及机器人名字的消息。

```python
class BotNameHandler(MessageHandler):
    """处理仅机器人名字"""
    
    async def handle(self, text: str, event: Dict, context) -> Optional[List]:
        # 检查文本是否仅包含 bot_name
        bot_name = context.config.get("bot_name", "")
        
        # 去除标点和空格
        cleaned = text.strip("，。！？、,.!? ")
        
        if cleaned == bot_name:
            # 返回帮助或问候
            return segments(
                f"你好！我是 {bot_name}\n"
                f"发送 /help 查看可用命令"
            )
        
        return None
```

#### CommandHandler

匹配并执行命令。

```python
class CommandHandler(MessageHandler):
    """命令匹配和执行"""
    
    async def handle(self, text: str, event: Dict, context) -> Optional[List]:
        # 1. 剥离前缀
        clean_text = self.dispatcher._strip_prefix(text, event, context)
        if clean_text is None:
            return None
        
        # 2. 路由匹配
        resolved = self.dispatcher.router.resolve(clean_text)
        if not resolved:
            return None
        
        # 3. 权限检查
        spec, args = resolved
        if spec.admin_only and not context.dispatcher.is_admin(event.get("user_id")):
            return segments("权限不足")
        
        # 4. 执行命令
        return await spec.handler(spec.name, args, event, context)
```

#### SessionHandler

处理活跃会话。

```python
class SessionHandler(MessageHandler):
    """活跃会话处理"""
    
    async def handle(self, text: str, event: Dict, context) -> Optional[List]:
        # 检查是否有活跃会话
        session_manager = self.dispatcher.session_manager
        if not session_manager:
            return None
        
        session = await session_manager.get(
            context.current_user_id,
            context.current_group_id
        )
        
        if not session:
            return None
        
        # 获取会话插件模块
        plugin_name = session.plugin_name
        plugin = self.dispatcher.app.plugin_manager.get_plugin(plugin_name)
        
        if not plugin or not hasattr(plugin.module, "handle_session"):
            return None
        
        # 调用 handle_session
        return await plugin.module.handle_session(text, event, context, session)
```

#### SmalltalkHandler

处理闲聊消息。

```python
class SmalltalkHandler(MessageHandler):
    """闲聊处理"""
    
    async def handle(self, text: str, event: Dict, context) -> Optional[List]:
        # 检查 smalltalk_mode
        decision = self.dispatcher._make_decision(text, ...)
        if not decision.smalltalk_mode:
            return None
        
        # 获取 smalltalk_provider
        provider_name = context.config.get("plugins", {}).get("smalltalk_provider")
        if not provider_name:
            return None
        
        plugin = self.dispatcher.app.plugin_manager.get_plugin(provider_name)
        
        if not plugin or not hasattr(plugin.module, "handle_smalltalk"):
            return None
        
        # 调用 handle_smalltalk
        result = await plugin.module.handle_smalltalk(text, event, context)
        
        # 返回空列表表示不回复
        if not result:
            return None
        
        return result
```

#### 短路机制

```python
# Handler 链执行示例
async def _handle_event(self, event: Dict) -> List[Dict]:
    results = []
    
    for handler in self._handlers:
        result = await handler.handle(text, event, context)
        
        if result is not None:
            # 短路：不再执行后续 Handler
            return result
        
        # 继续下一个 Handler
    
    # 所有 Handler 都不处理
    return []
```

**短路机制的优势**：
1. **性能优化**：一旦找到合适的处理者，立即返回
2. **优先级明确**：命令优先于会话，会话优先于闲聊
3. **解耦合**：每个 Handler 独立，互不影响

### 决策判断逻辑

```python
@dataclass
class ProcessDecision:
    """处理决策"""
    should_process: bool    # 是否应该处理
    smalltalk_mode: bool    # 是否进入闲聊模式


def _make_decision(
    self,
    text: str,
    is_private: bool,
    has_bot_name: bool,
    has_prefix: bool,
    group_id: Optional[int],
    ...
) -> ProcessDecision:
    """
    返回处理决策
    
    特殊处理：
    - 当 smalltalk_provider 为 xiaoqing_chat 时，所有群聊消息都进入 SmalltalkHandler
    - random_reply_rate 配置失效
    """
    # xiaoqing_chat 特殊处理
    if self._get_smalltalk_provider() == "xiaoqing_chat":
        return ProcessDecision(True, True)
    
    # 私聊始终处理，可闲聊
    if is_private:
        return ProcessDecision(True, True)
    
    # 群聊检查
    is_muted = self.is_muted(group_id)
    
    # 有命令前缀 -> 处理（静音不影响命令）
    if has_prefix:
        return ProcessDecision(True, False)
    
    # 有 bot_name -> 处理
    if has_bot_name:
        return ProcessDecision(True, not is_muted)  # 静音时不闲聊
    
    # 静音 -> 不处理
    if is_muted:
        return ProcessDecision(False, False)
    
    # 随机回复
    random_reply_rate = self.config.get("random_reply_rate", 0.05)
    if random.random() < random_reply_rate:
        return ProcessDecision(True, True)
    
    return ProcessDecision(False, False)


def _get_smalltalk_provider(self) -> Optional[str]:
    """获取当前闲聊提供者"""
    return self.config.get("plugins", {}).get("smalltalk_provider")
```

### 前缀剥离

```python
def _strip_prefix(self, text: str, event: Dict, context: PluginContext) -> Optional[str]:
    """剥离前缀（@机器人、bot_name、命令前缀）"""
    
    # 1. 剥离 @机器人
    text = self._strip_at_mention(text, event)
    
    # 2. 剥离 bot_name
    text = self._strip_bot_name(text, context)
    
    # 3. 剥离命令前缀
    text = self._strip_command_prefix(text, context)
    
    return text


def _strip_at_mention(self, text: str, event: Dict) -> str:
    """剥离 @机器人"""
    # 从消息段中提取 @信息
    message = event.get("message", [])
    for segment in message:
        if segment.get("type") == "at":
            text = text.replace(f"[CQ:at,qq={segment['data']['qq']}] ", "")
    return text


def _strip_bot_name(self, text: str, context: PluginContext) -> str:
    """剥离 bot_name（支持模糊匹配）"""
    bot_name = context.config.get("bot_name", "")
    
    if not bot_name:
        return text
    
    # 检查是否以 bot_name 开头（忽略大小写）
    if text.lower().startswith(bot_name.lower()):
        # 移除 bot_name 和后面的标点
        remainder = text[len(bot_name):]
        return remainder.lstrip("，。！？、,.!? ")
    
    return text


def _strip_command_prefix(self, text: str, context: PluginContext) -> str:
    """剥离命令前缀"""
    prefixes = context.config.get("command_prefixes", ["/"])
    
    for prefix in prefixes:
        if text.startswith(prefix):
            return text[len(prefix):]
    
    return text
```

### 静音管理

```python
def mute_group(self, group_id: int, duration_minutes: float) -> None:
    """静音群聊"""
    unmute_time = time.time() + duration_minutes * 60
    self._muted_groups[group_id] = unmute_time


def unmute_group(self, group_id: int) -> None:
    """解除静音"""
    if group_id in self._muted_groups:
        del self._muted_groups[group_id]


def is_muted(self, group_id: Optional[int]) -> bool:
    """检查是否静音（自动清理过期）"""
    if not group_id:
        return False
    
    if group_id not in self._muted_groups:
        return False
    
    unmute_time = self._muted_groups[group_id]
    if time.time() >= unmute_time:
        # 已过期，自动解除
        del self._muted_groups[group_id]
        return False
    
    return True


def get_mute_remaining(self, group_id: int) -> Optional[float]:
    """获取剩余静音时间（秒）"""
    if group_id not in self._muted_groups:
        return 0
    
    unmute_time = self._muted_groups[group_id]
    remaining = unmute_time - time.time()
    
    if remaining <= 0:
        del self._muted_groups[group_id]
        return 0
    
    return remaining
```

**静音影响范围**：

| 消息类型 | 静音时是否处理 |
|----------|---------------|
| 命令（有前缀） | ✅ 处理 |
| @机器人 | ✅ 处理命令，❌ 不闲聊 |
| 活跃会话 | ✅ 处理（会话优先级最高） |
| 随机回复 | ❌ 不处理 |
| 闲聊 | ❌ 不处理 |

---

## router.py - 命令路由

### 数据结构

```python
@dataclass
class CommandSpec:
    plugin: str         # 插件名
    name: str           # 命令名
    triggers: List[str] # 触发词列表
    help_text: str      # 帮助文本
    admin_only: bool    # 是否管理员专用
    handler: Handler    # 处理函数
    priority: int = 0   # 优先级
```

### 路由逻辑

```python
class CommandRouter:
    def __init__(self):
        self._commands: List[CommandSpec] = []
        self._sorted = False
    
    def register(self, spec: CommandSpec):
        """注册命令"""
        self._commands.append(spec)
        self._sorted = False
    
    def resolve(self, text: str) -> Optional[Tuple[CommandSpec, str]]:
        """解析命令"""
        # 按优先级和触发词长度排序
        if not self._sorted:
            self._commands.sort(
                key=lambda x: (x.priority, max(len(t) for t in x.triggers)),
                reverse=True
            )
            self._sorted = True
        
        # 匹配触发词
        for spec in self._commands:
            for trigger in spec.triggers:
                if text.startswith(trigger):
                    args = text[len(trigger):].strip()
                    return spec, args
        
        return None
    
    def clear_plugin(self, plugin_name: str):
        """清除某插件的所有命令"""
        self._commands = [c for c in self._commands if c.plugin != plugin_name]
```

---

## plugin_manager.py - 插件管理

### 数据结构

```python
@dataclass
class PluginDefinition:
    name: str
    version: str
    entry: str
    commands: List[Dict]
    schedule: List[Dict]
    concurrency: str
    enabled: bool = True

@dataclass
class LoadedPlugin:
    definition: PluginDefinition
    module: ModuleType
    mtime: float
```

### 加载流程

```python
class PluginManager:
    def load_plugin(self, plugin_dir: Path):
        # 1. 读取 plugin.json
        definition = self._load_definition(plugin_dir)
        if not definition or not definition.enabled:
            return
        
        # 2. 动态导入模块
        module = self._load_module(plugin_dir, definition)
        
        # 3. 注册命令
        self._register_commands(definition, module)
        
        # 4. 调用 init() 钩子
        if hasattr(module, "init"):
            result = module.init()
            if asyncio.iscoroutine(result):
                asyncio.create_task(result)
        
        # 5. 保存到字典
        self._plugins[definition.name] = LoadedPlugin(...)
    
    def _load_module(self, plugin_dir: Path, definition) -> ModuleType:
        """动态导入 Python 模块"""
        # 1. 确保父级路径在 sys.path 中
        if str(self.plugins_dir) not in sys.path:
            sys.path.insert(0, str(self.plugins_dir))
            
        # 2. 构造包名 (直接使用目录名作为包名)
        # 例如: plugins/myplugin -> myplugin.main
        entry_stem = Path(definition.entry).stem
        module_name = f"{plugin_dir.name}.{entry_stem}"
        
        # 3. 清理旧模块（支持热重载）
        if module_name in sys.modules:
            del sys.modules[module_name]
            
        # 4. 标准导入
        return importlib.import_module(module_name)
    
    async def reload_plugin(self, name: str):
        """热重载插件"""
        await self.unload_plugin(name)
        self.load_plugin(self.plugins_dir / name)
```

### 热重载监控

```python
async def watch(self):
    """监控文件变化"""
    while True:
        await asyncio.sleep(self._poll_interval)
        
        for plugin_dir in self.plugins_dir.iterdir():
            if not self._is_plugin_dir(plugin_dir):
                continue
            
            definition = self._load_definition(plugin_dir)
            mtime = self._get_mtime(plugin_dir, definition)
            existing = self._plugins.get(definition.name)
            
            if not existing:
                # 新插件
                self.load_plugin(plugin_dir)
            elif mtime != existing.mtime:
                # 文件变化，重载
                await self.reload_plugin(definition.name)
```

---

## context.py - 插件上下文

### 完整结构

```python
@dataclass
class PluginContext:
    # 配置
    config: Dict[str, Any]
    secrets: Dict[str, Any]
    
    # 路径
    plugin_name: str
    plugin_dir: Path
    data_dir: Path
    
    # 工具
    logger: logging.Logger
    http_session: aiohttp.ClientSession
    send_action: SendAction
    
    # 回调
    reload_config: Callable
    reload_plugins: Callable
    list_commands: Callable
    list_plugins: Callable
    
    # 运行时
    session_manager: Optional[SessionManager] = None
    current_user_id: Optional[int] = None
    current_group_id: Optional[int] = None
    dispatcher: Optional[Dispatcher] = None
```

### 会话便捷方法

```python
async def create_session(self, initial_data=None, timeout=300.0) -> Session:
    """创建会话"""
    if not self.session_manager or self.current_user_id is None:
        raise RuntimeError("...")
    
    return await self.session_manager.create(
        user_id=self.current_user_id,
        group_id=self.current_group_id,
        plugin_name=self.plugin_name,
        initial_data=initial_data,
        timeout=timeout,
    )

async def end_session(self) -> bool:
    """结束会话"""
    return await self.session_manager.delete(
        self.current_user_id, self.current_group_id
    )
```

---

## session.py - 会话管理

### Session 类

```python
@dataclass
class Session:
    user_id: int
    group_id: Optional[int]
    plugin_name: str
    state: str = "active"
    data: Dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    timeout: float = 300.0
    
    def get(self, key: str, default=None):
        return self.data.get(key, default)
    
    def set(self, key: str, value):
        self.data[key] = value
        self.updated_at = time.time()
    
    def is_expired(self) -> bool:
        return time.time() - self.updated_at > self.timeout
```

### SessionManager 类

```python
class SessionManager:
    def __init__(self, default_timeout=300.0):
        self._sessions: Dict[tuple, Session] = {}
        self._lock = asyncio.Lock()
    
    def _make_key(self, user_id, group_id):
        return (user_id, group_id)
    
    async def create(self, user_id, group_id, plugin_name, initial_data, timeout):
        async with self._lock:
            key = self._make_key(user_id, group_id)
            session = Session(
                user_id=user_id,
                group_id=group_id,
                plugin_name=plugin_name,
                data=initial_data or {},
                timeout=timeout,
            )
            self._sessions[key] = session
            return session
    
    async def get(self, user_id, group_id) -> Optional[Session]:
        async with self._lock:
            key = self._make_key(user_id, group_id)
            session = self._sessions.get(key)
            
            if session and session.is_expired():
                del self._sessions[key]
                return None
            
            return session
```

---

## plugin_base.py - 插件工具

### 消息段构建

```python
def text(content: str) -> Dict:
    return {"type": "text", "data": {"text": content}}

def image(file_path: str) -> Dict:
    return {"type": "image", "data": {"file": f"file:///{file_path}"}}

def image_url(url: str) -> Dict:
    return {"type": "image", "data": {"file": url}}

def segments(payload) -> List[Dict]:
    """统一转换为消息段列表"""
    if payload is None:
        return []
    if isinstance(payload, list):
        return payload
    if isinstance(payload, str):
        return [text(payload)]
    return []
```

### 异步工具

```python
async def run_sync(func: Callable, *args, **kwargs):
    """在线程池中运行同步函数"""
    return await asyncio.to_thread(func, *args, **kwargs)
```

### 文件工具

```python
def ensure_dir(path: Path):
    """确保目录存在"""
    path.mkdir(parents=True, exist_ok=True)

def load_json(path: Path, default=None) -> Dict:
    """加载 JSON"""
    if not path.exists():
        return default or {}
    return json.loads(path.read_text(encoding="utf-8"))

def write_json(path: Path, data: Dict):
    """写入 JSON"""
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
```

---

## onebot.py - OneBot 通信

### HTTP 发送器

```python
class OneBotHttpSender:
    def __init__(self, http_base: str, auth_token: str, session):
        self.http_base = http_base.rstrip("/")
        self.auth_token = auth_token
        self.session = session
    
    async def send_action(self, action: Dict):
        """发送 OneBot Action"""
        url = f"{self.http_base}/{action['action']}"
        headers = {"Authorization": f"Bearer {self.auth_token}"}
        
        await self.session.post(url, json=action['params'], headers=headers)
```

### WebSocket 客户端

```python
class OneBotWsClient:
    async def connect_and_listen(self, handler):
        """连接并监听消息"""
        while self._running:
            try:
                async with websockets.connect(self.ws_uri) as ws:
                    self._ws = ws
                    async for message in ws:
                        event = json.loads(message)
                        await handler(event)
            except Exception:
                self._ws = None
                await asyncio.sleep(5)  # 重连
```

---

## server.py - Inbound 服务器

```python
class InboundServer:
    def __init__(self, host, port, ws_path, token, handler):
        self.app = web.Application()
        self.app.add_routes([
            web.get("/health", self.health),
            web.post("/event", self.post_event),
            web.get(ws_path, self.ws_handler),
        ])
    
    async def post_event(self, request):
        """处理 POST 事件"""
        if not self._authorized(request):
            return web.json_response({"status": "unauthorized"}, status=401)
        
        payload = await request.json()
        actions = await self.handler(payload)
        return web.json_response({"actions": actions})
    
    def _authorized(self, request) -> bool:
        """Token 验证"""
        if not self.token:
            return True
        auth = request.headers.get("Authorization", "")
        expected = f"Bearer {self.token}"
        return hmac.compare_digest(auth.encode(), expected.encode())
```



---

## 下一步

- API 完整参考 → [05-api-reference.md](05-api-reference.md)
- 配置详解 → [06-configuration.md](06-configuration.md)
