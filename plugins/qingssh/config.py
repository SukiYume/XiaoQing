"""QingSSH 会话、连接、命令执行和状态键常量。"""

SESSION_TIMEOUT = 600.0  # 10 分钟
ADD_SERVER_TIMEOUT = 300.0  # 5 分钟

# 命令执行和输出边界
COMMAND_TIMEOUT = 30
MAX_OUTPUT_LENGTH = 2000

CONNECT_TIMEOUT = 10

# 会话控制词
CANCEL_KEYWORDS = {"取消", "cancel", "退出添加", "放弃"}
STOP_KEYWORDS = {"停止", "stop", "cancel", "ctrl+c"}

# 插件内部保留的负数返回码，避免与远端 shell 返回码冲突。
EXIT_CODE_INTERRUPTED = -999
EXIT_CODE_TIMEOUT = -998
EXIT_CODE_ERROR = -1


class SessionKeys:
    """集中定义会话字典键，避免跨模块使用不同拼写。"""

    STATE = "state"
    STEP = "step"
    SERVER_NAME = "server_name"
    SERVER_CONFIG = "server_config"
    COMMAND_COUNT = "command_count"
    HOST = "host"
    CWD = "cwd"  # 当前工作目录
    ENV_VARS = "env_vars"  # 环境变量字典
    HISTORY = "history"  # 命令历史列表
    USERNAME_OVERRIDE = "username_override"  # 用户名覆盖（用于 user@server 格式）
    CURRENT_TASK = "current_task"  # 当前命令的不可变 job_id（绝不存 asyncio.Task）


MAX_HISTORY_LENGTH = 20
