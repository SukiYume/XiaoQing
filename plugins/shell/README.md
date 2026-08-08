# 💻 Shell

`shell` 让 Bot 管理员通过 QQ 私聊在 Bot 主机执行单条本地命令。每次调用创建独立进程，工作目录和环境状态按调用重新建立。

---

## 🔐 权限与主机边界

Manifest 将命令标记为 `admin_only: true`，并将场景限定为私聊。子进程继承 Bot 账户的文件、网络和进程权限。命令启用列表用于降低误触概率；操作系统低权限账户、容器或专用执行服务负责安全隔离。

入站鉴权、Bot 管理员列表、私聊场景、终端配置和启用列表共同构成部署边界。生产环境建议采用专用低权限 Bot 账户和最小 `replace` 列表。

---

## ⌨️ 命令

```text
/shell <命令>
/shell list
/shell help
```

Manifest 等价入口为 `/shell`、`/sh` 和 `/exec`。`list` 显示当前启用入口及其在所选终端中的可用状态。

Git Bash 示例：

```text
/shell pwd
/shell ls -la
/shell git status --short
/shell python --version
```

解释器内建命令需要显式解释器，例如 Windows direct 后端可使用 `/shell cmd /c dir`。启用通用解释器会同时授予该解释器可表达的主机能力。

---

## ⚙️ 终端配置

终端选项位于 `config/config.json` 的 `plugins.shell.terminal`。

### Git Bash

```json
{
  "plugins": {
    "shell": {
      "terminal": {
        "backend": "git-bash",
        "executable": "C:/Program Files/Git/bin/bash.exe"
      }
    }
  }
}
```

Git Bash 使用 `--noprofile --norc -c` 启动。部署者在配置中填写实际可执行文件路径。

### Direct

```json
{
  "plugins": {
    "shell": {
      "terminal": {
        "backend": "direct"
      }
    }
  }
}
```

Direct 后端将首个 token 解析为 Bot 进程 PATH 中的外部程序或明确路径，并通过 `create_subprocess_exec` 启动。

---

## ⌨️ 命令启用与超时

敏感设置位于 `config/secrets.json`：

```json
{
  "plugins": {
    "shell": {
      "whitelist": ["ls", "pwd", "git", "python"],
      "whitelist_mode": "replace",
      "timeout": 30,
      "disable_whitelist": false
    }
  }
}
```

| 字段 | 规则 |
| --- | --- |
| `whitelist` | 命令首入口字符串列表 |
| `whitelist_mode=replace` | 使用自定义列表作为完整启用集合 |
| `whitelist_mode=extend` | 将自定义项加入项目默认集合 |
| `timeout` | 有限正数秒，默认 30 |
| `disable_whitelist` | JSON 布尔值 `true` 开启全部首入口 |

空的 `replace` 列表会关闭全部命令入口。配置项按严格类型解析。

---

## 🔐 执行边界

- 命令链接、管道、命令替换、多行输入、追加重定向和预定义高风险模式会在启动前被拒绝；
- 原始命令先经过模式检查和首入口检查，再交给所选终端；
- 标准输入连接空设备，适合单次非交互命令；
- 子进程在独立进程组中启动；
- 超时、任务取消和输出溢出会触发整棵子进程树回收；
- stdout 与 stderr 共享至少 64 KiB 原始捕获预算；
- QQ 回复正文共享 4000 字符首尾预算；
- 进程返回码、stdout 和 stderr 分区展示。

已启用解释器和程序可按自身能力访问文件、网络和子进程。管理员应把启用列表与主机权限一起审查。

---

## 🔄 路径规则

QQ 输入建议使用 `/`：

- Windows：`C:/workspace/a.txt`；
- Linux 和 macOS：`/home/user/a.txt`、`~/a.txt`、`./file`；
- `key=value` 中的路径值参与规范化；
- URL 保持原文；
- Windows 选项 `/c`、`/Y` 保持参数语义；
- 含空格路径使用引号。

---

## 💾 审计

敏感审计记录请求 ID、状态、返回码、命令长度和进程内单向指纹。原始命令、stdout、stderr 和配置 secret 保留在普通日志边界之外。

---

## 🩺 排障

| 现象 | 检查项 |
| --- | --- |
| 终端配置异常 | 核对 `backend` 和 Git Bash `executable` |
| 命令入口被拒绝 | 核对 `whitelist` 与 `whitelist_mode` |
| 程序解析失败 | 在所选终端中检查 PATH，或使用明确程序路径 |
| Shell 内建命令提示 | 显式调用对应解释器 |
| 命令超时 | 调整 `timeout`，或改用后台服务管理长任务 |
| 输出达到预算 | 缩小命令输出或将结果写入受控文件后再查询摘要 |

---

## ✅ 开发验证

在仓库根目录运行：

```bash
python -m ruff check plugins/shell tests/plugins/test_shell_plugin.py
python -m mypy plugins/shell
python -m pytest -q tests/plugins/test_shell_plugin.py \
  tests/plugins/test_shell_jupyter_log_privacy.py -n 2
```
