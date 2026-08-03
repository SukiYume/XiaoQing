# Minecraft 插件

管理员通过 XiaoQing 私聊连接一个或多个 Minecraft Java Edition RCON 服务，并可选择把本机可读的
服务器日志事件转发到发起连接的私聊。插件不负责启动 Java 进程，也不提供独立的
`start`、`stop` 或自动重启服务；如需停止服务器，应在连接后执行 Minecraft 自身支持的 RCON
命令，并自行管理服务进程。

## 命令

```text
/mc help                    # 查看帮助
/mc connect <配置名>        # 读取本地 config.json 并连接
/mc status                  # 查看当前私聊的连接状态
/mc <服务器命令>            # 执行 RCON 命令，例如 /mc list
/mc disconnect              # 关闭当前私聊的连接
```

<!-- manifest-command-aliases:start -->
| 功能 | 推荐入口 | manifest 等价别名 |
| --- | --- | --- |
| Minecraft 管理 | `/mc` | `/minecraft` |
| 直接进入连接流程 | `/mcconnect` | `/mc连接` |
| 直接断开当前连接 | `/mcdisconnect` | `/mc断开` |
<!-- manifest-command-aliases:end -->

`/mcconnect default` 等价于 `/mc connect default`；`/mcdisconnect` 等价于
`/mc disconnect`。所有入口均由 manifest 标记为管理员专用且仅允许私聊。

## 配置

在被 `.gitignore` 排除的 `plugins/minecraft/config.json` 中只保存非敏感 profile。不要把主机、
路径或密码作为聊天参数发送：

```json
{
  "default": {
    "host": "127.0.0.1",
    "port": 25575,
    "log_file": "C:/minecraft/logs/latest.log"
  },
  "staging": {
    "host": "mc.internal.example",
    "port": 25575,
    "log_file": ""
  }
}
```

- 在 `config/secrets.json` 的 `plugins.minecraft` 下，以同名 profile 保存 RCON 密码：

```json
{
  "plugins": {
    "minecraft": {
      "default": "请替换为强密码",
      "staging": "请替换为另一条强密码"
    }
  }
}
```

- profile 名最长 64 字，只接受字母、数字、下划线、点和连字符。
- `host` 必须是无空白和控制字符的主机名或 IP；`port` 必须在 1–65535。
- 密码只从同一 profile 的 `config/secrets.json` 密钥读取；非空、不能含 NUL，
  UTF-8 编码后最多 4096 字节。公开 `config.json` 中出现 `password` 会被拒绝。
- `log_file` 可留空。相对路径以 `plugins/minecraft/` 为基准；绝对路径按原路径读取。
- `config.json` 最大 64 KiB。配置错误只返回固定诊断，不回显密码或底层异常。
- 日志路径不可用时仍会保留已成功建立的 RCON 连接，只停用日志转发。

Minecraft 服务端需要启用 RCON，并使这里的端口与密码和服务端配置一致。Source RCON 本身不加密，
密码和命令会在网络上明文传输；优先让服务端只监听 `127.0.0.1`，跨主机访问时通过 SSH
本地端口转发或其他受保护隧道连接，并用防火墙限制 RCON 端口，禁止暴露到不可信网络。
协议格式可参阅 [Valve Source RCON Protocol](https://developer.valvesoftware.com/wiki/Source_RCON_Protocol)。

## 连接与命令边界

- 每名私聊管理员拥有独立连接；同一目标重新连接时先原子发布新连接，再关闭旧连接。
- RCON 建连、认证、命令和关闭在单连接内串行执行，避免并发命令互相覆盖流对象。
- 命令最多 4096 UTF-8 字节，禁止嵌入 NUL。
- 客户端严格校验小端长度、请求 ID、包类型、双 NUL、UTF-8 和累计响应上限。
- Minecraft 的 4096 字符分包同时按 UTF-8 字节和 Java UTF-16 单元识别；整块响应没有额外终止包时，
  短暂无后续数据会结束本次收集，并在 QQ 回复中标明“响应可能不完整”。
- 单条 RCON 响应最多在内存中累计 1 MiB；发回 QQ 前清除 ANSI/C0/C1 控制序列，并截到
  4000 字和 12 KiB。
- 普通日志只记录命令、响应和连接目标的类型、长度、字节数与进程内不可逆指纹，不记录原文。

## 日志监控与投递确认

配置 `log_file` 后，插件每 5 秒检查一次 Minecraft `INFO` 日志，识别聊天、加入、离开、死亡和
进度事件。

- 初次启用且没有历史游标时从文件末尾开始，不回放整份旧日志。
- 游标保存在插件运行数据目录的 `log_cursors/`；只有 OneBot 明确返回 `True` 后才提交。
- 发送失败、超时或本轮未选中的批次不会推进游标，下轮继续读取；损坏游标从当前末尾安全恢复。
- 文件轮换、截断和未写完的最后一行会被识别；读取和 `fstat` 固定使用同一文件句柄。
- 每次最多读取最新 1 MiB，并保留最近 1000 个匹配事件；更早内容以精确字节数和可用行数汇总。
- 每个目标每轮最多转发 12 个事件，跨轮 token bucket 容量为 24、每秒补充 0.5 个 token。
- 每轮全局最多发送 5 个 action，并轮转起点保证多目标公平；不会把其他私聊的溢出统计发给
  某一个目标。
- 每条日志 action 同时受 1800 字和 6000 UTF-8 字节上限约束，过量事件合并为丢弃摘要。

## 运行要求与排障

- 纯 RCON 模式只要求 XiaoQing 能访问服务器 TCP 端口，不要求 Minecraft 与 XiaoQing 位于同一台
  主机。
- 日志转发要求 `log_file` 在 XiaoQing 运行主机上是可读普通文件；远程服务器日志不会自动下载。
- `status` 显示“日志监控未启用”时，先检查 `log_file` 是否为空、相对路径基准是否正确，以及运行
  XiaoQing 的账户是否有读取权限。
- 认证失败、网络不可用、超时、协议错误和响应超限会分别返回固定提示；失败连接会被重置，避免
  把错误显示成“空响应成功”。
