# 🐦 Twitter 图片

`twitter` 从指定 X/Twitter 账号抓取图片到有界本地缓存，并随机发送当前轮次尚待发送的一张图片。

需要 Pillow（属于项目的 `plugins` 可选依赖）：

```bash
python -m pip install -e ".[plugins]"
```

Pillow 缺失时插件不会加载。安装后发送 `/reload` 重载插件。

---

## ⌨️ 命令与调度

| 用法 | 权限 | 行为 |
| --- | --- | --- |
| `/twimg`、`/twitter`、`/推特` | 全部用户 | 从本地缓存随机发送一张图片 |
| `/tw_fetch`、`/抓取推特` | Bot 管理员 | 提交后台抓取，并在完成后向发起会话通知结果 |

两个命令都支持 `help`、`帮助` 和 `?`。每天 03:00 自动抓取一次，时间按 `config.timezone`（默认 `Asia/Shanghai`）计，只更新缓存，不发消息。

手动抓取与定时抓取共享同一个后台任务。并发提交会复用当前任务，将分页和下载合并为一次执行。

抓取分首次全量和增量两种模式，规则见“抓取流程”。

---

## ⚙️ 凭据配置

在 `config/secrets.json` 中配置：

```json
{
  "plugins": {
    "twitter": {
      "user_id": "Twitter用户ID",
      "headers": {
        "authorization": "Bearer <TWITTER_BEARER_TOKEN>"
      },
      "cookies": {},
      "proxy": "http://proxy.example.com:8080",
      "max_pages": 50
    }
  }
}
```

| 字段 | 规则 |
| --- | --- |
| `user_id` | 必填；目标账号的非空字符串或正整数 ID |
| `headers` | 字符串键值映射，访问 GraphQL API 的请求头（含 `authorization`），需要自行取得 |
| `cookies` | 字符串键值映射，访问 GraphQL API 的 Cookie，需要自行取得 |
| `proxy` | 可选 HTTP 或 HTTPS 代理 URL，同时用于 GraphQL API 与媒体下载 |
| `max_pages` | 1～50 的整数，默认 50 |

`headers` 和 `cookies` 在代码里不强制必填，认证信息无效时抓取会失败。API 请求使用配置的认证头、Cookie 和代理。媒体下载使用同一代理和独立的最小请求头，只允许受信的 Twitter 媒体域名和 HTTPS。认证信息只存放在 `config/secrets.json`，不会写入日志。

---

## 🔄 抓取流程

1. 请求 `x.com` 用户时间线 GraphQL 接口。
2. 从有界 JSON 中提取图片 URL 与分页游标。
3. 把媒体 URL 规范化为 `name=large` 大图地址。
4. 最多并发下载 4 张图片：配置了 `proxy` 时经代理下载，未配置时经 Core 的公共安全下载。
5. 校验图片并按内容 SHA-256 写入缓存。
6. 首次全量抓取遍历到时间线结束或 `max_pages` 上限，并记录当前 `user_id` 的完成标记。
7. 后续增量抓取连续两页没有新增图片时结束。

X/Twitter 的 GraphQL 接口与认证要求可能随站点更新而变化，届时抓取会失败，见“排障”。

---

## 🔐 网络与图片边界

| 项目 | 当前预算 |
| --- | ---: |
| API 响应 | 5 MiB，并限制 JSON 深度、节点和字符串 |
| 允许媒体域名 | `pbs.twimg.com`、`ton.twitter.com`、`video.twimg.com` |
| 媒体协议 | HTTPS |
| 单张图片 | 10 MiB、4000 万像素、120 帧 |
| 图片格式 | JPEG、PNG、WebP |
| 缓存 | 5000 项、2 GiB、90 天 |

---

## 💾 发送轮次与数据

运行数据位于：

```text
data/twitter/images/
data/twitter/posted.txt
data/twitter/backfill_complete.json
```

`images/` 保存按内容身份命名的图片。`posted.txt` 保存当前轮次已确认发送的文件名，读取上限为 1 MiB。发送前插件会预留候选图片，OneBot 确认后提交文件名。发送异常会释放预留。全部有效图片完成一轮后，状态自动开始新轮次。

`backfill_complete.json` 是按 `user_id` 绑定的小型首次全量标记。标记缺失、损坏或账号变化时，下一轮自动重新执行全量回填。完整遍历且媒体下载没有失败后才会更新标记。

GraphQL `errors` 和缺失或畸形的时间线结构会进入抓取失败路径，保留既有回填状态。结构完整的空时间线可作为正常结束信号。

缓存读取会再次校验图片格式与资源预算，并清理状态中的陈旧文件名。

---

## ⏰ 生命周期

卸载、重载或 Bot 关闭时，进行中的后台抓取和完成通知会被取消。图片与发送轮次状态保留在数据目录。

---

## 🩺 排障

| 现象 | 检查项 |
| --- | --- |
| `/twimg` 提示缓存为空 | 由管理员执行 `/tw_fetch`，等待完成通知后再次取图 |
| GraphQL 认证失败 | 更新 Bearer、Cookie、目标用户 ID 和请求特性 |
| 图片下载全部失败 | 核对代理 URL、代理可用性和 Twitter 媒体域名连通性 |
| 每次都执行首次全量抓取 | 检查 `backfill_complete.json` 是否可写，以及完成日志中是否存在媒体下载失败 |
| 图片被资源边界拒绝 | 检查媒体域名、格式、字节、像素和帧数 |
| 定时缓存长期无更新 | 检查 03:00 调度日志和站点接口变化 |

---

## ✅ 开发验证

在仓库根目录运行：

```bash
python -m ruff check plugins/twitter tests/plugins/twitter
python -m mypy plugins/twitter
python -m pytest -q tests/plugins/twitter \
  tests/plugins/contracts/test_twitter_voice_resource_bounds.py -n 2
```
