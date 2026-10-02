# 🌌 APOD 每日天文图

APOD 插件抓取 NASA Astronomy Picture of the Day 当前页面，返回图片或视频链接、标题、说明、日期、署名和原页面链接。

---

## 🔐 使用条件

- 命令支持群聊与私聊。
- 运行依赖为 Beautiful Soup 与 Pillow。
- 网络需要访问 `science.nasa.gov` 和图片主机 `assets.science.nasa.gov`。插件同时支持旧页面主机 `apod.nasa.gov`。

---

## ⌨️ 命令

| 命令 | 功能 |
|---|---|
| `/apod` | 获取当前 APOD |
| `/apod help` | 插件帮助 |

`/apod` 使用空参数调用，默认读取 NASA 当前 APOD 页面 `https://science.nasa.gov/apod/`。

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
      "url": "https://science.nasa.gov/apod/",
      "allowed_hosts": []
    }
  }
}
```

每日 13:30 任务采用 Core `broadcast` 模式，将结果发送到该 schedule 的 `group_ids`；字段省略时使用 `default_group_ids`。生产调度请配置至少一个目标群。

`url` 和 `allowed_hosts` 均可省略。内置主机白名单包含 `science.nasa.gov`、`assets.science.nasa.gov` 和 `apod.nasa.gov`，`allowed_hosts` 用于增加其他可信主机。配置中的旧入口 `https://apod.nasa.gov/apod/astropix.html` 会自动切换到新首页。管理员指定的其他页面继续使用原配置地址，包括新版历史 `image-article` 页面。

## 页面解析

新版页面使用 `.hds-media-detail-hero` 作为 APOD 主体。插件从主体内的 `h1` 或 `h2` 提取作品标题，从 `.media-detail-hero__description` 提取说明，从 `.media-detail-hero__meta-table` 提取日期和署名。正文会截断投稿公告、迁移公告和次日预告。

媒体提取限定在 `.media-detail-hero__media` 内，支持图片、iframe 和 video；视频优先展示实际媒体链接。响应式图片优先选取 `srcset` 中宽度不超过 1600 像素的最大候选，随后使用 `src` 或 `data-src`。所有下载候选继续通过主机白名单检查。旧页面继续支持 `center` 标题和 Explanation 段落。

---

## 🔐 数据与网络边界

缓存位于 `data/apod/images/`。页面与媒体请求使用 HTTPS，重定向会重新校验 DNS 与 `allowed_hosts`。上述三个 NASA 内置主机可使用 Clash 透明代理的保留 fake-IP 地址，请求使用安全校验阶段确定的解析地址并验证 TLS 主机名；全局安全层拒绝其余非公网地址。管理员增加的主机使用公网 DNS 校验。HTML、图片字节、MIME、尺寸和像素均使用有界校验，缓存名根据最终 URL 的 SHA-256 生成。

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
