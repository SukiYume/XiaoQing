# 🌍 Earthquake

`earthquake` 从[中国地震台网速报微博](https://www.weibo.com/ceic)读取近期地震快讯。手动命令查询最新记录，定时任务向 Core 为该 schedule 解析出的目标群投递 4.0 级及以上的新快讯。

---

## ⌨️ 命令

<!-- manifest-command-aliases:start -->
| 功能 | 推荐入口 | Manifest 等价别名 |
| --- | --- | --- |
| 地震快讯 | `/earthquake` | `/地震` |
<!-- manifest-command-aliases:end -->

| 用法 | 说明 |
| --- | --- |
| `/earthquake` | 查询最近一条有效快讯 |
| `/earthquake latest` | 查询最近一条有效快讯 |
| `/earthquake help` | 显示本地帮助 |

`最新` 是 `latest` 的中文别名；`h` 和 `帮助` 是 `help` 的别名。手动查询保持定时任务游标原值。

---

## ⏰ 定时投递

Manifest 每 5 分钟以 Core `targeted` 模式运行一次 `scheduled`：

1. 优先续发 `earthquake_delivery.json` 中的待办事件；
2. 拉取并校验微博卡片；
3. 扫描新事件，并筛选 4.0 级及以上记录用于投递；
4. 按定时任务目标群逐个发送并原子记录确认状态；
5. 全部目标确认后提交微博游标和检查点。

低震级记录会进入扫描进度，待办事件会在重启后继续投递。目标群为空、网络异常或部分目标发送异常时，现有投递游标保持原值。

---

## 💾 本地状态

数据根目录默认为 `data/earthquake/`：

| 路径 | 内容 |
| --- | --- |
| `earthquake.json` | 当前微博游标 |
| `earthquake.checkpoint.json` | 游标恢复检查点 |
| `earthquake_delivery.json` | 待办事件与逐目标进度 |
| `EarthquakeFigures/` | 校验后的图片缓存 |

游标文件采用有界 JSON 格式。结构异常的文件会原子移动为 `*.corrupt-*` 取证副本，插件随后从有效检查点恢复。图片缓存按内容哈希命名，最多 64 项、64 MiB，保留期为 30 天。

---

## 🔐 网络与数据边界

- 微博移动端访客接口响应会经过 MIME、压缩比例、解码大小、JSON 深度、节点数和卡片数限制；
- 图片来源限定为 `wx1.sinaimg.cn` 至 `wx4.sinaimg.cn` 的 HTTPS 地址；
- 单张图片上限为 8 MiB，并校验格式、容器结尾、帧数、尺寸、像素数与解码内存；
- 微博正文会清理脚本、样式、HTML 标签和控制字符；
- 单条卡片的解析异常局限在该记录内。

插件使用 `requests`、`beautifulsoup4` 和 `Pillow`。微博会话在当前插件代内复用，并在 15 分钟后轮换。

---

## 📌 信息用途

微博访客接口提供尽力而为的消息提醒。应急判断请同时使用中国地震台网官方 App、微信服务号、小程序和所在地权威应急渠道复核。

插件凭据配置为空集。手动查询需要访问微博与新浪图片域名；自动投递需要该 schedule 的 `group_ids` 或全局 `default_group_ids` 解析出至少一个目标群。

---

## ⏰ 生命周期

定时扫描由进程内锁串行化。卸载、重载或 Bot 关闭时，`shutdown()` 在线程执行池中关闭当前微博会话。跨重启进度由游标、检查点和待办文件共同恢复。

---

## 🩺 排障

| 现象 | 检查项 |
| --- | --- |
| 手动查询暂时失败 | 检查微博访客接口、网络连通性和运行日志 |
| 自动消息持续待办 | 检查默认群、Bot 发送权限与待办文件 |
| 状态文件进入隔离副本 | 检查检查点内容和数据目录写权限 |
| 图片下载失败 | 检查新浪图片域名、图片预算和 Pillow 解码日志 |

---

## ✅ 开发验证

在仓库根目录运行：

```bash
python -m pytest -q tests/plugins/earthquake/test_earthquake.py
python -m ruff check plugins/earthquake
python -m mypy plugins/earthquake
```
