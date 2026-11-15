# 🔐 QingSSH

QingSSH 为 Bot 管理员提供 SSH 远程控制（支持私聊和群聊），支持保存服务器、导入 `~/.ssh/config`、持久会话、流式命令输出、远端命令终止和图片查看。

---

## 🔐 权限与依赖

全部 Manifest 入口均为 Bot 管理员命令，支持私聊和群聊。群里的命令输出全群可见。安装 SSH 后端：

```bash
python -m pip install -e ".[ssh]"
```

未安装 `paramiko` 时插件仍会加载，使用 SSH 时提示安装依赖。安装后发送 `/reload` 重载插件。

远端命令继承目标 SSH 账号权限。服务器配置、私钥、密码 secret 与 Bot 管理员列表共同构成运行安全边界。

---

## ⌨️ 命令

<!-- manifest-command-aliases:start -->
| 功能 | 推荐入口 | Manifest 等价别名 |
| --- | --- | --- |
| SSH 管理与连接 | `/ssh` | `/SSH` `/远程` `/ssh连接` `/sshconnect` |
| 断开连接 | `/ssh断开` | `/sshdisconnect` `/ssh退出` |
| 服务器列表 | `/ssh列表` | `/sshlist` `/ssh服务器` |
| 添加服务器 | `/ssh添加` | `/sshadd` |
| 删除服务器 | `/ssh删除` | `/sshremove` `/sshdel` |
| 导入 SSH config | `/ssh导入` | `/sshimport` |
| 查看 SSH config | `/sshconfig` | `/ssh配置` |
| 活跃连接状态 | `/ssh状态` | `/sshstatus` `/ssh连接数` `/sshactive` |
<!-- manifest-command-aliases:end -->

表里的入口和 `/ssh` 的同名子命令走同一个处理器；`/ssh` 的子命令如下：

```text
/ssh help
/ssh list
/ssh add [名称 主机 [端口] [用户名]]
/ssh remove <服务器名>
/ssh import [Host名|all|全部]
/ssh config
/ssh status
/ssh disconnect [服务器名]
/ssh <服务器名>
/ssh <用户名>@<服务器名>
```

独立入口与相应 `/ssh` 子命令共享相同权限、参数和连接策略。

`/ssh add` 带参数时为快速添加：端口默认 22，用户名默认 `root`，使用 SSH Agent 认证。密码认证只能在管理员私聊中用不带参数的引导式 `/ssh add`。

---

## ⚙️ 服务器配置

保存的 profile 位于：

```text
data/qingssh/servers.json
```

私钥认证示例：

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

`auth_type` 接受 `agent`、`key` 和 `password`。密码通过管理员私聊的 `/ssh add` 引导输入（群里添加服务器只能选密钥或 SSH Agent，选密码会被拒绝），由 Core 密钥存储保存。`servers.json` 只记录指向密码的引用 `password_ref`，不含密码本身。

主机名、端口、用户名、服务器别名和路径均执行长度、字符与范围校验。

---

## 📅 SSH config 导入

`/ssh config` 列出 `~/.ssh/config` 中具有明确 Host 名的条目。`/ssh import <Host>` 和 `/ssh import all` 将可用条目复制到 `servers.json`。

支持的跳板形式：

- 单跳 `ProxyJump`。
- 结构可验证的 `ssh -W` ProxyCommand。

Host Key 始终通过 `~/.ssh/known_hosts` 严格验证。新主机与指纹变化需要管理员先核对指纹并更新 `known_hosts`。本地命令型 ProxyCommand 会被拒绝。

---

## 💬 会话操作

连接成功后，当前私聊或群里发起连接的管理员进入 Core Session，群里其他人的消息不受影响。直接发送文本即可执行远端命令：

```text
pwd
cd /srv/app
git status
help
停止
退出
```

| 会话输入 | 行为 |
| --- | --- |
| 普通文本 | 在当前远端目录执行 POSIX Shell 命令 |
| `cd <路径>` | 更新会话工作目录 |
| `help` | 显示会话内命令 |
| `状态`、`status` | 当前服务器、目录、环境变量数和已执行命令数 |
| `历史`、`history` | 最近 20 条命令 |
| `!!` | 重复上一条命令 |
| `停止`、`stop`、`ctrl+c` | 终止当前远端命令进程组 |
| `showimg <路径> [--page N]` | 查看远端图片，见“远程图片” |
| `退出`、`取消`、`exit`、`quit`、`q` | 关闭会话与 SSH 连接 |

会话按“用户 + 私聊或群 + 服务器”隔离，空闲期为 10 分钟。

`停止` 先向远端进程组发送 `TERM`。随后按需要发送 `KILL`，并清理本地通道。远端 PID 已确认时返回终止状态。PID 握手进行期间返回远端状态待确认提示。

目标系统需要提供 POSIX `sh` 和 `kill`。有 `setsid` 时（Linux）命令独占进程组，`停止` 整组收回。没有 `setsid` 的主机（macOS、Windows 上的 OpenSSH + Git Bash）退回普通后台进程，`停止` 直接结束该进程，它另外派生的子进程可能需要手动清理。

---

## 📌 输出预算

