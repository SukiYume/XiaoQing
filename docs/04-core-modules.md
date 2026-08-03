# ⚙️ 04 - 核心模块详解

本章把 XiaoQing `core/` 里的主要模块逐个拆开说明。

> [!NOTE]
> 本章面向框架开发者。只做插件开发时，优先阅读 [03-plugin-development.md](03-plugin-development.md) 和 [05-api-reference.md](05-api-reference.md)。

---

## 📋 模块概览

| 模块 | 文件 | 职责 |
|------|------|------|
| 应用主类 | `app.py`、`app_support.py` | 生命周期、组件编排和共享身份记录 |
| 应用控制面 | `app_delivery.py`、`app_ingress.py`、`app_plugin_watch.py`、`app_scheduling.py` | 投递、端点重协商、watcher 监督和排程 |
| 消息分发 | `dispatcher.py` | 解析消息，路由到插件 |
| 命令路由 | `router.py` | 匹配触发词 |
| 插件管理 | `plugin_manager.py`、`plugin_manager_support.py` | 管理器装配、导入屏障和共享事务记录 |
| 插件生命周期 | `plugin_generation.py`、`plugin_watcher.py`、`plugin_runtime.py`、`plugin_data.py` | 代际发布、扫描、执行 gate、服务与数据目录 |
| 插件上下文 | `context.py` | 插件运行环境 |
| AI 路由 | `ai.py` | 统一模型注册表、重试、fallback 和有界请求 |
| 能力实现 | `capabilities.py` | 向插件暴露受限服务，不下发全局凭据 |
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
| 数据模型 | `models.py` | 通用数据模型与 Manifest 校验（`OneBotEvent`、`PluginManifest`） |
| 时间工具 | `clock.py` | 时区感知的时间工具 |
| 全局常量 | `constants.py` | 全局常量定义 |
| 日志配置 | `logging_config.py` | 日志系统 |
| 版本解析 | `version.py` | 从 `pyproject.toml`/wheel 元数据解析运行时 `VERSION` |
| 执行门控 | `plugin_execution.py` | 每插件 bulkhead、有界队列、同步 broker 与公平调度 |
| 投递回执 | `delivery.py` | commit-after-ack 的进程内投递回执与 handoff |
| 出站安全 | `safe_http.py` | 面向不可信 URL 的 fail-closed HTTP 客户端与 SSRF 防护 |
| 有界传输 | `bounded_http.py` | 有界响应体读取与结构化解析 |
| 外部图片校验 | `image_validation.py` | 容器、逐帧解码、资源预算及本地文件身份复核 |
| 原子存储 | `atomic_store.py` | core 与插件共享的崩溃安全本地持久化原语 |
| 有界文件缓存 | `bounded_file_cache.py` | 带 TTL/LRU/条目与字节上限的崩溃安全磁盘缓存 |
| 持久扇出 | `durable_fanout.py` | 定时通知按目标记录的崩溃安全进度 |
| 键控锁 | `async_keyed_lock.py` | 有界、引用计数的 asyncio 按键锁池 |
| 鉴权 | `auth.py` | Bearer token 的常量时间校验等鉴权工具 |
| 入站策略 | `inbound_policy.py` | 明文 Inbound HTTP/WS listener 的绑定安全策略（loopback / 可信 TLS proxy） |
| 生命周期 | `lifecycle.py` | 核心生命周期任务的取消与致命异常处理 |
| 公共错误 | `public_errors.py` | 面向公开入口的脱敏、可关联错误响应 |
| 敏感审计 | `sensitive_audit.py` | 敏感日志元数据的重启期指纹 |

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

