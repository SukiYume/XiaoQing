# 🐦 Twitter 图片

`twitter` 从指定 X/Twitter 账号抓取图片到有界本地缓存，并随机发送当前轮次尚待发送的一张图片。

---

## ⌨️ 命令与调度

| 用法 | 权限 | 行为 |
| --- | --- | --- |
| `/twimg`、`/twitter`、`/推特` | 全部用户 | 从本地缓存随机发送一张图片 |
| `/tw_fetch`、`/抓取推特` | Bot 管理员 | 提交后台抓取，并在完成后私聊通知结果 |

两个命令都支持 `help`、`帮助` 和 `?`。Manifest 每天 03:00 以 Core `silent` 模式启动一次后台抓取，任务只更新缓存。

手动抓取与定时抓取共享同一个后台任务。并发提交会复用当前任务，将分页和下载合并为一次执行。

插件按账号维护首次全量完成标记。新账号首次抓取会遍历允许的全部分页；全量完成后进入增量模式，连续两页没有新增图片时结束增量抓取。

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
| `headers` | 有界字符串键值映射，用于 GraphQL API |
| `cookies` | 有界字符串键值映射，用于 GraphQL API |
| `proxy` | 可选 HTTP 或 HTTPS 代理 URL，同时用于 GraphQL API 与媒体下载 |
| `max_pages` | 1～50 的整数，默认 50 |

API 请求使用配置的认证头、Cookie 和代理。媒体下载使用同一代理和独立的最小请求头，并限制为受信 Twitter 媒体域名、HTTPS 协议和有界响应。真实认证信息保存在 secret 配置与普通日志边界之外。

---

## 🔄 抓取流程

1. 请求 `x.com` 用户时间线 GraphQL 接口；
2. 从有界 JSON 中提取图片 URL 与分页游标；
3. 把媒体 URL 规范化为原图地址；
4. 通过已配置的代理最多并发下载 4 张图片；
5. 校验图片并按内容 SHA-256 写入缓存；
6. 首次全量抓取遍历到时间线结束或 `max_pages` 上限，并记录当前 `user_id` 的完成标记；
7. 后续增量抓取连续两页没有新增图片时结束。

X/Twitter GraphQL 接口与认证字段可能随站点更新而变化，维护者需要同步更新请求特性和测试夹具。

---

## 🔐 网络与图片边界

| 项目 | 当前预算 |
| --- | ---: |
| API 响应 | 5 MiB，并限制 JSON 深度、节点和字符串 |
| 允许媒体域名 | `pbs.twimg.com`、`ton.twitter.com`、`video.twimg.com` |
| 媒体协议 | HTTPS |
| 单张图片 | 10 MiB、4000 万像素、120 帧 |
| 图片格式 | JPEG、PNG、WebP |
| 首次全量范围 | 时间线结束或最多 50 页 |
| 增量停止条件 | 连续 2 页没有新增图片 |
| 并发下载 | 4 张 |
| 缓存 | 5000 项、2 GiB、90 天 |

---

## 💾 发送轮次与数据

运行数据位于：

```text
data/twitter/images/
data/twitter/posted.txt
data/twitter/backfill_complete.json
```

`images/` 保存按内容身份命名的图片。`posted.txt` 保存当前轮次已确认发送的文件名，读取上限为 1 MiB。发送前插件会预留候选图片，OneBot 确认后提交文件名；发送异常会释放预留。全部有效图片完成一轮后，状态自动开始新轮次。

`backfill_complete.json` 是按 `user_id` 绑定的小型首次全量标记。标记缺失、损坏或账号变化时，下一轮自动重新执行全量回填；完整遍历且媒体下载没有失败后才会更新标记。

GraphQL `errors` 和缺失或畸形的时间线结构会进入抓取失败路径，保留既有回填状态。结构完整的空时间线可作为正常结束信号。

缓存读取会再次校验图片格式与资源预算，并清理状态中的陈旧文件名。

---

## ⏰ 生命周期

后台抓取任务和手动通知任务保存在当前插件代。卸载、重载或 Bot 关闭时，`shutdown()` 取消并收敛这些任务。图片与发送轮次状态保留在数据目录。

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
python -m ruff check plugins/twitter tests/plugins/twitter/test_twitter.py
python -m mypy plugins/twitter
python -m pytest -q tests/plugins/twitter/test_twitter.py \
  tests/plugins/contracts/test_twitter_voice_resource_bounds.py -n 2
```
