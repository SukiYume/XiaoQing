# 🔗 URL Parser

`url_parser` 为完整的单 URL 消息生成网页标题、描述和可选预览图。插件通过 Dispatcher URL 入口工作，Manifest 命令列表为空。

---

## 📌 触发条件

清理后的消息整体匹配一个 `http://` 或 `https://` URL 时，Dispatcher 调用 `handle_url()`。命令消息、附加文字和多 URL 消息走各自的 Dispatcher 分支。

示例：

```text
https://example.com/article
```

---

## 💾 元数据规则

| 内容 | 来源与规则 |
| --- | --- |
| 标题 | `<title>`，折叠空白，最多 200 个字符 |
| 描述 | `description`、`og:description`、`twitter:description`，最多 100 个字符 |
| 图片 | `og:image`、`twitter:image`，相对地址按最终页面 URL 补全 |
| 兜底标题 | 页面提供描述或图片时使用“网页预览” |

页面提供有效元数据时，插件返回文本摘要和可选图片。页面元数据为空时，返回空消息段交由 Core 结束本次预览。

---

## 🔐 公网请求边界

页面和图片使用独立的无凭据公网客户端。每次请求与重定向都会重新校验 URL、DNS 和目标地址，目标范围限定为公网 HTTP/HTTPS 服务。

| 项目 | 当前预算 |
| --- | ---: |
| 输入 URL | 2048 个字符 |
| HTML | 2 MiB |
| 图片 URL | 4096 个字符 |
| 单张图片 | 5 MiB、2000 万像素、120 帧 |
| 图片格式 | JPEG、PNG、WebP |
| 并发网页预览 | 4 个 |

客户端使用独立会话、空凭据和空环境代理配置。图片处理异常会保留已解析的文字摘要；页面请求异常会结束本次预览。

---

## 💾 图片缓存

预览图位于：

```text
data/url_parser/url_previews/
```

文件名由图片 URL 的 SHA-256 和验证后的扩展名组成。缓存最多 128 项、128 MiB，保留期为 7 天。每次缓存命中都会重新校验当前 URL/DNS 策略和本地图片资源边界。

---

## 📌 网站兼容性

插件解析服务端返回的标准 HTML 元数据。服务端渲染标题、Open Graph 和 Twitter Card 的页面可直接生成预览。需要浏览器脚本、登录会话或专有接口的页面取决于其初始 HTML 内容。

---

## 🔐 隐私与日志

请求携带最小公开请求头。Core 应用凭据、Cookie、插件 secret、共享代理和用户身份保留在请求边界之外。日志记录目标 host、标题长度、状态与安全错误类别。

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
