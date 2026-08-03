# 影视飓风远端签到

该插件使用部署者配置的一组有赞/影视飓风共享凭据执行远端签到，不是 QQ 用户本地积分、排行或每日打卡
系统。因此 `/signin` 与等价入口 `/签到` 必须保持 `admin_only`。

## 命令与计划任务

```text
/signin yingshi
/signin y
/signin help
```

- `yingshi`、`yingshijufeng` 和 `y` 执行同一签到流程。
- 清单在调度器时区每天 00:30 调用 `scheduled_yingshi`；手动命令和计划任务按插件顺序执行，避免并发
  触发同一组共享凭据。

## Secret 配置

凭据只应写入 `config/secrets.json`：

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

四个字段都必须是非空字符串或整数。不要通过聊天、日志或公开配置传递这些凭据。

## 远端行为与边界

- 插件固定访问 `https://h5.youzan.com`：先查询签到 ID，再执行签到；访问令牌只放在
  `Authorization` 请求头中，不写入 URL 参数。
- HTTP 状态、Content-Type、压缩比例、响应字节数和 JSON 深度/节点数均由 core 有界传输层校验。
- 第三方消息字段会做类型检查和长度限制，奖励回复最多保留 20 条成功记录。
- 凭据缺失或 HTTP 会话未初始化会返回明确提示；网络、格式或内部异常使用统一的脱敏错误边界。

使用前请确认账号授权及第三方平台服务条款。远端接口变化或凭据失效时只会报告失败，不会回退为本地签到。
