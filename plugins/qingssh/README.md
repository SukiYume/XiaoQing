# QingSSH 插件

SSH 远程控制插件，支持保存服务器配置、从 `~/.ssh/config` 导入、交互式命令执行和远程图片查看。

## 常用命令

```text
/ssh help
/ssh <服务器名>
/ssh <用户名>@<服务器名>
/ssh添加 <名称> <主机> [端口] [用户名]
/ssh列表
/ssh状态
/ssh断开
/ssh导入 <Host名>|all
/sshconfig
```

连接建立后，直接发送命令即可执行；发送 `停止` 可中断当前命令，发送 `退出` / `取消` 可结束会话。

## 会话与隔离

- 连接按 `用户 + 群 + 服务器` 隔离。
- 同一用户跨群不会复用连接状态。
- 不同用户即使连接同一台服务器，也拥有独立会话环境。

## 安全行为

- 默认严格校验 `~/.ssh/known_hosts` 中的 Host Key。
- 未知主机或 Host Key 变更不会自动放行，需要先修复 `known_hosts`。
- 导入 `~/.ssh/config` 时支持 `ProxyJump` 和安全的 `ssh -W` 跳板形式。
- 其他会在本地执行命令的 `ProxyCommand` 会被拒绝。
- 推荐优先使用私钥认证，而不是密码认证。

## 服务器配置

服务器配置保存在 `plugins/qingssh/data/servers.json`：

```json
{
  "myserver": {
    "host": "192.168.1.100",
    "port": 22,
    "username": "root",
    "password": "password123",
    "key_file": "~/.ssh/id_rsa"
  }
}
```

认证优先级为“密钥优先于密码”。

## 远程图片

```text
/showimg /home/user/plot.png
/showimg user@server:/data/chart.png
```

## 注意事项

- 仅管理员可用。
- 会话空闲 10 分钟后会自动断开。
- 远程命令具有高权限，请谨慎开放服务器配置和导入来源。
