# URL Parser 插件

URL 解析插件，提供链接解析和预览功能。

## 功能介绍

URL Parser 插件可以自动解析消息中的 URL 链接，获取网页标题、描述、图片等元信息，并生成预览。

## 使用方法

### 自动模式

插件会自动检测消息中的 URL 并解析（如果启用了自动模式）。

### 手动命令

```
/url <链接>             # 解析指定链接
/url help               # 显示帮助信息
```

### 示例

```
/url https://github.com
/url https://arxiv.org/abs/2301.00000
/url help
```

## 配置说明

在 `config/secrets.json` 中配置：

```json
{
  "plugins": {
    "url_parser": {
      "auto_parse": true,
      "proxy": "http://proxy.example.com:8080",
      "timeout": 10,
      "max_url_length": 500
    }
  }
}
```

### 配置项说明

- `auto_parse` - 是否自动解析消息中的 URL（默认: false）
- `proxy` - 代理服务器地址（可选）
- `timeout` - 请求超时时间（秒，默认: 10）
- `max_url_length` - 最大 URL 长度（默认: 500）

## 功能特性

- 自动检测 URL
- 获取网页元信息（标题、描述、图片）
- 支持多种网站
- 特殊网站优化（GitHub, arXiv 等）
- 图片预览
- 缓存机制
- 代理支持

## 支持的元信息

- 网页标题（title）
- 描述（description）
- 预览图片（og:image）
- 站点名称（og:site_name）
- 文章作者（author）

## 特殊支持

### GitHub

- 仓库信息
- Issue/PR 标题和状态
- 用户资料

### arXiv

- 论文标题
- 作者列表
- 摘要信息

## 注意事项

- 某些网站可能需要代理访问
- 解析耗时取决于目标网站响应速度
- 自动模式可能影响性能，建议谨慎开启
- 尊重目标网站的访问限制

## 依赖

- beautifulsoup4
- aiohttp
- lxml (可选，提升解析性能)
