# ADS Paper

命令入口是 `/paper`。所有笔记、写作灵感、研究主题、截稿日期和文献库均按 QQ `user_id` 隔离；搜索请求会发送到 NASA ADS，启用 AI 摘要时论文标题和摘要会发送到配置的 LLM 服务。

## 配置

在 `config/secrets.json` 中配置：

```json
{
  "plugins": {
    "ads_paper": {
      "ads_token": "NASA_ADS_TOKEN",
      "api_base": "https://llm.example/v1",
      "api_key": "LLM_API_KEY",
      "model": "model-name"
    }
  }
}
```

只有 `ads_token` 是搜索必需项；三个 LLM 字段必须同时存在才会生成 AI 摘要，否则返回 ADS 原始摘要。

## 命令

- `/paper search <关键词>`、`author <作者>`
- `/paper cite <ID>`、`cite-network <ID>`、`related <ID>`
- `/paper note ...`、`writing ...`、`topics ...`、`deadline ...`
- `/paper summarize <ID>`、`daily`
- `/paper ref_add <ID>`、`refs`
- `/paper help`

`ID` 支持 arXiv ID、arXiv URL 或 ADS bibcode。完整子命令格式以 `/paper help` 为准。
