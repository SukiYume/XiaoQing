# QingSSH 插件

仅供 Bot 管理员在私聊中使用的 SSH 远程控制插件，支持保存服务器配置、从 `~/.ssh/config` 导入、交互式命令执行和远程图片查看。

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

<!-- manifest-command-aliases:start -->
| 功能 | 推荐入口 | manifest 等价别名 |
| --- | --- | --- |
| SSH 主入口 | `/ssh` | `/SSH`、`/远程`、`/ssh连接`、`/sshconnect` |
| 断开连接 | `/ssh断开` | `/sshdisconnect`、`/ssh退出` |
| 服务器列表 | `/ssh列表` | `/sshlist`、`/ssh服务器` |
| 添加服务器 | `/ssh添加` | `/sshadd` |
| 删除服务器 | `/ssh删除` | `/sshremove`、`/sshdel` |
| 导入 SSH config | `/ssh导入` | `/sshimport` |
| 查看 SSH config | `/sshconfig` | `/ssh配置` |
| 活跃连接状态 | `/ssh状态` | `/sshstatus`、`/ssh连接数`、`/sshactive` |
<!-- manifest-command-aliases:end -->

表内 alias 都由 manifest 注册并受相同管理员与私聊场景约束。英文短名和旧连接入口仅为
兼容既有命令习惯保留，不提供额外权限或不同的连接策略。

连接建立后，直接发送命令即可执行；发送 `停止` 会优先向该命令的远端进程组发送 `TERM`，必要时升级为 `KILL`，并始终清理本地命令通道。若停止发生在远端 PID 标记返回前，Bot 会明确提示“远端状态未知”，不会把本地通道关闭误报成远端进程已退出。发送 `退出` / `取消` 可结束会话。

远端命令本身不受 allowlist 或内容截断限制。为避免高输出命令淹没 OneBot 私聊，QQ 侧只投影有界的开头与末尾，并限制单条命令的 action 数、累计文字量、发送频率和单次发送等待时间；超出 QQ 预算时，完整输出会保存到仅本机可访问的 `data/command_outputs/ssh-output-*.txt`。归档也有独立磁盘硬上限，超过时明确保留受控首尾。取消任务会先回收远端命令，再删除未提交的临时归档。

## 会话与隔离

- 连接按 `私聊用户 + 服务器` 隔离。
- 不同用户即使连接同一台服务器，也拥有独立会话环境。
- 每条后台命令使用唯一 `job_id`；命令只会在启动它的会话事务提交后连接 SSH。
- 命令结束时通过 `job_id` 原子更新目录和状态；会话已退出、回滚或被替换时，旧任务不会复活或覆盖新会话。

## 安全行为

- 默认严格校验 `~/.ssh/known_hosts` 中的 Host Key。
- 未知主机或 Host Key 变更不会自动放行，需要先修复 `known_hosts`。
- 导入 `~/.ssh/config` 时支持单跳 `ProxyJump` 和安全的 `ssh -W` 跳板形式。
- 其他会在本地执行命令的 `ProxyCommand` 会被拒绝。
- 推荐优先使用私钥认证，而不是密码认证。
- 只有 `~/.ssh/config` 中明确声明、且不含通配符的 `Host` 才能直接连接或导入。

## 服务器配置

服务器配置保存在插件数据目录的 `servers.json`。密钥认证示例：

```json
{
  "myserver": {
    "host": "192.168.1.100",
    "port": 22,
    "username": "root",
    "auth_type": "key",
    "key_path": "/home/bot/.ssh/id_rsa"
  }
}
```

认证方式由 `auth_type` 明确指定，可选 `agent`、`key` 或 `password`。密码只允许在管理员私聊的引导式 `/ssh添加` 中输入，明文不会进入会话状态或 `servers.json`；配置文件只保存由插件生成的 `password_ref`，实际密码由 core 密钥存储管理。旧配置中的明文 `password` 会在启动时事务式迁移，写盘失败则回滚新建密钥。不要手工把 `password` 字段写入服务器配置。

## 远程图片

```text
showimg plot.png
showimg *.png
```

`showimg` 只能在已连接的 SSH 会话中使用，会从当前远端工作目录选择最多五张、每张不超过 10 MiB 的图片。临时下载文件在 OneBot 接收动作后立即删除，失败或取消时也会清理。

## 注意事项

- 仅 Bot 管理员私聊可用。
- 会话空闲 10 分钟后会自动断开。
- 远端命令控制依赖 POSIX `sh`、`setsid` 和 `kill`；不具备这些工具的目标系统不在支持范围内。
- 远程命令具有高权限，请谨慎开放服务器配置和导入来源。

## 输出与超时配置

以下选项位于全局配置的 `plugins.qingssh`；`command_timeout_seconds = 0` 明确表示不设置命令时限，适合可信管理员的长任务。QQ 与归档预算只保护 Bot/消息通道，不会修改或提前截断远端命令。

```json
{
  "plugins": {
    "qingssh": {
      "command_timeout_seconds": 30,
      "max_connections": 32,
      "qq_max_actions": 6,
      "qq_max_text_chars": 10000,
      "qq_max_message_chars": 1800,
      "qq_head_chars": 6000,
      "qq_tail_chars": 2000,
      "qq_send_interval_seconds": 0.35,
      "qq_send_timeout_seconds": 5,
      "archive_max_bytes": 67108864,
      "archive_tail_bytes": 1048576,
      "archive_retention_files": 20
    }
  }
}
```