`XiaoQingApp` 是稳定门面；投递、ingress、插件 watcher 与排程实现分别由四个
`app_*` 职责模块提供。共享状态仍只在 `XiaoQingApp.__init__` 初始化一次，拆分模块不各自
创建客户端、锁或缓存，因此没有双写或兼容分支。

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
        # 最终 bind 前再次执行安全策略：仅 loopback，或显式确认受可信 TLS proxy 保护
        # TCPSite 本身固定为明文；https/wss listener 配置会 fail closed
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
    def __init__(
        self,
        router,               # CommandRouter
        config_provider,      # ConfigProvider（提供 config 快照）
        plugin_registry,      # PluginRegistry
        admin_check,          # AdminCheck（is_admin / issue_user_principal）
        build_context,        # ContextFactory
        semaphore,            # AdjustableSemaphore | asyncio.Semaphore | None
        session_manager=None,
        metrics=None,
        clock=None,
        random_gen=None,
        parser=None,
    ) -> None:
        ...
        self._muted_groups: Dict[int, float] = {}  # 静音管理

    async def handle_event(self, event: Dict) -> List[Dict]:
        """处理事件（先丢弃非 message 事件，再带并发控制处理）"""
        if event.get("post_type") != "message":
            return []
        event_data = self._validate_event(event)   # OneBotEvent 校验
        if event_data is None:
            return []
        if self.semaphore:
            async with self.semaphore:
                return await self._process_event(event_data)
        return await self._process_event(event_data)
```

### 线性处理流程

命令解析发生在观察之前，保证命令正文永远不会进入 Xiaoqing Chat 记忆；URL 解析被放在处理门控与静音门控**之后**，避免 URL 消息绕过授权创建网络路径。

```
Step 0: 解析事件 → MessageContext（空消息只在存在活跃会话时保留）
Step A: 处理门控 should_process =
          私聊
          OR require_bot_name_in_group=False
          OR has_prefix（/ 开头 OR bot_name OR @me）
          OR 存在活跃 session（且非 is_only_bot_name）
        · 先解析命令（router.resolve），再按敏感类别决定是否观察消息
        · 未通过门控：单 URL 丢弃；允许的普通群闲聊交 smalltalk（静音则跳过）；否则返回
Step B: is_url_only → url_parser（在门控与静音之后；静音时跳过，SSRF 目标被拦截）
Step C: is_only_bot_name → 默认回应 / call_bot_name_only
Step D: 活跃 session → 转 session 插件（会话输入可与全局命令重名，优先消费）
Step E: router 命中 → 执行命令（含 permission/contexts 调用前校验）
Step F: has_command_prefix 且命令未命中且首字母为字母 → 未知命令提示
Step G: 回落 smalltalk provider（mute 仅在此步及普通群闲聊阻塞）
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


def get_mute_remaining(self, group_id: int) -> float:
    """获取剩余静音时间（分钟）"""
    if group_id not in self._muted_groups:
        return 0
    
    unmute_time = self._muted_groups[group_id]
    remaining = unmute_time - time.time()
    
    if remaining <= 0:
        del self._muted_groups[group_id]
        return 0
    
    return remaining / 60.0
```

**静音影响范围**：

| 消息类型 | 静音时是否处理 |
|----------|---------------|
| 命令（有前缀） | ✅ 处理 |
| 单 URL | ❌ 静音时跳过 `url_parser`（URL 解析在门控与静音之后） |
| @机器人 / bot_name | ✅ 通过门控，最终 smalltalk 回落会被静音阻塞 |
| 活跃会话 | ✅ 处理 |
| 闲聊回落 | ❌ 不处理 |

---

## 🗺️ router.py 命令路由

### 数据结构

```python
@dataclass(frozen=True, slots=True)
class CommandCatalogNode:
    code: str                         # 稳定码：plugin.root.child
    plugin: str
    path: tuple[str, ...]             # 用户输入路径中的规范名
    name: str
    aliases: tuple[str, ...]
    help_text: str
    usage: str
    match_mode: str
    permission: str
    contexts: tuple[str, ...]
    examples: tuple[str, ...]
    invalid_examples: tuple[str, ...]
    children: tuple["CommandCatalogNode", ...]

@dataclass
class CommandSpec:
    plugin: str         # 插件名
    name: str           # 命令名
    triggers: List[str] # 触发词列表
    help_text: str      # 帮助文本
    admin_only: bool    # 是否管理员专用
    handler: Handler    # 处理函数
    priority: int = 0   # 优先级
    usage: str | None = None
    catalog: CommandCatalogNode | None = None

@dataclass(frozen=True, slots=True)
class CommandInvocation:
    root: CommandCatalogNode
    chain: tuple[CommandCatalogNode, ...]
    remainders: tuple[str, ...]
