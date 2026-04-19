# Twitter 图片插件

从指定 X/Twitter 账号抓取图片到本地，并随机发送库存中的一张图片。

## 命令

```text
/twimg
/twitter
/推特
/tw_fetch
/抓取推特
```

- `/twimg`：随机发送一张已抓取图片
- `/tw_fetch`：立即抓取新图片，仅管理员可用

插件还会在每天 `03:00` 自动抓取。

## 配置

在 `config/secrets.json` 中配置：

```json
{
  "plugins": {
    "twitter": {
      "user_id": "Twitter用户ID",
      "headers": {},
      "cookies": {},
      "proxy": "http://proxy.example.com:8080",
      "max_pages": 50
    }
  }
}
```

### 配置项说明

- `user_id`：要抓取的目标用户 ID
- `headers`：请求头，通常用于补充认证信息
- `cookies`：Cookie 配置
- `proxy`：可选代理地址；只有显式配置时才启用
- `max_pages`：单次抓取最多检查的页数

## 数据与行为

- 新图片会下载到本地，避免重复抓取。
- 随机发送时优先选择未发送过的图片。
- 当所有图片都发过一轮后，会自动重置发送状态。
- 不再默认使用本地 `127.0.0.1:1080` 代理。

## 注意事项

- 该插件依赖目标站点当前可用的网页接口与认证头。
- 若目标站点在当前网络环境不可达，再考虑显式配置 `proxy`。
