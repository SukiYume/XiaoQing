# 🔊 Echo

`echo` 提供有界文本回显与用户问候，也是命令分发、消息段返回和公开错误边界的最小示例插件。

---

## ⌨️ 命令

<!-- manifest-command-aliases:start -->
| 功能 | 推荐入口 | Manifest 等价别名 |
| --- | --- | --- |
| 文本回显 | `/echo` | `/回显` |
| 用户问候 | `/hello` | `/你好` |
<!-- manifest-command-aliases:end -->

```text
/echo 你好世界
/回显 支持中文别名
/hello
/你好
```

---

## 🔐 行为边界

- `/echo` 去除文本首尾空白后回显；空文本显示本地帮助；
- 回显文本长度上限为 Core 的单条 QQ 文本上限，当前为 3000 个字符；
- 换行和制表符可保留，其余 C0/C1 控制字符会触发参数错误；
- `/hello` 接受空参数，并显示经过正整数校验的当前 QQ 号；
- 用户 ID 缺失或格式异常时显示“未知用户”；
- 日志记录已接受文本的字符数，正文保留在日志边界之外。

插件运行配置、凭据、网络请求、数据文件和定时任务均为空集。

---

## 🛠️ 开发参考

`main.py` 只发布 `handle()` 入口。帮助文本、控制字符校验和 QQ 号规范化分别保持独立职责，可作为简单插件的消息返回与公开错误处理参考。

---

## ✅ 开发验证

在仓库根目录运行：

```bash
python -m pytest -q tests/plugins/test_echo.py
python -m ruff check plugins/echo
python -m mypy plugins/echo
```
