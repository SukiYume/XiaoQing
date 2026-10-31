"""
XiaoQing 核心模块

- app: 主应用入口，组装配置、插件、调度与收发链路
- config: 配置加载、快照与热重载
- dispatcher / router: 消息分发与命令路由
- plugin_manager / plugin_*: 插件加载、执行与热重载
- scheduler: 定时任务
- server / onebot: 入站 HTTP/WebSocket 服务与 OneBot 协议客户端
- session: 多轮对话会话
- context / interfaces: 提供给插件的上下文与接口契约
"""