```

### 路由逻辑

`CommandRouter.resolve()` 先用不可变索引按优先级、触发词长度匹配顶层入口；命中后，
`resolve_catalog_invocation()` 再按规范名或别名最长匹配递归子树。每一级均保留消费后的
余串，因此插件既能取得规范子命令，也不会丢失业务参数中的空格和换行。

PluginManager 在候选插件代完全校验成功后，一次性用 `replace_plugin()` 发布
`CommandSpec` 与目录树；帮助查询不会看到新旧两代混合状态。`get_command_catalog()` 返回
按插件名和稳定码排序的根节点元组。节点为冻结 dataclass，`walk()` 可展开子树，
`to_dict()` 可生成不含处理器的 JSON 公共视图。

Dispatcher 把解析结果写入 `PluginContext.command_invocation`，并以最深命中节点的
`permission` 和 `contexts` 做调用前校验。子节点权限只能比父节点相同或更严格，场景只能
收窄。`/help` 的文本分页、精确查询、搜索和 JSON 导出都直接读取这份快照。

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
    authorized_entry: Path
```

### 加载流程

```python
class PluginManager:
    def load_plugin(self, plugin_dir: Path):
        # 1. 核验真实插件根目录并稳定读取普通文件 plugin.json
        definition = self._load_definition(plugin_dir)
        if not definition or not definition.enabled:
            return
        
        # 2. 解析并固定 manifest 授权的真实入口文件
        authorized_entry = resolve_plugin_entry(
            self.plugins_dir, plugin_dir, definition.entry
        )

        # 3. 在隔离事务中从源码导入模块
        module = self._load_module(plugin_dir, definition)
        
        # 4. 构造并原子发布命令规范
        command_specs = self._build_command_specs(definition, module)
        self.router.replace_plugin(definition.name, command_specs)
        
        # 5. 调用 init() 钩子
        if hasattr(module, "init"):
            result = module.init()
            if asyncio.iscoroutine(result):
                self._track_init_task(result, definition.name)
        
        # 6. 再次核验 Manifest、指纹、模块来源和入口身份后原子发布
        self._register_loaded_plugin(
            definition,
            module,
            mtime,
            authorized_entry=authorized_entry,
        )
    
    async def reload_plugin(self, name: str):
        """热重载插件"""
        await self.unload_plugin(name)
        self.load_plugin(self.plugins_dir / name)
        await self.wait_inits()
```

### 热重载监控

`PluginManager.watch()` 每轮在后台线程取得插件目录快照，再在 lifecycle lock 下逐路径收敛运行态。根目录快照不完整时不会据此推断插件已删除；单个目录、Manifest、依赖检查或文件指纹发生可恢复 I/O 竞态时，只跳过受影响路径并在下一轮重试。日志使用固定桶限流，避免持续损坏文件每轮刷屏。

源码指纹覆盖插件内全部 `.py`、插件根目录的 `.json`、`plugin.json` 和 Manifest `watch_files` 显式列出的嵌套普通文件；运行数据位于源码树外，遍历只剪枝 `__pycache__/`。每个文件核对 open/fstat/read/fstat/stat 身份，完整读取后再复核全体路径和身份；删除、原子替换或多文件部署形成的混合快照不会触发发布。扫描同时限制单目录条目、全树条目、目录数、文件数、路径深度/字节和单文件/总字节；超限或第二遍不稳定只影响当前插件。Manifest 授权未改变时，暂时无法证明稳定快照会保留旧代；授权已经改变却无法验证候选时则 fail closed。

Manifest 的 `entry` 会先经过跨平台的规范 POSIX 相对路径校验，再解析为插件真实根目录内的普通文件。插件根、Manifest、入口、包目录和源码文件不允许经过 symlink、junction 或 reparse point；模块的 `__spec__.origin` 与 `__file__` 必须同时、逐身份匹配被授权入口。运行时在初始化、回调和延迟相对导入期间保留插件专属 source-only finder，直接读取稳定源码快照并跳过 `.pyc`，因此 `__pycache__` 既不参与指纹，也不会成为实际执行来源。

