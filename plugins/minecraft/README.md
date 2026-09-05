# ⛏️ Minecraft

`minecraft` 让 Bot 管理员通过 QQ 私聊连接 Minecraft Java Edition RCON，并把本机可读的服务器日志事件转发到发起连接的私聊。Java 服务进程由部署工具负责管理。

---

## 🔐 权限与命令

三个 Manifest 命令均为管理员私聊入口。

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
| `/mc connect <配置名>` | 读取本地 profile 并连接 |
| `/mc status` | 查看当前私聊的连接和日志监控状态 |
| `/mc <服务器命令>` | 执行完整 RCON 命令 |
| `/mc say <消息>` | 向全部在线玩家广播 |
| `/mc tell <玩家名> <消息>` | 向指定玩家发送私信 |
| `/mc disconnect` | 关闭当前私聊的连接 |
| `/mcconnect <配置名>` | 等价于 `/mc connect <配置名>` |
| `/mcdisconnect` | 等价于 `/mc disconnect` |

`say`、`tell` 和 `tellraw` 属于 Minecraft 服务端命令。插件会把 `/mc` 后的完整内容通过 RCON 发送。

---

## ⚙️ 配置

在 `plugins/minecraft/config.json` 中保存连接 profile：

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

profile 文件上限为 64 KiB。日志路径读取异常时，RCON 连接继续用于服务器命令。

---

## 🔐 RCON 安全

Minecraft 服务端需要启用 RCON，并使用与 profile 一致的端口和密码。Source RCON 以明文传输密码与命令，建议采用以下网络边界：

- 服务端监听回环地址；
- 跨主机连接通过 SSH 本地端口转发或受保护隧道；
- 防火墙只放行 Bot 主机；
- RCON 使用独立强密码。

协议格式参阅 [Valve Source RCON Protocol](https://developer.valvesoftware.com/wiki/Source_RCON_Protocol)。

---

## 🔐 连接与响应边界

- 每名私聊管理员拥有独立连接；重连会原子发布新连接并关闭旧连接；
- 单连接内的建连、认证、命令和关闭按顺序执行；
- 命令长度上限为 4096 UTF-8 字节；
- 协议校验覆盖小端长度、请求 ID、包类型、双 NUL、UTF-8 和累计响应；
- 单次响应内存上限为 1 MiB；
- QQ 文本上限为 4000 字符和 12 KiB，并清理 ANSI、C0 与 C1 控制序列；
- 分包边界同时按 UTF-8 字节和 Java UTF-16 单元识别；
- 续包读取超时、半包、EOF 或协议校验失败会关闭当前连接，再次执行命令前需要重新连接以获得干净的响应流；
- 日志记录命令、响应和目标的类型、长度、字节数与单向指纹。

---

## 💾 日志转发

配置 `log_file` 后，Manifest 每 5 秒检查一次 Minecraft `INFO` 日志。识别事件包括：

- 玩家聊天；
- 玩家加入与离开；
- 玩家死亡；
- 玩家进度。

消息发送到发起连接的 QQ 私聊。初次启用从文件末尾建立游标，后续进度保存在：

```text
data/minecraft/log_cursors/
```

OneBot 明确确认发送后，插件提交对应游标。发送异常、超时和待处理批次会保留游标供下一轮继续。日志轮换、截断和末尾半行由监视器识别。

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

每个连接拥有独立 RCON 锁，5 秒调度任务以 Core `targeted` 模式使用全局锁串行轮询；群目标受该 schedule 的目标群约束，私聊目标由连接所有者确定。卸载、重载或 Bot 关闭时，`shutdown()` 摘除并关闭全部 RCON 连接，同时清理进程内限流状态。日志游标保留在数据目录供重启恢复。

---

## 🩺 排障

| 现象 | 检查项 |
| --- | --- |
| RCON 认证失败 | 核对服务端配置、profile 名和 secret 密码 |
| RCON 连接失败 | 检查监听地址、端口、防火墙和隧道 |
| `/mc say` 执行异常 | 先用 `/mc list` 验证连接，再核对服务端命令权限 |
| QQ 收到玩家登录消息 | 说明日志转发与游标提交链路正常 |
| QQ 收取日志异常 | 核对 `log_file`、Bot 账户读取权限和 `/mc status` |
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
