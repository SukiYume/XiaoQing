# QingSSH - SSH Remote Control Plugin
#
# 模块结构：
# - main.py: 命令入口和路由
# - handlers.py: 命令处理器（处理 /ssh, /ssh列表 等命令）
# - session_handlers.py: 会话处理器（处理多轮对话流程）
# - ssh_manager.py: SSH 连接管理器（核心功能）
# - config.py: 配置常量（超时、关键词、默认值等）
# - validators.py: 输入验证工具（服务器名、端口、主机地址）
# - message_formatter.py: 消息格式化工具（统一 UI 风格）
# - types.py: 类型定义（Protocol 接口）
