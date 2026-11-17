"""
XiaoQing 核心模块

应用与收发
- app / app_*: 主应用入口及其 Mixin，组装配置、插件、调度与收发链路
- config: 配置加载、快照与热重载
- dispatcher / router / args / message: 消息分发、命令路由、命令参数与消息解析
- session: 多轮对话会话
- scheduler / durable_fanout: 定时任务与通知投递进度
- server / onebot / inbound_*: 入站 HTTP/WebSocket 服务、OneBot 协议客户端、入站消息登记与监听策略
- delivery: 发送回执

插件
- plugin_manager / plugin_*: 插件加载、执行、数据目录与热重载
- context / interfaces / capabilities: 提供给插件的上下文、接口契约与受限能力
- plugin_base / public_errors: 插件开发用的基础工具与对外错误响应
- ai: 统一的模型路由与有界请求

对外输入与本地存储
- bounded_http / safe_http / image_validation: 外部 HTTP 响应与图片的上限和校验
- atomic_store / bounded_file_cache / private_files / async_keyed_lock: 原子落盘、磁盘缓存、私有文件与按键互斥
- process_tree / windows_job: 外部命令的进程树归属与回收

基础
- auth / lifecycle: Bearer 校验、取消与致命异常处理
- clock / constants / exceptions / models / version / log / metrics: 时钟、常量、异常、数据模型、版本、日志与性能统计
"""
