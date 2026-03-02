# QingSSH 插件

SSH 远程管理插件，提供安全的远程服务器连接和命令执行功能。

## 功能介绍

QingSSH 插件允许通过 XiaoQing 管理远程服务器，支持 SSH 连接、命令执行、文件传输等功能。

## 使用方法

### 基本命令

```
/ssh connect <主机>      # 连接到服务器
/ssh exec <命令>         # 执行命令
/ssh disconnect          # 断开连接
/ssh status              # 查看连接状态
/ssh help                # 显示帮助信息
```

### 示例

```
/ssh connect server1
/ssh exec ls -la
/ssh exec df -h
/ssh status
/ssh disconnect
/ssh help
```

## 配置说明

在 `config/secrets.json` 中配置：

```json
{
  "plugins": {
    "qingssh": {
      "servers": {
        "server1": {
          "host": "192.168.1.100",
          "port": 22,
          "username": "user",
          "password": "password",
          "key_file": "/path/to/private_key"
        },
        "server2": {
          "host": "example.com",
          "port": 22,
          "username": "admin",
          "key_file": "/path/to/key"
        }
      },
      "allowed_users": ["user_id_1", "user_id_2"],
      "timeout": 30,
      "max_output_length": 4000
    }
  }
}
```

### 配置项说明

- `servers` - 服务器配置列表（必需）
  - `host` - 服务器地址
  - `port` - SSH 端口（默认: 22）
  - `username` - 登录用户名
  - `password` - 密码（可选，推荐使用密钥）
  - `key_file` - SSH 私钥文件路径（推荐）
- `allowed_users` - 允许使用此插件的用户 ID
- `timeout` - 命令执行超时（秒，默认: 30）
- `max_output_length` - 最大输出长度（字符，默认: 4000）

## 功能特性

- 多服务器管理
- SSH 密钥认证
- 命令执行
- 会话管理
- 输出截断保护
- 权限控制
- 连接状态监控
- 超时保护

## 安全特性

- 支持密钥认证（推荐）
- 用户权限控制
- 命令执行超时
- 敏感信息过滤
- 连接状态验证

## 注意事项

⚠️ **安全警告**：
- 此插件具有远程服务器访问权限，请特别小心
- 强烈推荐使用 SSH 密钥认证而非密码
- 必须配置 `allowed_users` 限制可操作用户
- 定期审查命令执行日志
- 不建议在生产环境中开放给所有用户

## 推荐配置

1. 使用 SSH 密钥认证
2. 限制 allowed_users
3. 设置合理的超时时间
4. 配置输出长度限制
5. 使用非标准 SSH 端口

## 依赖

- paramiko (SSH 客户端库)

安装依赖：
```bash
pip install paramiko
```

## 适用场景

- 远程服务器监控
- 快速运维操作
- 服务状态查询
- 日志查看
- 简单的批量操作
