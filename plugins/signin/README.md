# 影视飓风远端签到

该插件操作部署者配置的一组共享有赞/影视飓风账号凭据，不是 QQ 用户本地积分、排行或每日打卡系统。因此命令默认且必须保持 Bot 管理员专用。

## Secret 配置

```json
{
  "plugins": {
    "signin": {
      "yingshijufeng": {
        "app_id": "...",
        "kdt_id": "...",
        "access_token": "...",
        "sid": "..."
      }
    }
  }
}
```

凭据只应写入 `config/secrets.json`，不得通过聊天、日志或公开配置传递。命令 `/signin yingshi`（或 `y`）会向 `h5.youzan.com` 查询并执行签到；Sony 路径已弃用。每日 00:30 的 schedule 只使用部署侧显式目标群。

使用前请确认账号授权及第三方平台服务条款；远端接口变化或凭据失效时会返回失败，不会回退为本地签到。
