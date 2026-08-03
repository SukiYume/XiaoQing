# Wolfram|Alpha 插件

管理员可以通过固定的 Wolfram|Alpha HTTPS API 执行数学、物理、化学、单位换算和公开数据查询。插件不会抓取任意 URL，也不会把 App ID 放进 URL、回复或日志。

## 命令

```text
/alpha 1+1
/wa sin(pi/4)
/alpha --mode=step integrate x^2
/alpha --mode=complete population of China
```

支持 `/alpha`、`/wolfram`、`/wa`、`/计算` 四个触发词，命令仅限 Bot 管理员。查询模式必须显式指定：

- 默认或 `--mode=simple`：调用快速文本结果端点。
- `--mode=step`：读取步骤解答 XML。
- `--mode=complete`：读取 Result pod 的 JSON；`--mode=cp` 是兼容别名。

自然问题末尾的 `step` 或 `cp` 始终属于问题正文，不再被解释成模式。查询最多 500 个字符，空查询显示帮助。

## 配置

只从 `config/secrets.json` 的 `plugins.wolframalpha.appid` 读取 App ID：

```json
{
  "plugins": {
    "wolframalpha": {
      "appid": "YOUR-WOLFRAM-APPID"
    }
  }
}
```

App ID 必须是最多 128 个字符的字母、数字、连字符或下划线组合。不要把它写入公开 `config.json`、文档或聊天消息。

## 运行边界

- 三种模式都按官方接口约定使用 GET、30 秒总超时、固定 API 域名和禁止重定向的有界客户端；App ID 仅作为查询参数交给客户端，插件日志不会记录请求 URL 或参数。
- 线上的压缩前后响应均最多 1 MiB；XML/JSON 还受深度、节点数、属性和字符串预算约束。
- 全插件最多同时发出 2 个 Wolfram 请求。
- 最多读取 20 个结果项，最终 API 文本最多 2400 个字符，确保连同问题和提示仍处于 QQ 单条消息预算内。
- HTTP、超时、网络、格式和未知错误分别返回稳定公开提示；日志不记录 App ID 或完整查询结果。
