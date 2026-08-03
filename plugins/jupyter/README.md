# Jupyter 插件

仅供 Bot 管理员在私聊中使用的 Python 代码执行插件，提供按用户隔离的持久内核、单次执行、REPL 代码缓冲和内核管理。

## 命令

<!-- manifest-command-aliases:start -->
| 命令别名 | 功能 |
| --- | --- |
| `/jupyter`、`/py`、`/python` | 执行 Python 代码或启动 REPL |
| `/kernel`、`/内核` | 管理当前隔离内核 |
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

timeout 选项只在代码最前面解析一次，必须是 0.1–600 秒的 ASCII 数值；超过范围、缺值或畸形格式会被拒绝。之后的内容逐字作为 Python 代码，代码中的 `-t` 不会被误删。单次代码最多 16,000 字且不超过 32 KiB。

## REPL 会话

进入 `/py repl` 后，可以逐行或一次发送多行代码：

- `run` / `执行`：执行完整缓冲区；成功后清空，失败时保留以便修改；
- `show` / `显示`：查看有界预览，预览截断不会改变实际缓冲区；
- `clear` / `清空`：清空缓冲区；
- `help` / `帮助`：查看 REPL 帮助；
- `退出` / `取消` / `exit` / `quit` / `q`：由框架统一结束会话。

缓冲区最多 64 行，每行最多 3,000 字，总计仍受 16,000 字/32 KiB 预算。REPL 会话 10 分钟无操作后过期。命令检测到其他插件会话时会提示先退出，不读取或主动删除其状态。

## 内核与输出边界

- 缺失或异常用户身份不会回退到共享全局内核；每名私聊用户使用独立所有者键。
- 内核 ready 后才发布；启动失败会停止 channels、关闭/强杀 kernel 并清理资源，无法确认退出的实例会被隔离。
- 执行超时或输出超过 64 KiB 时会中断代码并等待恢复；所有内部调用也受 600 秒硬上限。
- 文本显示最多 2,000 字；每张 PNG 最多 5 MiB、2,000 万像素，单次图片合计最多 10 MiB。
- 图片以验证后的内存字节发送，不再创建逐次执行文件。内核空闲约 5 分钟后自动关闭；REPL 会话与内核寿命彼此独立。
- 原始代码、缓冲区、异常正文和用户输入不会写入普通日志；审计只记录类型、长度和不可逆指纹。

## 依赖与风险

安装项目声明的可选依赖：

```bash
pip install "xiaoqing[jupyter]"
```

该插件能够在宿主机执行管理员提交的 Python 代码。不要向非受信用户开放；进程级 CPU、内存和文件权限仍由部署环境负责隔离。
