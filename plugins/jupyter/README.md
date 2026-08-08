# 📓 Jupyter

`jupyter` 为 Bot 管理员提供私聊 Python 执行、持久内核、REPL 代码缓冲和内核管理。每个私聊用户拥有独立的内核所有者键。

---

## 🔐 权限与运行边界

Manifest 将两个命令标记为 `admin_only: true`，并将上下文限定为私聊。Python 内核继承 Bot 账户的文件、网络、CPU 和内存权限；生产部署应使用可信管理员和操作系统级资源隔离。

安装可选依赖：

```bash
pip install "xiaoqing[jupyter]"
```

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
```

执行时限选项位于代码开头，接受 `-t <秒>`、`--timeout <秒>` 和 `--timeout=<秒>`，范围为 0.1～600 秒。后续文本逐字作为 Python 代码。

---

## 💬 REPL 会话

使用 `/py repl` 进入 10 分钟代码缓冲会话：

| 输入 | 作用 |
| --- | --- |
| `run`、`执行` | 执行完整缓冲区；成功后清空 |
| `show`、`显示` | 查看缓冲区的有界预览 |
| `clear`、`清空` | 清空缓冲区 |
| `help`、`帮助` | 显示 REPL 帮助 |
| `退出`、`取消`、`exit`、`quit`、`q` | 结束 Core Session |

缓冲区最多 64 行，每行最多 3000 个字符；完整代码同时受 16000 字符和 32 KiB 预算保护。执行异常时缓冲区保持原值，便于修改后再次运行。其他插件拥有活动 Session 时，入口会提示先结束该会话。

---

## 📌 内核与输出预算

| 项目 | 当前预算 |
| --- | ---: |
| 单次代码 | 16000 字符、32 KiB |
| 执行时限 | 默认 30 秒，范围 0.1～600 秒 |
| 文本输出 | 64 KiB 收集预算、2000 字符展示预算 |
| 图片数量 | 最多 5 张 |
| 单张图片 | 5 MiB、2000 万像素 |
| 图片合计 | 10 MiB |
| 内核实例 | 全局最多 64 个 |
| 内核空闲期 | 约 5 分钟 |
| REPL 空闲期 | 10 分钟 |

内核通过 ready 检查后进入可用状态。执行超时或输出达到预算时，管理器中断代码并执行恢复流程。PNG 结果经过字节、格式、像素与累计容量校验后以内存消息段发送。

---

## 🔐 审计与隐私

敏感审计记录操作类型、状态、代码长度和单向指纹。代码正文、REPL 缓冲区、用户输入和异常正文保留在普通日志边界之外。用户身份校验异常时，内核所有者创建会终止于入口边界。

---

## ⏰ 生命周期

`init()` 探测 `jupyter_client` 和 `ipykernel`。内核按需创建，空闲监视任务负责回收。卸载、重载或 Bot 关闭时，`shutdown()` 先停止监视任务，再并行回收活动和隔离中的内核。REPL Session 与内核进程拥有独立生命周期。

---

## 🩺 排障

| 现象 | 检查项 |
| --- | --- |
| 提示 Jupyter 依赖缺失 | 安装 `xiaoqing[jupyter]`，随后重载插件 |
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