远端命令内容会完整交给 SSH Shell。QQ 侧只发送有界的输出，长输出归档到：

```text
data/qingssh/command_outputs/ssh-output-*.txt
```

归档完成后，QQ 里会显示归档文件名。任务被取消时，远端进程会被终止，临时归档被清理；远端状态待确认时会给出明确提示。

连接和输出上限在 `config/config.json` 的 `plugins.qingssh` 下配置，全部字段和默认值如下，都可省略：

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

| 字段 | 取值范围 | 说明 |
| --- | --- | --- |
| `command_timeout_seconds` | 0 或正数 | 命令超时秒数 |
| `max_connections` | 至少 1 | 同时活跃的 SSH 连接上限 |
| `qq_max_actions` | 2～20 | 单条命令输出最多发送的 QQ 消息数 |
| `qq_max_text_chars` | 1024～60000 | 单条命令输出发送的文字总量 |
| `qq_max_message_chars` | 256～3000 | 单条 QQ 消息的字符数 |
| `qq_head_chars` | 至少 256 | 输出超出预算时保留的开头字符数 |
| `qq_tail_chars` | 至少 128 | 输出超出预算时保留的结尾字符数 |
| `qq_send_interval_seconds` | 0～60 | 相邻消息的发送间隔 |
| `qq_send_timeout_seconds` | 大于 0，至多 300 | 单条消息的发送等待时间 |
| `archive_max_bytes` | 1 MiB～1 GiB | 单个归档文件的字节上限，达到后只保存开头和结尾 |
| `archive_tail_bytes` | 64 KiB～`archive_max_bytes` 的一半 | 归档达到上限后保留的结尾字节数 |
| `archive_retention_files` | 1～1000 | 保留的归档文件数量 |

范围之外或相互冲突的取值（例如 `qq_head_chars` 与 `qq_tail_chars` 之和超过 `qq_max_text_chars`）会被拒绝。交互会话里 `command_timeout_seconds` 为 0 表示不设超时，由管理员用 `停止` 终止长任务；agent 服务按自己传入的超时执行，缺省 30 秒。

---

## 🎨 远程图片

会话内使用：

```text
showimg plot.png
showimg *.png
showimg plot-??.jpg
showimg ./*
showimg ./plots/*.png
showimg ./* --page 2
showimg /srv/charts/*.jpg --page 3
```

路径支持 `./`、相对目录和绝对目录。目录部分使用明确路径，最后一级文件名支持 `*`、`?` 和 `[]`。例如 `./plots/*.png` 会在当前工作目录的 `plots` 子目录中匹配图片。

结果按文件名字典序分页，每页 5 张。第一页省略 `--page`，后续页使用 `--page N`，回复会给出上一页和下一页命令。分页覆盖当前目录中的全部匹配图片。每张消息依次显示全局序号、匹配总数、远端文件名和图片，每张图片上限为 10 MiB。

SFTP 下载到临时文件，经 OneBot action 接收后清理。发送异常与任务取消也会进入清理路径。

---

## ⏰ 生命周期

插件每分钟检查一次，回收已没有会话所有者的连接；小青 agent 的一次性连接不受影响。卸载、重载或 Bot 关闭时，活动命令、SSH 连接、跳板连接、SFTP 和临时归档都会被清理。

---

## 🛠 小青 agent 命令

`xiaoqing_agent` 通过 `qingssh.agent_command` 服务列出服务器摘要（名称、主机、端口、用户名、是否经跳板机，不含认证材料），或在一台服务器上执行一条命令。服务按 `/ssh` 在 plugin.json 里的声明签发给能用 `/ssh` 的发起人（`/ssh` 需要 Bot 管理员权限）。

- 每次执行新建一条独立连接，执行完立即断开，不占用管理员的 `/ssh` 会话。输出上限 32,000 字符。
- 超时或任务停止时按普通命令的终止流程收回远端进程。
- 远端命令与交互会话使用同一套包装：有 `setsid` 时独占进程组，没有时退回普通后台进程。

---

## 💾 日志与审计

日志只记录操作类型、状态、长度、计数、错误类别和不可还原的摘要，不记录远端命令、输出、密码、私钥内容、主机凭据与用户文本。

---

## 🩺 排障

| 现象 | 检查项 |
| --- | --- |
| 提示 Paramiko 缺失 | 运行 `python -m pip install -e ".[ssh]"`，随后 `/reload` |
| Host Key 校验失败 | 核对服务器指纹并维护 `~/.ssh/known_hosts` |
| 认证失败 | 核对 `auth_type`、私钥、agent 或 Core secret |
| 跳板连接失败 | 检查 `ProxyJump`、`ssh -W` 参数和跳板 Host Key |
| 命令超时 | 调整 `command_timeout_seconds`，或在会话中发送 `停止` |
| QQ 输出显示归档文件 | 到 Bot 主机的 `data/qingssh/command_outputs/` 查看对应文件 |
| Session 已结束 | 重新发送 `/ssh <服务器名>` |

---

## ✅ 开发验证

在仓库根目录运行：

```bash
python -m ruff check plugins/qingssh tests/plugins/qingssh
python -m mypy plugins/qingssh
python -m pytest -q tests/plugins/qingssh -n 2
```
