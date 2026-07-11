# NASA Astronomy Picture of the Day

`/apod` 抓取 NASA `https://apod.nasa.gov/apod/astropix.html` 的当天页面，返回图片或视频链接、标题和说明。当前版本不支持日期参数，也不使用 UCL mirror 或 API key。

## 命令

- `/apod`：获取当天 APOD
- `/apod help`：帮助

每天 13:30 的 schedule 使用部署侧 `default_group_ids`；干净安装没有目标群时不会发送。

## 可选公开配置

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

页面和图片只允许 HTTPS；每次重定向都会重新校验公网 DNS 和 `allowed_hosts`。HTML、图片字节数、MIME 和解码像素均有限制，缓存名使用最终 URL 的 SHA-256。
