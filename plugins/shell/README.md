# Shell 插件

Shell 命令执行插件，提供受控的系统命令执行功能。

## 功能介绍

Shell 插件允许授权用户在服务器上执行预定义的 Shell 命令，支持命令白名单和权限控制。

## 使用方法

### 基本命令

```
/shell <命令>           # 执行 Shell 命令
/shell help             # 显示帮助信息
```

### 示例

```
/shell ls               # 列出文件
/shell pwd              # 显示当前目录
/shell df -h            # 查看磁盘使用情况
/shell help             # 显示帮助
```

## 配置说明

在 `config/secrets.json` 中配置：

```json
{
  "plugins": {
    "shell": {
      "allowed_users": ["user_id_1", "user_id_2"],
      "allowed_commands": ["ls", "pwd", "df", "date", "uptime"],
      "command_mode": "extend",
      "timeout": 30
    }
  }
}
```

### 配置项说明

- `allowed_users` - 允许使用此插件的用户 ID 列表（必需）
- `allowed_commands` - 允许执行的命令列表（必需）
- `command_mode` - 命令模式
  - `extend`: 扩展默认白名单
  - `replace`: 替换默认白名单
- `timeout` - 命令执行超时时间（秒，默认: 30）

### 默认白名单命令

```
ls, pwd, date, whoami, uptime, df, free, ps, top, 
netstat, ifconfig, ip, ping, traceroute, dig, nslookup
```

## 安全特性

- 命令白名单机制
- 用户权限控制
- 执行超时保护
- 危险命令过滤
- 参数注入防护

## 注意事项

⚠️ **安全警告**：
- 此插件具有系统命令执行权限，使用时请特别小心
- 必须配置 `allowed_users` 限制可操作用户
- 仅添加必要的命令到白名单
- 定期审查命令执行日志
- 不建议在生产环境中开放此插件

## 适用场景

- 开发和测试环境
- 服务器状态监控
- 简单的运维操作
- 受控的命令执行需求

## 不推荐用途

- 生产环境中的日常操作
- 复杂的批处理任务
- 需要交互的命令
- 长时间运行的进程
