# 📓 Jupyter

`jupyter` 为 Bot 管理员提供 Python 执行、持久内核、REPL 代码缓冲和内核管理，支持私聊和群聊。同一用户在每个私聊或群里各有独立的内核。

---

## 🔐 权限与运行边界

两个命令都只有 Bot 管理员能用，支持私聊和群聊。群里的代码和输出全群可见。Python 内核继承 Bot 账户的文件、网络、CPU 和内存权限，部署时应只授予可信管理员，并用操作系统级手段（低权限账户、容器）限制资源。

安装可选依赖：

```bash
python -m pip install -e ".[jupyter]"
```

未安装依赖时插件仍会加载，帮助命令可用，执行代码时提示依赖不可用。安装后发送 `/reload` 重载插件。

---

## ⌨️ 命令

<!-- manifest-command-aliases:start -->
| 功能 | 推荐入口 | Manifest 等价别名 |
| --- | --- | --- |
| 执行 Python 或启动 REPL | `/jupyter` | `/py` `/python` |
| 管理当前用户内核 | `/kernel` | `/内核` |
<!-- manifest-command-aliases:end -->

```text
/py print("hello")
/py -t 60 print("bounded")
/py --timeout=30 print("bounded")
/py repl
/py help
/kernel status
/kernel start
/kernel restart
/kernel shutdown
/kernel help
```

执行时限选项位于代码开头，接受 `-t <秒>`、`--timeout <秒>` 和 `--timeout=<秒>`，默认值和范围见“内核与输出预算”。后续文本逐字作为 Python 代码。

子命令别名：

- `/py repl`：`interactive`、`交互`。
- `/py help` 和 `/kernel help`：`帮助`、`?`；`/kernel help` 另接受 `-h`。
- `/kernel status`、`start`、`restart`、`shutdown`：`状态`、`启动`、`重启`、`关闭`；`shutdown` 另接受 `stop`、`停止`。

---

## 💬 REPL 会话

使用 `/py repl` 进入代码缓冲会话，之后发送的每段文本追加到缓冲区：

| 输入 | 作用 |
| --- | --- |
| `run`、`执行`、`运行` | 执行完整缓冲区；成功后清空 |
| `show`、`显示`、`buffer`、`缓冲区` | 查看缓冲区预览 |
| `clear`、`清空`、`reset` | 清空缓冲区 |
| `help`、`帮助`、`?` | 显示 REPL 帮助 |
| `退出`、`取消`、`exit`、`quit`、`q` | 结束 REPL 会话 |

缓冲区和完整代码的上限见“内核与输出预算”。执行异常时缓冲区保持原值，便于修改后再次运行。其他插件拥有活动会话时，`/py repl` 会提示先发送 `退出` 结束该会话。

---

## 📌 内核与输出预算

| 项目 | 当前预算 |
| --- | ---: |
| 单次代码 | 16000 字符、32 KiB |
| REPL 缓冲区 | 64 行，每行 3000 字符 |
| 执行时限 | 默认 30 秒，范围 0.1～600 秒 |
| 文本输出 | 64 KiB 收集预算、2000 字符展示预算 |
| 图片数量 | 最多 5 张 |
| 单张图片 | 5 MiB、2000 万像素 |
| 图片合计 | 10 MiB |
| 内核实例 | 全局最多 64 个 |
| 内核空闲期 | 约 5 分钟 |
| REPL 空闲期 | 10 分钟 |

执行超时或输出达到预算时，代码被中断，内核自动恢复。图片仅支持 PNG，经过大小、格式、像素和累计容量校验后发送。

---

## 🔐 审计与隐私

日志只记录操作类型、状态、代码长度和不可还原的指纹，不记录代码正文、REPL 缓冲区、用户输入和异常正文。

---

## ⏰ 生命周期

内核按需创建，空闲约 5 分钟后自动回收。卸载、重载或 Bot 关闭时，全部内核都会被回收。REPL 会话与内核各自计时，互不影响。

---

## 🩺 排障

| 现象 | 检查项 |
| --- | --- |
| 提示 Jupyter 依赖不可用 | 运行 `python -m pip install -e ".[jupyter]"`，随后 `/reload` |
| 内核启动失败 | 检查 Python 环境、`ipykernel`、进程权限和启动日志 |
| 执行超时 | 调整 `-t`，检查代码循环和外部 I/O |
| 输出达到预算 | 缩小打印结果、图片数量或图片尺寸 |
| 内核状态异常 | 使用 `/kernel restart` 创建干净内核 |
| REPL 提示其他会话 | 发送 `退出` 结束当前 Core Session |

---

## ✅ 开发验证

在仓库根目录运行：

```bash
python -m pytest -q tests/plugins/jupyter/test_jupyter.py \
  tests/plugins/jupyter/test_jupyter_contracts.py \
  tests/plugins/jupyter/test_jupyter_lifecycle.py \
  tests/plugins/jupyter/test_jupyter_manager_contracts.py \
  tests/plugins/contracts/test_shell_jupyter_log_privacy.py
python -m ruff check plugins/jupyter
python -m mypy plugins/jupyter
```
