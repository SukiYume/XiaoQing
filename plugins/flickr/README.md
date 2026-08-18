# 📷 Flickr 公共摄影插件

> 在 QQ 中浏览 Flickr 精选、公共搜索、用户公开照片与公开相册，并保留作者、许可和原图页信息。

---

## ✨ 功能概览

Flickr 插件通过官方 REST API 读取公开照片。默认入口展示今日精选，搜索支持关键词、标签、排序、许可类型和拍摄日期。每次查询会建立 15 分钟的会话，方便使用 `/flickr more` 连续浏览。

插件默认使用 `license=any`，覆盖 Flickr 上全部公开许可类型，包括 All Rights Reserved、Creative Commons、公共领域标记和政府作品。每条回复展示对应许可，图片使用仍需遵循照片作者与 Flickr 页面标明的条件。

| 能力 | 说明 |
| --- | --- |
| 今日精选 | 读取 Flickr Interestingness 当日列表 |
| 公共搜索 | 按文字、标签、排序、许可和拍摄日期筛选 |
| Commons | 限定 Flickr Commons 公共典藏来源 |
| 用户浏览 | 通过用户名、NSID 或个人页 URL 查找公开照片 |
| 相册浏览 | 读取公开相册中的照片 |
| 连续浏览 | 同一用户、同一会话范围内继续发送 1–5 张 |
| 照片详情 | 展示标题、作者、许可、日期、标签、描述与原图页 |

---

## ✅ 使用条件

1. 安装常用插件依赖：

   ```bash
   python -m pip install -e ".[plugins]"
   ```

2. 在 [Flickr App Garden](https://www.flickr.com/services/apps/create/apply/) 创建非商业 API Key。Flickr 申请页会依据账号方案显示可用资格。

3. 将 Key 写入 `config/secrets.json`：

   ```json
   {
     "plugins": {
       "flickr": {
         "api_key": "your-flickr-api-key"
       }
     }
   }
   ```

`api_key` 属于私密配置。仓库中的 `config/secrets.json.example` 只保存占位值，真实 Key 由部署环境管理。

首次部署在启动 Bot 前保存完整 secrets 文件。运行实例已包含示例中的 Flickr 路径时，也可在管理员私聊中提交：

```text
/set_secret plugins.flickr.api_key <你的 API Key>
```

运行实例需要新增 Flickr 路径时，可写入完整有效的 `config/secrets.json`。公开配置保持当前版本时，watcher 暂存候选并私聊管理员显示 `plugins.flickr.api_key` 已新增；核对后发送 `/reload` 应用。通知与日志仅记录字段路径，API Key 保留在 secrets 来源中。

---

## ⌨️ 命令

### 今日精选

```text
/flickr
/flicker
/弗里克
```

返回今日精选的第一张照片，并建立后续浏览会话。

### 搜索公开照片

```text
/flickr search aurora
/flickr search Milky Way --sort interesting
/flickr search --tags nebula,telescope --license cc
/flickr search eclipse --date 2026-08
/flickr 搜索 星云 --license any
```

| 选项 | 值 | 默认值 | 含义 |
| --- | --- | --- | --- |
| `--tags` | 逗号分隔标签 | 空 | 所有标签共同匹配 |
| `--sort` | `relevance`、`interesting`、`new`、`old` | `relevance` | 相关度、趣味度、发布时间新旧 |
| `--license` | `any`、`cc`、`public-domain` | `any` | 全部公开许可、CC、公共领域类 |
| `--date` | `YYYY-MM` 或 `YYYY-MM-DD` | 空 | 按拍摄日期筛选 |

无文字关键词时可单独使用 `--tags`。

### Flickr Commons

```text
/flickr commons astronomy
/flickr 典藏 moon --sort interesting
```

查询参数与 `search` 相同，同时将来源限定为 Flickr Commons。

### 用户公开照片

```text
/flickr user NASA on The Commons
/flickr user 12345678@N00
/flickr user https://www.flickr.com/photos/example/
```

用户名通过 Flickr API 解析为用户 NSID。标准 `photos` 与 `people` 个人页 URL 均可使用。

### 公开相册

```text
/flickr album https://www.flickr.com/photos/example/albums/72100000000000000
/flickr album example 72100000000000000
```

相册 API 同时需要所有者和相册 ID。完整相册 URL 会自动提取这两个字段。

### 继续浏览

```text
/flickr more
/flickr more 5
/flickr 更多 3
```

默认继续 1 张，一次最多 5 张。浏览状态按“私聊用户”或“群号 + 用户”隔离，15 分钟后自动失效。

### 照片详情

```text
/flickr info
/flickr info 123456789
/flickr info https://www.flickr.com/photos/example/123456789/
/flickr info https://flic.kr/p/AbCdEf
```

无参数时查看当前会话最近一张照片。照片 ID、标准照片页和 `flic.kr` 短链接均可解析。

---

## 🔐 网络、内容与许可边界

- API 请求固定发送到 `https://api.flickr.com/services/rest`，采用 HTTPS、显式超时、响应字节上限、JSON 深度/节点上限和严格 MIME 校验。
- API Key 仅进入固定 API 请求参数。公开错误、日志和 QQ 回复均不会包含 Key。
- 公共无 OAuth 调用遵循 Flickr Safe Search 可见范围，只读取公开照片。
- 图片下载仅接受 `https://live.staticflickr.com`，每次重新校验域名、TLS、重定向、MIME、字节、像素、尺寸和帧数。
- 外部标题、作者、标签和描述均经过字符数与 UTF-8 字节预算处理。
- 回复中的许可来自 Flickr 照片元数据。All Rights Reserved 照片可在私人 Bot 会话中浏览，复制、转载和再利用需取得对应权利。

---

## 💾 数据与生命周期

| 数据 | 位置 | 边界 |
| --- | --- | --- |
| 图片缓存 | `data/flickr/images/` | 最多 256 项、256 MiB、1 小时 |
| 浏览会话 | 插件 generation 内存 | 最多 512 个、15 分钟 |
| API Key | `config/secrets.json` | Git 忽略的部署私密配置 |

插件热重载和进程重启会清空浏览会话。图片缓存采用内容来源哈希命名并保留在 `data/flickr/`，可随运行数据一起备份或清理。

---

## 🧯 排障

| 提示 | 检查项 |
| --- | --- |
| `Flickr API Key 未配置` | 检查 `plugins.flickr.api_key` 层级与 JSON 格式 |
| `Flickr API Key 无效或已失效` | 在 App Garden 确认 Key 状态并更新私密配置 |
| `Flickr 暂时不可用` | 检查公网、DNS、代理、Flickr 服务状态与调用频率 |
| `没有找到可发送的公开照片` | 放宽关键词、标签、日期或许可条件 |
| `图片下载暂时失败` | 打开回复中的 Flickr 原图页；检查静态图片域名和代理 DNS |
| `当前没有可继续的结果` | 重新执行精选、搜索、用户或相册命令 |

---

## 🧪 验证

```bash
python -m pytest tests/plugins/flickr -q
python -m pytest tests/core/test_plugin_manifest_stability.py -q
python -m ruff check plugins/flickr tests/plugins/flickr
python -m ruff format --check plugins/flickr tests/plugins/flickr
```

单元测试覆盖固定 API 传输、JSON 契约、默认全部许可、筛选参数、用户与相册解析、短链接、会话隔离、翻页、图片校验、缓存、错误输入和密钥脱敏。配置有效的运行环境还可通过 QQ 命令执行真实 API 验收。