规范模块名使用 `plugins.<name>...`，而 `sys.modules` 是进程全局状态。导入事务会拒绝复用在事务前已经存在且不属于当前插件根的同名模块；没有该插件生命周期所有权的另一个 `PluginManager` 也不能卸载或清除这些模块。入口或来源在代码执行后发生漂移时，框架不会发布该代，并把无法证明可安全清理的部分代标记为需要进程重启。

卸载和回滚的热重载路径使用 CPython 的私有 module lock，并在原子恢复期间把
`ModuleSpec._initializing` 设为真，阻止其他线程从 `sys.modules` fast path 看到半提交
父子图。框架不再按 Python 小版本放行：启动时会发布一个合成 initializing module，
由另一线程真实导入，确认它在锁释放前被阻塞、释放后能完成。探针失败时管理器仍可
启动并完成首次加载，但 watcher 与手动热重载都关闭并要求重启生效。旧代恢复必须先
还原其精确冻结模块图，才能支持普通相对导入和 `importlib`；恢复初始化期间命令、服务
和 manager-visible gate 仍关闭，重新授权成功后才发布。插件代码本身是受信任扩展而非
沙箱，因此模块可见性不被描述为安全隔离边界。

默认不会自动启动该 watcher；需要在 `config.json` 中将 `enable_plugin_watcher` 设为
`true`，且模块导入屏障行为探针必须通过。若探针失败，日志会给出原因，运行模式为
restart-only。`plugin_poll_interval` 热更新会唤醒当前 sleep 而不取消正在进行的
reconcile。应用监督 watcher 的正常意外返回、普通异常和致命异常：始终只保留一个
重启所有者，并用有上限的指数退避避免故障热循环；disable 与停机则取消 watcher 和
待执行重启。

### 执行 gate 与同步 bulkhead

每个已加载插件拥有独立 `PluginExecutionGate`。入口先受 `parallel_limit` 和 `admission_queue_limit` 约束；同步 callback 及 `plugin_base.run_sync()` 再进入该插件的 `sync_parallel_limit`/`sync_queue_limit`。`PluginManager` 持有一个四 worker 的 `PluginSyncBroker`，按插件轮转提交，且用 `global_sync_queue_limit` 限制进程级等待量。因此插件 A 的慢同步调用不会占满四个 worker 阻塞插件 B；任一队列满都会快速拒绝并留下可观察日志，而不是无界增长。

取消只能阻止尚未启动的同步调用；Python 不能安全地强杀已经运行的线程。关闭插件时 gate 先原子停止接纳，再取消排队项并跟踪真实 future 直到 `drain_timeout_seconds`。超时的旧代保持关闭并进入 quarantine，代码、状态和 broker 引用均不会提前释放，也不会同时装入新代。应用停机还会在同一全局期限内关闭 broker；只有所有真实同步工作结束后，线程池才算完整回收。

---

## 🤖 ai.py 统一模型路由

`core.ai` 把原先分散在插件中的 OpenAI-compatible 请求合并成一条受控路径。

- `config.ai.providers` 保存公开连接信息。
- `config.ai.models` 保存可复用模型 profile 和模态。
- `config.plugins.<plugin>.ai.routes` 保存插件自己的有序模型链和调用预算。
- `secrets.ai.providers` 只保存同名 provider 的 API Key。

`XiaoQingApp._build_plugin_capabilities()` 为每个插件构造绑定插件名的 `AIService`。调用方只提供 route 名、messages 和任务级参数，无法把插件名改成别的命名空间，也拿不到 provider 密钥。每次 `complete()` 或 `list_models()` 读取一份新的 `ConfigSnapshot`，保证热重载后的新请求及时生效，同时避免单次调用混用不同配置代。

route 中 `models` 的第一个 profile 是主模型。core 先在同一模型内做有界重试，再只对配置允许的网络、超时、限流、服务端错误、模型不可用或响应异常切换到下一个 profile；认证失败和无效请求直接返回。整条链还受 `total_timeout_seconds` 约束。管理员显式传入 `pinned_model` 时只调用该 profile，不执行跨模型 fallback。

模型响应使用 `AICompletionResult` 返回：`content` 是常见文本内容，`response` 保留工具调用所需的原始 JSON，`profile/provider/model/finish_reason/attempts` 是不含凭据的诊断元数据。

