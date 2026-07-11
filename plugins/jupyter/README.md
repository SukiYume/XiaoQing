# Jupyter 插件

通过持久 Jupyter 内核执行 Python 代码，支持单次执行、交互式 REPL 和内核管理。

## 常用命令

```text
/py <代码>
/py -t 60 <代码>
/py --timeout 30 <代码>
/py repl
/kernel status
/kernel restart
/kernel shutdown
```

## 主要行为

- 代码在持久内核中执行，变量会在重启前保留。
- 内核按“用户 + 群”隔离；同一用户跨群不会共享变量。
- 执行超时会主动中断当前代码，避免超时后继续污染内核状态。
- 空闲约 10 分钟后会自动关闭内核。
- 内核启动采用“ready 后才提交”语义；构造 client、启动 channels 或 ready 检查失败时会停止 channels、关闭/强杀 kernel 并清理资源。无法确认孤儿进程退出的实例会被隔离，不会被后续请求复用。
- `matplotlib` 等图像输出会自动转换为图片消息。

## REPL 模式

```text
/py repl
```

进入后可连续输入多行代码：

- `run` 执行当前缓冲区
- `show` 查看缓冲区
- `clear` 清空缓冲区
- `退出` / `取消` 结束 REPL

## 依赖

需要本地安装 Jupyter 相关依赖，例如：

```bash
pip install jupyter jupyter_client ipykernel matplotlib
```

## 注意事项

- 仅管理员可用。
- 这是代码执行插件，不建议在不受控环境中开放给普通用户。
- 长时间运行或高资源占用代码仍可能影响宿主机资源。
