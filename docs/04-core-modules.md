# ⚙️ 04 - 核心模块详解

本章把 XiaoQing `core/` 里的主要模块逐个拆开说明。

> [!NOTE]
> 本章面向框架开发者。只做插件开发时，优先阅读 [03-plugin-development.md](03-plugin-development.md) 和 [05-api-reference.md](05-api-reference.md)。

---

## 📋 模块概览

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
| 参数解析 | `args.py` | 命令参数结构化解析（`ParsedArgs`） |
| 运行指标 | `metrics.py` | 插件执行统计（`MetricsCollector`） |
| 接口定义 | `interfaces.py` | Protocol 接口定义，降低耦合 |
| 异常定义 | `exceptions.py` | 自定义异常类 |
| 数据模型 | `models.py` | 通用数据模型 |
| 时间工具 | `clock.py` | 时区感知的时间工具 |
| 全局常量 | `constants.py` | 全局常量定义 |
| 日志配置 | `logging_config.py` | 日志系统 |

核心模块只负责所有插件共享的基础设施。像 `pendo` 的 SQLite 数据模型、Web Transfer，或 `xiaoqing_chat` 的 attention gate、PFC planner、主 LLM 和媒体 marker 解析，都放在插件目录内维护。这样做可以保持 core 稳定，也让大型插件能够在不污染框架层的前提下演进自己的业务架构。

从一次消息处理看，核心模块按以下关系协作。

```text
InboundServer / OneBotWsClient
  -> XiaoQingApp
  -> Dispatcher
  -> Handler chain
  -> CommandRouter / SessionManager / Smalltalk provider
  -> PluginManager 提供插件模块和 PluginContext
  -> plugin.handle() 或 plugin.handle_smalltalk()
  -> OneBotHttpSender / WebSocket 发送消息段
```

---

## 🏠 app.py 应用主类

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
    self.config_watch_task = asyncio.create_task(self.config_manager.watch())
    if self.config.get("enable_plugin_watcher", False):
        self.plugin_watch_task = asyncio.create_task(self.plugin_manager.watch())
    
    # 5. 启动 WS 客户端（可选）
    if self.config.get("enable_ws_client"):
        self.ws_client = OneBotWsClient(...)
        self.ws_task = asyncio.create_task(self.ws_client.connect_and_listen(...))
    
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
    if self.ws_task:
        self.ws_task.cancel()
    
    # 2. 停止定时任务
    self.scheduler.scheduler.shutdown(wait=True)
    if self.plugin_watch_task:
        self.plugin_watch_task.cancel()
    if self.config_watch_task:
        self.config_watch_task.cancel()
    
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

## 🔀 dispatcher.py 消息分发器

### 核心逻辑

Dispatcher 负责把已经验证的 OneBot 消息事件解析成 `MessageContext`，再按照固定的 A-G 线性流程处理。处理状态保存在 `MessageContext` 与局部控制流中。

```python
class Dispatcher:
    def __init__(self, router, app, build_context, semaphore, session_manager):
        self.router = router
        self.app = app
        self.build_context = build_context
        self.semaphore = semaphore
        self.session_manager = session_manager
        self._muted_groups: Dict[int, float] = {}  # 静音管理
    
    async def handle_event(self, event: Dict) -> List[Dict]:
        """处理事件（带并发控制）"""
        async with self.semaphore:
            return await self._process_event(event)
    
    async def _process_event(self, event: Dict) -> List[Dict]:
        # Step 0: 解析消息
        ctx = self.parser.parse(event)
        if ctx is None:
            return []

        await self._observe_message(ctx)

        # Step A: clean_text 是单个 URL 时直接交给 url_parser
        if ctx.is_url_only:
            return await self._invoke_url_parser(ctx, ctx.clean_text.strip()) or []

        # Step B: 处理门控
        should_process = (
            ctx.is_private
            or not self.config_provider.config.get("require_bot_name_in_group", True)
            or ctx.has_prefix
            or await self._has_active_session(ctx)
        )
        if not should_process:
            return []

        # Step C-G: 只喊名字、命令、未知命令、会话、闲聊回落
        ...
```