---

## 🔧 context.py 插件上下文

### 完整结构

```python
@dataclass
class PluginContext:
    # 配置
    config: Mapping[str, Any]       # 公开配置 + 当前插件命名空间，只读
    secrets: Mapping[str, Any]      # 仅当前插件秘密命名空间，只读

    # 路径
    plugin_name: str
    plugin_dir: Path
    data_dir: Path

    # 工具
    logger: _RequestLogger          # 自动附带 request_id 的日志记录器
    http_session: aiohttp.ClientSession | None
    capabilities: PluginCapabilities  # 绑定插件与当前身份的窄能力集合
    send_action: SendAction         # 发送 OneBot Action 的回调
    metrics: MetricsCollector | None  # 运行指标收集器

    # 回调
    reload_config: Callable
    reload_plugins: Callable
    get_command_catalog: Callable[[], tuple[CommandCatalogNode, ...]]
    list_plugins: Callable

    # 运行时（由 Dispatcher 注入）
    session_manager: SessionManager | None = None
    current_user_id: int | None = None
    current_group_id: int | None = None
    mute_control: MuteControl | None = None
    config_manager: ConfigManagerLike | None = None
    request_id: str | None = None
    command_invocation: CommandInvocation | None = None

    # 插件私有状态（当次请求生命周期）
    state: Dict[str, Any] = field(default_factory=dict)
```

`PluginContext` 构造时再次执行命名空间裁剪和冻结，因此绕过 `XiaoQingApp` 的扩展工厂也
不能注入全局管理员列表或其他插件秘密。插件读取自身值应使用 `get_config(path)` 和
`get_secret(path)`；管理员判断使用核心签发 principal/capability 支持的 `is_global_admin()`。

### 会话便捷方法

