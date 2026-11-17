# 🔗 URL Parser

`url_parser` 为完整的单 URL 消息生成网页标题、描述和可选预览图。插件没有命令，消息整体是一个 URL 时自动触发。

需要 `beautifulsoup4` 和 Pillow（属于项目的 `plugins` 可选依赖）：

```bash
python -m pip install -e ".[plugins]"
```

两者缺失时插件不会加载。安装后发送 `/reload` 重载插件。

---

## 📌 触发条件

消息整体是一个 `http://` 或 `https://` URL 时触发。带命令、带附加文字或包含多个 URL 的消息不生成预览，按普通消息或命令处理。群里的 URL 消息与其他消息一样，`require_bot_name_in_group` 为 `true`（默认）时需要先点名机器人，被静音的群不触发。

示例：

```text
https://example.com/article
```

回复格式：

```text
🔗 <标题>
<描述>

链接: <URL>
```

有预览图时，图片附在文字之后。

---

## 💾 元数据规则

| 内容 | 来源与规则 |
| --- | --- |
| 标题 | `<title>`，折叠空白，最多 200 个字符 |
| 描述 | `description`、`og:description`、`twitter:description`，最多 100 个字符 |
| 图片 | `og:image`、`twitter:image`，相对地址按最终页面 URL 补全 |
| 兜底标题 | 页面提供描述或图片时使用“网页预览” |

页面提供有效元数据时，插件回复文本摘要和可选图片。页面元数据为空时不回复。

---

## 🔐 公网请求边界

页面和图片使用独立的公网客户端：独立会话，不带任何凭据，不使用环境代理配置，请求只带最小的公开请求头。每次请求与重定向都会重新校验 URL、DNS 和目标地址，目标范围限定为公网 HTTP/HTTPS 服务。

| 项目 | 资源预算 |
| --- | ---: |
| 输入 URL | 2048 个字符 |
| HTML | 2 MiB |
| 图片 URL | 4096 个字符 |
| 单张图片 | 5 MiB、2000 万像素、120 帧 |
| 图片格式 | JPEG、PNG、WebP |
| 并发网页预览 | 4 个 |

图片处理异常会保留已解析的文字摘要。页面请求异常时不回复。

---

## 💾 图片缓存

预览图位于：

```text
data/url_parser/url_previews/
```

文件名由图片 URL 的 SHA-256 和验证后的扩展名组成。缓存最多 128 项、128 MiB，保留期为 7 天。每次缓存命中都会重新校验当前 URL/DNS 策略和本地图片资源边界。

---

## 📌 支持范围

插件只解析服务端返回的标准 HTML 元数据，没有针对具体站点的适配。服务端渲染标题、Open Graph 和 Twitter Card 的页面可直接生成预览。需要浏览器脚本渲染、登录会话或专有接口的页面，只有初始 HTML 里带有这些元数据时才能生成预览。

---

## 🔐 隐私与日志

请求不携带 Core 应用凭据、Cookie、插件密钥、共享代理和用户身份。日志记录标题长度、状态与错误类别，目标主机只记长度和指纹，不记 URL 和页面内容。

---

## 🩺 排障

| 现象 | 检查项 |
| --- | --- |
| 消息未触发预览 | 确认消息内容是单个完整 HTTP/HTTPS URL |
| 返回文字且缺少图片 | 检查页面图片元数据、图片域名和资源预算 |
| 页面摘要为空 | 查看原始 HTML 中的 title 与 description 元数据 |
| 请求被安全边界拒绝 | 检查目标 DNS、重定向链和公网地址属性 |
| 缓存写入失败 | 检查 `data/url_parser/` 权限与磁盘空间 |

---

## ✅ 开发验证

在仓库根目录运行：

```bash
python -m ruff check plugins/url_parser tests/plugins/url_parser/test_url_parser.py
python -m mypy plugins/url_parser
python -m pytest -q tests/plugins/url_parser/test_url_parser.py -n 2
```
