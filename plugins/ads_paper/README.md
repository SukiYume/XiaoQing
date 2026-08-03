# ADS Paper

命令入口是 `/paper`。所有笔记、写作灵感、研究主题、截稿日期和文献库均按 QQ `user_id` 隔离；搜索请求会发送到 NASA ADS，启用 AI 摘要时论文标题和摘要会发送到配置的 LLM 服务。

`search`、`author`、`cite`、`cite-network`、`related` 与 `summarize` 可在私聊或群聊使用；`note`、`writing`、`topics`、`deadline`、`daily`、`ref_add` 与 `refs` 涉及个人数据，只允许私聊。

## 配置

NASA ADS Token 仍是插件私有凭据；LLM 摘要改用统一的 `summary` route。

`config/secrets.json`：

```json
{
  "plugins": {
    "ads_paper": {
      "ads_token": "NASA_ADS_TOKEN"
    }
  }
}
```

`config/config.json`：

```json
{
  "plugins": {
    "ads_paper": {
      "ai": {
        "routes": {
          "summary": {
            "models": ["deepseek-pro", "glm-5.2"],
            "temperature": 0.7,
            "max_tokens": 1200,
            "timeout_seconds": 60,
            "total_timeout_seconds": 90,
            "max_retry": 0
          }
        }
      }
    }
  }
}
```

模型 profile、服务商连接和 API Key 分别来自项目级 `config.ai.models`、`config.ai.providers` 和 `secrets.ai.providers`。只有 `ads_token` 是搜索必需项；`summary` route 不可用时返回 ADS 原始摘要。

## 命令

- `/paper search <关键词>`、`author <作者>`
- `/paper cite <ID>`、`cite-network <ID>`、`related <ID>`
- `/paper note ...`、`writing ...`、`topics ...`、`deadline ...`
- `/paper summarize <ID>`、`daily`
- `/paper ref_add <ID>`、`refs`
- `/paper help`

`ID` 支持 arXiv ID、arXiv URL 或 ADS bibcode。完整子命令格式以 `/paper help` 为准。

`/paper daily` 只显示 UTC 当天新建的 ADS 记录：查询和返回结果都使用 ADS 的 `entdate` 字段，并按 `entdate` 降序、bibcode 升序稳定排列；旧记录、缺少可验证 entry date 的记录不会冒充“今日论文”。

本地 JSON 使用原子写入。若笔记、灵感、主题或 deadline 文件出现截断、非法编码、非法常量或非对象根节点，插件会保留原文件、生成带内容摘要后缀的 `.corrupt-*` 隔离副本，并拒绝所有可能覆盖该文件的 mutation。管理员需要检查隔离副本并显式修复或恢复原文件后才会恢复写入。文献库使用结构化 BibTeX 边界扫描，不会把邮箱、URL、注释或字段值中的 `@` 当成新条目。
