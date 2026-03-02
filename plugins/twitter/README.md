# Twitter 插件

Twitter 监控插件，提供 Twitter 账号的动态监控和推送功能。

## 功能介绍

Twitter 插件可以监控指定的 Twitter 账号，当账号发布新推文时自动推送通知。

## 使用方法

### 基本命令

```
/twitter add <用户名>    # 添加监控账号
/twitter remove <用户名> # 移除监控账号
/twitter list            # 列出监控账号
/twitter check           # 手动检查更新
/twitter help            # 显示帮助信息
```

### 示例

```
/twitter add elonmusk
/twitter list
/twitter check
/twitter remove elonmusk
/twitter help
```

## 配置说明

在 `config/secrets.json` 中配置：

```json
{
  "plugins": {
    "twitter": {
      "api_key": "your_api_key",
      "api_secret": "your_api_secret",
      "access_token": "your_access_token",
      "access_secret": "your_access_secret",
      "check_interval": 300
    }
  }
}
```

### 配置项说明

- `api_key` - Twitter API Key（必需）
- `api_secret` - Twitter API Secret（必需）
- `access_token` - Twitter Access Token（必需）
- `access_secret` - Twitter Access Secret（必需）
- `check_interval` - 检查间隔（秒，默认: 300）

## 功能特性

- 多账号监控
- 自动推送新推文
- 手动刷新检查
- 监控列表管理
- 推文内容展示（包括文字、图片、视频）
- 避免重复推送

## Twitter API 申请

1. 访问 [Twitter Developer Portal](https://developer.twitter.com/)
2. 创建应用获取 API 凭证
3. 申请适当的访问级别
4. 将凭证配置到 secrets.json

## 数据存储

监控数据存储在 `data/twitter.json`：

```json
{
  "monitored_users": ["username1", "username2"],
  "last_tweet_ids": {
    "username1": "1234567890",
    "username2": "0987654321"
  }
}
```

## 注意事项

- 需要 Twitter API 访问权限（可能需要付费）
- API 有速率限制，注意不要设置过短的检查间隔
- 监控过多账号可能超出 API 配额
- Twitter API 政策经常变化，注意及时更新