### 线性处理流程

```
Step A: URL short-circuit (clean_text 单 URL → url_parser；mute 不影响)
Step B: 处理门控
        - 私聊：处理
        - require_bot_name_in_group=False：处理
        - has_prefix（/ 开头 OR bot_name OR @me）：处理
        - 有活跃 session 且非 is_only_bot_name：处理
        - 否则：丢弃
Step C: is_only_bot_name → 默认回应 / call_bot_name_only
Step D: router 命中 → 执行命令
Step E: has_command_prefix 且命令未命中 → 未知命令提示
Step F: 活跃 session → 转 session 插件
Step G: 回落 smalltalk provider（mute 仅在此步阻塞）
```

### 解析信号

- `has_prefix` 表示消息以命令前缀（默认 `/`）开头，或包含 `bot_name`（任意位置），或包含 @机器人（任意位置）。
- `has_command_prefix` 单独标识严格以命令前缀开头。
- URL 处理改用 `ctx.is_url_only`：仅当 `clean_text` strip 后整体匹配 `^https?://\S+$` 时调度到 `url_parser`。

### 前缀剥离

```python
parsed = parse_text_command_context(
    text,
    event,
    bot_name=bot_name,
    prefixes=prefixes,
    self_id=self_id,
    bot_name_pattern=self._bot_name_pattern,
    message_scan=message_scan,
)
```

`parse_text_command_context()` 统一产出 `clean_text`、`has_bot_name`、`has_command_prefix`、`has_prefix`、`is_only_bot_name`、`is_at_me` 和 `is_url_only`。前缀剥离只移除开头的 @、开头的 bot_name 及随后的命令前缀；`has_prefix` 的检测范围更宽，bot_name 或 @me 在任意位置都会让消息被视为指向机器人。

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
| 单 URL | ✅ 处理，仍进入 `url_parser` |
| @机器人 / bot_name | ✅ 通过门控，最终 smalltalk 回落会被静音阻塞 |
| 活跃会话 | ✅ 处理 |
| 闲聊回落 | ❌ 不处理 |

---

## 🗺️ router.py 命令路由

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

## 📦 plugin_manager.py 插件管理

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
                self._track_init_task(result, definition.name)
        
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
        await self.wait_inits()
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

默认不会自动启动该 watcher；需要在 `config.json` 中将 `enable_plugin_watcher` 设为 `true`。

---

## 🔧 context.py 插件上下文

### 完整结构

```python
@dataclass
class PluginContext:
    # 配置
    config: Dict[str, Any]          # config.json 完整内容
    secrets: Dict[str, Any]         # secrets.json 完整内容

    # 路径
    plugin_name: str
    plugin_dir: Path
    data_dir: Path

    # 工具
    logger: _RequestLogger          # 自动附带 request_id 的日志记录器
    http_session: aiohttp.ClientSession | None
    send_action: SendAction         # 发送 OneBot Action 的回调
    metrics: MetricsCollector | None  # 运行指标收集器

    # 回调
    reload_config: Callable
    reload_plugins: Callable
    list_commands: Callable
    list_plugins: Callable

    # 运行时（由 Dispatcher 注入）
    session_manager: SessionManager | None = None
    current_user_id: int | None = None
    current_group_id: int | None = None
    mute_control: MuteControl | None = None
    config_manager: ConfigManagerLike | None = None
    request_id: str | None = None

    # 插件私有状态（当次请求生命周期）
    state: Dict[str, Any] = field(default_factory=dict)
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

### 主动发送回调

`send_action` 是插件上下文中的异步回调，底层会进入 `XiaoQingApp._send_action()` 的统一发送链路。普通命令应优先返回消息段；后台任务、定时任务或插件内独立队列需要在稍后回发结果时，再直接调用它。

```python
from core.plugin_base import build_action, segments

