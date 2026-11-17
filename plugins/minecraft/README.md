# ⛏️ Minecraft

`minecraft` 让 Bot 管理员在 QQ 私聊或群里连接 Minecraft Java Edition RCON，并把本机可读的服务器日志事件转发到这个私聊或群。连上之后，群里所有人都能和游戏里的玩家聊天。服务器的启动和停止不由本插件管理。

---

## 🔐 权限与命令

三个 Manifest 命令支持私聊和群聊。连接、断开和 `/mc run` 服务器命令需要 Bot 管理员权限。`/mc say`、`/mc tell`、`/mc list`、`/mc status`、`/mc help` 所有人可用，使用的是当前私聊或群的连接。

<!-- manifest-command-aliases:start -->
| 功能 | 推荐入口 | Manifest 等价别名 |
| --- | --- | --- |
| Minecraft 管理 | `/mc` | `/minecraft` |
| 直接连接 | `/mcconnect` | `/mc连接` |
| 直接断开 | `/mcdisconnect` | `/mc断开` |
<!-- manifest-command-aliases:end -->

| 用法 | 说明 |
| --- | --- |
| `/mc help` | 显示本地帮助 |
| `/mc connect <配置名>` | 读取配置中的服务器 profile 并连接（Bot 管理员） |
| `/mc status` | 查看本会话的连接和日志监控状态 |
| `/mc list` | 查看在线玩家（所有人） |
| `/mc say <消息>` | 用自己的 QQ 名字对全部在线玩家说（所有人） |
| `/mc tell <玩家名> <消息>` | 悄悄对一名玩家说（所有人） |
| `/mc run <服务器命令>` | 原样执行一条 RCON 命令（Bot 管理员） |
| `/mc disconnect` | 关闭本会话的连接（Bot 管理员） |
| `/mcconnect <配置名>` | 等价于 `/mc connect <配置名>` |
| `/mcdisconnect` | 等价于 `/mc disconnect` |

`/mc connect` 另有别名 `连接`，`/mc disconnect` 为 `断开`，`/mc status` 为 `状态`，`/mc run` 为 `执行`，`/mc help` 为 `帮助`、`?`。

`/mc say` 与 `/mc tell` 以 `tellraw` 文本组件发出，游戏里显示为 `[QQ] <QQ 名字> 消息`。消息合并为一行，最长 256 个字符，其中的 `@a`、`@e` 等目标选择器和命令都只当文字。`/mc tell` 的玩家名限 1～16 个字母、数字或下划线。`/mc run` 的内容由 Bot 管理员原样通过 RCON 发送。

---

## ⚙️ 配置

在 `config/config.json` 的 `plugins.minecraft.servers` 下保存连接 profile：

```json
{
  "plugins": {
    "minecraft": {
      "servers": {
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
    }
  }
}
```

在 `config/secrets.json` 中用同名 profile 保存 RCON 密码：

```json
{
  "plugins": {
    "minecraft": {
      "default": "replace-with-a-strong-password",
      "staging": "replace-with-another-strong-password"
    }
  }
}
```

| 字段 | 规则 |
| --- | --- |
| profile 名 | 1～64 个字母、数字、下划线、点或连字符 |
| `host` | 结构完整的主机名或 IP，最长 253 个字符 |
| `port` | 1～65535 的整数，默认 25575 |
| RCON 密码 | 来自 `config/secrets.json` 的同名项，UTF-8 上限 4096 字节 |
| `log_file` | 空字符串关闭日志转发；相对路径以 `plugins/minecraft/` 为基准 |

日志路径读取异常时，RCON 连接继续用于服务器命令。

Minecraft 服务端需要启用 RCON，端口和密码与 profile 一致。在服务端的 `server.properties` 中设置后重启服务端：

```properties
enable-rcon=true
rcon.port=25575
rcon.password=replace-with-a-strong-password
```

---

## 🔐 RCON 安全

Source RCON 以明文传输密码与命令，建议采用以下网络边界：

- 服务端监听回环地址。
- 跨主机连接通过 SSH 本地端口转发或受保护隧道。
- 防火墙只放行 Bot 主机。
- RCON 使用独立强密码。

协议格式参阅 [Valve Source RCON Protocol](https://developer.valvesoftware.com/wiki/Source_RCON_Protocol)。

---

## 🔐 连接与响应边界

- 每个私聊或群拥有独立连接，重新连接会关闭旧连接。
- 命令长度上限为 4096 UTF-8 字节。
- 单次响应上限为 1 MiB。
- QQ 文本上限为 4000 字符和 12 KiB，并清理 ANSI、C0 与 C1 控制序列。
- 续包读取超时、半包、EOF 或响应不符合 RCON 协议时会关闭当前连接，再次执行命令前需要重新连接。
- 日志里的命令、响应、连接目标和日志路径只有长度与进程内指纹，不记录内容。

---

## 💾 日志转发

配置 `log_file` 后，Manifest 每 5 秒检查一次 Minecraft `INFO` 日志。识别事件包括：

- 玩家聊天。
- 玩家加入与离开。
- 玩家死亡。
- 玩家进度。

消息发送到连接所属的私聊或群。群只有在 `check_log` 定时任务的目标群里才会收到转发，目标群在 `config/config.json` 中配置：

```json
{
  "plugins": {
    "minecraft": {
      "schedules": {
        "check_log": {
          "group_ids": [123456789]
        }
      }
    }
  }
}
```

没写 `group_ids` 时使用全局 `default_group_ids`，写空列表则不向任何群转发。其他群连接时，回复会说明不转发日志、只能用命令。连接后从目标群里移除的群暂停轮询。私聊连接不受目标群限制。

初次启用从文件末尾建立游标，后续进度保存在：

```text
data/minecraft/log_cursors/
```

发送成功后游标前进。发送结果未知（例如等待回执超时）时游标同样前进，宁可漏转这一批也不重复刷屏。明确失败或发送异常时保留游标，下一轮继续转发。日志轮换、截断和末尾半行会被正确处理。

---

## 📌 转发预算

| 项目 | 当前预算 |
| --- | ---: |
| 单次文件读取 | 最新 1 MiB |
| 单次保留匹配事件 | 1000 个 |
| 单连接单轮事件 | 12 个 |
| 全局单轮 action | 5 个 |
| 单条 action | 1800 字符、6000 UTF-8 字节 |
| Token bucket | 容量 24，每秒补充 0.5 |

调度器轮转连接起点，让多个目标获得公平投递机会。超过展示预算的事件会汇总为统计摘要。

---

## ⏰ 生命周期

卸载、重载或 Bot 关闭时，所有 RCON 连接都会关闭，进程内的限流状态随之清除。日志游标保留在数据目录，重启后继续。

---

## 🩺 排障

| 现象 | 检查项 |
| --- | --- |
| RCON 认证失败 | 核对服务端配置、profile 名和 secret 密码 |
| RCON 连接失败 | 检查监听地址、端口、防火墙和隧道 |
| `/mc say` 执行异常 | 先用 `/mc list` 验证连接，再核对服务端命令权限 |
| QQ 收取日志异常 | 核对 `log_file`、Bot 账户读取权限、`check_log` 目标群和 `/mc status` |
| 响应显示截取提示 | 缩小服务器命令结果，或在服务端控制台查看完整输出 |

---

## ✅ 开发验证

在仓库根目录运行：

```bash
python -m pytest -q tests/plugins/minecraft/test_minecraft.py \
  tests/plugins/minecraft/test_minecraft_flood.py \
  tests/plugins/minecraft/test_minecraft_rcon_results.py
python -m ruff check plugins/minecraft
python -m mypy plugins/minecraft
```