```python
async def create_session(self, initial_data=None, timeout=300.0) -> Session:
    """创建会话并返回隔离快照"""
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

async def update_session(self, callback):
    """在私有工作副本上执行一次原子读改写"""
    return await self.session_manager.update(
        self.current_user_id, self.current_group_id, callback
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

`codex` 插件使用这种方式在 CLI 任务完成后主动发送 `[codex:<label> #<job_id>]` 文字和图片结果；发送链路会继续负责纯文本长消息分割、WS/HTTP 回退和错误日志，Codex 插件会在混合图片消息前先拆分过长文本。`arxiv_filter` 的 Codex 摘要侧路也通过这种主动发送完成：论文列表由 `/arxiv` 正常返回，摘要或失败消息在后台任务结束后再发送。

---

## 💬 session.py 会话管理

### Session 类

```python
@dataclass
class Session:
    user_id: int
    group_id: Optional[int]
    plugin_name: str
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    state: str = "active"
    data: Dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    timeout: float = 300.0
    
    def get(self, key: str, default=None):
        return self.data.get(key, default)
    
    def set(self, key: str, value):
        self.data[key] = value
        self.update()

    def delete(self, key: str) -> bool: ...
    
    def is_expired(self) -> bool:
        return time.time() - self.updated_at > self.timeout
```

### SessionManager 类

```python
class SessionManager:
    def __init__(self, default_timeout=300.0):
        self._sessions: Dict[tuple, Session] = {}
        self._transactions = {}  # exact (asyncio.Task, key) -> staged transaction
    
    async def create(self, user_id, group_id, plugin_name, initial_data, timeout):
        """校验并克隆安全值树，存储正式值，返回另一个隔离快照"""
    
    async def get(self, user_id, group_id) -> Optional[Session]:
        """返回快照并刷新空闲超时；快照修改不会写回"""

    async def update(self, user_id, group_id, callback):
        """工作副本 -> callback -> 成功后第二次受控克隆并一次提交"""
```

会话键先把 `user_id/group_id` 规范化为正整数，timeout 必须是有限正数。每个键由独立锁
串行化，不同用户仍可并发；事务 callback 的异常、`BaseException`、取消或值树校验失败均
回滚正式值及元数据。普通 update 保留稳定 `session_id` 且 `version` 只增加一次，create
替换会生成新 ID。会话数据只接受字符串键的内建 `dict/list/tuple` 和标量，拒绝循环、
自定义对象及超限树。callback 必须返回普通值或未调度的 awaitable；误返现成
`asyncio.Task`/`Future` 时框架会先取消并 drain，再拒绝该事务。

这些限制不是面向普通聊天数据的额外样板，而是事务隔离边界：插件可控制会话值，
因此克隆不能执行自定义对象钩子，取消也不能遗留稍后提交的后台 Future。修改节点/深度
限制、克隆策略或取消漏斗时，必须同时验证快照隔离、异常回滚与取消后无延迟提交。

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
from .plugin_execution import offload_plugin_sync

async def run_sync(func: Callable, *args, **kwargs):
    """经当前插件的有界、公平同步 bulkhead 运行同步函数"""
    return await offload_plugin_sync(func, *args, **kwargs)
```

插件代码应调用 `run_sync()`，不要直接调用 `asyncio.to_thread()` 或默认 executor。只有拥有专用有界 executor、明确停止接纳并在自身 `shutdown()` 中有界 drain 的底层组件可以自管 worker。

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
        """连接并监听消息；所有断开路径都经过同一退避状态机。"""
        retry_base = 5.0
        while self._running:
            try:
                connected_seconds = await self._connect_once(handler)
            except Exception as exc:
                connected_seconds = None
            if connected_seconds is not None and connected_seconds >= 30:
                retry_base = 5.0
            await self._wait_for_reconnect(jitter(retry_base))
            retry_base = min(retry_base * 2, 60.0)
```

客户端把正常关闭、协议异常和连接失败统一视为一次断开：短连接按 5、10、20、40、60 秒基准递增，并在每次等待中加入连续有界抖动；最大档仍分布在 48–60 秒，不会让一半实例同时固定在 60 秒。只有连接持续至少 30 秒才重置。配置热更新会通过代际事件同时唤醒退避与连接 attempt；旧 recv 即使吞掉取消、socket close 失败或挂起，也会被隔离而不阻塞新代。

认证参数在启动和每次连接前解析。`websockets` 新接口使用 `additional_headers`，旧接口使用 `extra_headers`；参数必须在真实签名中明确允许关键字传入，签名检查失败、仅位置参数或同名 `**kwargs` 都不视为证明。只要 `onebot_token` 非空而无法确认支持，客户端就 fail closed，绝不会尝试无认证连接。VALID secrets 中缺省/明确空字符串才是合法匿名模式；来源故障或 token 类型错误会撤销 HTTP/WS holder，任何网络调用都在边界前返回。同步热更新还会核验 holder 的 endpoint/token/trust 后置状态，未实际执行更新的兼容对象会被摘除或隔离，直到新的 VALID revision 重建可信 holder。

---

## 🖥️ server.py Inbound 服务器

`InboundManager` 可让 HTTP 与 WS 共用一个 listener，也可在不同端口创建两个 `InboundServer`；两种布局都注入同一个 `_InboundEventDispatcher`。HTTP `/event` 与 WS `/ws` 完成鉴权、解析和来源归一化后，在 dispatcher 的 `admit` 处取得接纳序号。同一私聊用户或“群 + 用户”键只有一个 in-flight handler，跨 HTTP/WS 仍严格 FIFO；不同键由有界 worker 公平并行。

dispatcher 容量为 worker 数加等待上限。过载快速拒绝，排队取消会物理摘除 ticket，handler 执行前还会复验 token 代；旧凭据排队项不会产生业务副作用。handler 的同步/异步普通异常、真实 `BaseException` 和非法返回值都转为 task-safe outcome，不会让 worker 静默死亡或遗留未完成 ticket。

停止时先同步关闭接纳，再有界排空或取消剩余项。配置热切换若使用不同端口，会先预绑定不接纳的候选 listener，排空旧 dispatcher 后再整代提交；同端口则先安全停止旧代。部分 child stop 失败时 manager 保留原 server、共享 dispatcher 和最新 token 的所有权，以便后续重试。FIFO 边界到 handler 返回为止；HTTP 响应和 WS Action 写入是另一个有界传输阶段，慢客户端不会阻塞同键状态机。



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
