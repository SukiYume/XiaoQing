# ✅ 影视飓风远端签到

`signin` 使用部署者配置的一组有赞/影视飓风凭据执行远端签到，所有调用共用这一组凭据。命令只有 Bot 管理员能用。

---

## ⌨️ 命令与调度

```text
/signin yingshi
/签到 yingshi
/signin y
/signin help
```

`yingshi`、`yingshijufeng` 和 `y` 指向同一签到流程。`帮助` 和 `?` 是 `help` 的别名。

每天 00:30 自动签到一次，时间按 `config.timezone`（默认 `Asia/Shanghai`）计，结果以 `broadcast` 模式发到目标群。目标群由 `config/config.json` 的 `plugins.signin.schedules.yingshi.group_ids` 指定，没写时使用全局 `default_group_ids`。插件的并发模式为 `sequential`：手动签到和定时签到按调用顺序依次访问远端服务。

---

## ⚙️ 凭据配置

在 `config/secrets.json` 中配置：

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

四个字段接受非空字符串或整数。凭据不会出现在聊天消息、公开配置和日志里。

---

## 🔄 远端流程

插件固定访问 `https://h5.youzan.com`：

1. 查询当前签到 ID；
2. 使用签到 ID 提交签到；
3. 解析签到描述、累计次数和奖励；
4. 最多展示 20 条有效奖励记录。

访问令牌位于 `Authorization` 请求头。Core 有界传输层负责 HTTP 状态、MIME、压缩比例、响应字节、JSON 深度和节点数校验。第三方文本还会经过类型、长度与控制字符处理。

账号授权与远端服务使用范围由部署者按第三方平台条款管理。

---

## 🩺 排障

| 现象 | 检查项 |
| --- | --- |
| 提示凭据缺失 | 核对四个字段和 JSON 层级 |
| 远端返回认证错误 | 更新 `access_token`、`sid` 和账号授权 |
| 定时任务执行异常 | 核对调度器时区、00:30 日志和网络连接 |
| 响应格式异常 | 检查有赞接口变化与有界响应错误码 |

---

## ✅ 开发验证

在仓库根目录运行：

```bash
python -m ruff check plugins/signin tests/plugins/signin/test_signin.py
python -m mypy plugins/signin
python -m pytest -q tests/plugins/signin/test_signin.py -n 2
```
