# 🔐 QingSSH

QingSSH 为 Bot 管理员提供私聊 SSH 远程控制，支持保存服务器、导入 `~/.ssh/config`、持久会话、流式命令输出、远端命令终止和图片查看。

---

## 🔐 权限与依赖

全部 Manifest 入口均为管理员私聊命令。安装 SSH 后端：

```bash
pip install paramiko
```

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

规范入口集中在 `/ssh`：

```text
/ssh help
/ssh list
/ssh add [名称 主机 [端口] [用户名]]
/ssh remove <服务器名>
/ssh import [Host名|all]
/ssh config
/ssh status
/ssh disconnect [服务器名]
/ssh <服务器名>
/ssh <用户名>@<服务器名>
```

独立入口与相应 `/ssh` 子命令共享相同权限、参数和连接策略。

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

`auth_type` 接受 `agent`、`key` 和 `password`。密码通过管理员私聊的 `/ssh add` 引导输入，由 Core 密钥存储保存；`servers.json` 记录插件生成的 `password_ref`。配置写入和 secret 写入采用补偿式事务，任一步异常都会收敛已创建资源。

主机名、端口、用户名、服务器别名和路径均执行长度、字符与范围校验。

---

## 📅 SSH config 导入

`/ssh config` 列出 `~/.ssh/config` 中具有明确 Host 名的条目。`/ssh import <Host>` 和 `/ssh import all` 将可用条目复制到 `servers.json`。

支持的跳板形式：

- 单跳 `ProxyJump`；
- 结构可验证的 `ssh -W` ProxyCommand。

Host Key 始终通过 `~/.ssh/known_hosts` 严格验证。新主机与指纹变化需要管理员先核对指纹并更新 `known_hosts`。本地命令型 ProxyCommand 会在配置解析边界被拒绝。

---

## 💬 会话操作

连接成功后，私聊进入 Core Session。直接发送文本即可执行远端命令：

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
| `停止` | 终止当前远端命令进程组 |
| `退出`、`取消`、`exit`、`quit`、`q` | 关闭会话与 SSH 连接 |

会话按“私聊用户 + 服务器”隔离，空闲期为 10 分钟。每个后台命令具有唯一 `job_id`，会话事务提交后才启动 SSH 通道。任务完成时按 `job_id` 原子更新状态。

`停止` 先向远端进程组发送 `TERM`，随后按需要发送 `KILL`，并清理本地通道。远端 PID 已确认时返回终止状态；PID 握手进行期间返回远端状态待确认提示。

目标系统需要提供 POSIX `sh`、`setsid` 和 `kill`。

---

## 📌 输出预算

远端命令内容会完整交给 SSH Shell。QQ 侧采用有界投影，长输出归档到：

```text
data/qingssh/command_outputs/ssh-output-*.txt
```

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

`command_timeout_seconds=0` 表示由管理员主动终止长任务。QQ 预算控制 action 数、累计文字、单条消息、首尾保留、发送频率与等待时间。归档预算控制单文件字节、尾部保留和文件数量；达到归档预算时保存受控首尾。

任务取消会先收敛远端进程，再清理临时归档。完成的长输出归档只向 QQ 展示文件名，完整本机路径进入内部日志。

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

路径支持 `./`、相对目录和绝对目录。目录部分使用明确路径，最后一级文件名支持 `*`、`?` 和 `[]`；例如 `./plots/*.png` 会在当前工作目录的 `plots` 子目录中匹配图片。

结果按文件名字典序分页，每页 5 张。第一页省略 `--page`，后续页使用 `--page N`，回复会给出上一页和下一页命令；分页覆盖当前目录中的全部匹配图片。每张消息依次显示全局序号、匹配总数、远端文件名和图片，每张图片上限为 10 MiB。

SFTP 下载到临时文件，经 OneBot action 接收后清理；发送异常与任务取消也会进入清理路径。

---

## ⏰ 生命周期

插件首次使用时创建 `SSHManager`。每分钟的 `cleanup_orphans` 以 Core `silent` 模式对比 Core Session 与活动连接，回收失去会话所有者的连接。卸载、重载或 Bot 关闭时，`shutdown()` 收敛活动命令、SSH 连接、跳板连接、SFTP 和临时归档。

---

## 💾 日志与审计

普通日志记录操作类型、状态、长度、计数、错误类别和单向摘要。远端命令、输出、密码、私钥内容、主机凭据与用户文本保留在日志边界之外。

---

## 🩺 排障

| 现象 | 检查项 |
| --- | --- |
| 提示 Paramiko 缺失 | 安装 `paramiko`，随后重载插件 |
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
python -m ruff check plugins/qingssh tests/plugins/qingssh/test_qingssh*.py
python -m mypy plugins/qingssh
python -m pytest -q tests/plugins -k qingssh -n 2
```