action = build_action(segments("任务完成"), user_id, group_id)
if action:
    await context.send_action(action)
```

`codex` 插件使用这种方式在 CLI 任务完成后主动发送 `[codex:<label> #<job_id>]` 文字和图片结果；发送链路会继续负责纯文本长消息分割、WS/HTTP 回退和错误日志，Codex 插件会在混合图片消息前先拆分过长文本。

---

## 💬 session.py 会话管理

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

## 🛠️ plugin_base.py 插件工具

### 消息段构建

```python
from pathlib import Path

def text(content: str) -> Dict:
    return {"type": "text", "data": {"text": content}}

def image(file_path: str) -> Dict:
    return {"type": "image", "data": {"file": Path(file_path).resolve().as_uri()}}

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

> 说明：框架提供的 `image()` / `record()` 会负责把本地路径转成标准 `file://` URI；手写消息段时也应优先使用 `Path(...).resolve().as_uri()`，不要自己拼接 `file:///` 字符串。

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
    """加载 JSON（文件不存在时返回 default）"""
    if not path.exists():
        return default or {}
    return json.loads(path.read_text(encoding="utf-8"))

def write_json(path: Path, data: Dict):
    """写入 JSON（先写临时文件再原子替换）"""
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2))

def atomic_write_text(path: Path, payload: str) -> None:
    """原子写入文本文件（避免写入中断导致文件损坏）"""
    ...
```

### 长消息分割

```python
def split_message_segments(
    segs: Segments,
    max_length: int = 500,
) -> list[Segments]:
    """
    将消息段列表按文本长度分割，用于防止超长消息被截断。
    每个分片的文本总长度不超过 max_length。
    """
    ...
```

---

## 🔗 onebot.py OneBot 通信

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

## 🖥️ server.py Inbound 服务器

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

## 🔍 args.py 命令参数解析

提供 `ParsedArgs` 类，用于将命令参数字符串结构化解析：

```python
from core.args import parse

parsed = parse("add 完成报告 --cat=工作 -p 2")

parsed.first          # "add"（第一个位置参数）
parsed.second         # "完成报告"（第二个位置参数）
parsed.get(2)         # ""（第三个位置参数，不存在返回空）
parsed.rest(1)        # "完成报告"（从第 1 个参数开始拼接）
parsed.opt("cat")     # "工作"（长选项值）
parsed.opt("p")       # "2"（短选项值）
parsed.has("dry-run") # False（检查 flag 是否存在）
len(parsed)           # 2（位置参数数量）
bool(parsed)          # True（参数字符串非空时为 True）
```

**支持格式**：

| 格式 | 示例 | 说明 |
|------|------|------|
| 位置参数 | `arg1 arg2` | `parsed.get(0)`, `parsed.first` |
| 长选项有值 | `--key=value` 或 `--key value` | `parsed.opt("key")` |
| 长选项标志 | `--flag` | `parsed.opt("flag") == "true"` |
| 短选项有值 | `-k value` | `parsed.opt("k")` |
| 引号包裹 | `"hello world"` | 视为单个 token |

---

## 📊 metrics.py 运行指标

`MetricsCollector` 收集插件执行统计，通过 `/metrics` 命令查看：

```python
# 插件可通过 context.metrics 访问
if context.metrics:
    stats = await context.metrics.get_summary()
    # {
    #   "total_requests": 1234,
    #   "uptime": 3600.0,
    #   "slow_plugins": [...],
    #   "error_rate": 0.02,
    # }
```

通常不需要手动调用，框架在每次命令执行后自动记录。`timed_async` 装饰器可用于自定义计时：

```python
from core.metrics import timed_async, get_metrics_collector

@timed_async(get_metrics_collector(), "myplugin", "my_command")
async def my_command_handler(...):
    ...
```

---

## ➡️ 下一步

- API 参考见 [05-api-reference.md](05-api-reference.md)
- 配置详解见 [06-configuration.md](06-configuration.md)
