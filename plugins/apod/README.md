# 🌌 APOD 每日天文图

APOD 插件抓取 NASA Astronomy Picture of the Day 当前页面，返回图片或视频链接、标题和说明。

---

## 🔐 使用条件

- 命令支持群聊与私聊。
- 运行依赖为 Beautiful Soup 与 Pillow。
- 网络需要访问 `apod.nasa.gov` 及页面引用的媒体地址。

---

## ⌨️ 命令

| 命令 | 功能 |
|---|---|
| `/apod` | 获取当前 APOD |
| `/apod help` | 插件帮助 |

`/apod` 使用空参数调用，目标固定为 NASA 当前 APOD 页面 `apod.nasa.gov/apod/astropix.html`。

<!-- manifest-command-aliases:start -->
| 功能 | 推荐入口 | Manifest 等价别名 |
|---|---|---|
| 获取当前 APOD | `/apod` | `/每日一天文图` |
<!-- manifest-command-aliases:end -->

完整参数与错误样例可通过 `/help apod` 查看。

---

## ⚙️ 配置与调度

可选公开配置：

```json
{
  "plugins": {
    "apod": {
      "url": "https://apod.nasa.gov/apod/astropix.html",
      "allowed_hosts": ["apod.nasa.gov"]
    }
  }
}
```

每日 13:30 任务采用 Core `broadcast` 模式，将结果发送到该 schedule 的 `group_ids`；字段省略时使用 `default_group_ids`。生产调度请配置至少一个目标群。

---

## 🔐 数据与网络边界

缓存位于 `data/apod/`。页面与媒体请求使用 HTTPS，重定向会重新校验 DNS 与 `allowed_hosts`。`apod.nasa.gov` 可使用 Clash 透明代理的保留 fake-IP 地址，请求使用安全校验阶段确定的解析地址并验证 TLS 主机名；全局安全层拒绝其余非公网地址。HTML、图片字节、MIME、尺寸和像素均使用有界校验，缓存名根据最终 URL 的 SHA-256 生成。

---

## 🩺 排障

1. 使用 `/apod` 验证页面抓取。
2. 检查 `allowed_hosts` 与最终媒体主机。
3. 检查日志中的 DNS、安全策略、HTTP 状态、重定向、MIME 和图片解码结果。
4. 检查 `data/apod/` 写入权限与缓存容量。

---

## ✅ 开发验证

```bash
python -m pytest tests/plugins/apod/test_apod.py -q
python -m ruff check plugins/apod tests/plugins/apod/test_apod.py
```
