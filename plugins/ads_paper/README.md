# 🔭 ADS Paper

ADS Paper 将 NASA ADS 论文检索与个人科研资料管理整合到 `/paper` 命令。搜索、引用网络和相关论文支持群聊与私聊；笔记、灵感、主题、截稿日期、每日推荐和文献库使用私聊用户作用域。

---

## 🔐 使用条件

- NASA ADS Token 用于检索与 BibTeX。
- `aiohttp` 提供 ADS API 请求。
- 统一 AI `summary` route 提供中文摘要增强。

---

## ⌨️ 命令

| 命令 | 场景 | 功能 |
|---|---|---|
| `/paper search <关键词>` | 群聊、私聊 | 关键词检索 |
| `/paper author <作者>` | 群聊、私聊 | 作者检索 |
| `/paper cite <ID>` | 群聊、私聊 | BibTeX |
| `/paper cite-network <ID>` | 群聊、私聊 | 引用与被引网络 |
| `/paper related <ID>` | 群聊、私聊 | 相关论文 |
| `/paper summarize <ID>` | 群聊、私聊 | 中文摘要或 ADS 摘要 |
| `/paper note ...` | 私聊 | 论文笔记增删查 |
| `/paper writing ...` | 私聊 | 写作灵感增删查 |
| `/paper topics ...` | 私聊 | 每日推荐主题管理 |
| `/paper deadline ...` | 私聊 | 截稿日期管理 |
| `/paper daily` | 私聊 | UTC 当天新论文推荐 |
| `/paper ref_add <ID>` | 私聊 | 添加个人 BibTeX 条目 |
| `/paper refs` | 私聊 | 查看个人文献库 |
| `/paper help` | 群聊、私聊 | 插件帮助 |

`ID` 支持 arXiv ID、arXiv URL 和 ADS bibcode。完整子命令与错误样例可通过 `/help ads_paper` 查看。

---

## ⚙️ 配置与凭据

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

Provider 地址位于项目级 `config.ai.providers`，模型 profile 位于 `config.ai.models`，API Key 位于 `secrets.ai.providers`。ADS Token 负责论文检索，AI `summary` route 负责中文摘要；route 可用状态决定摘要展示层级。

---

## 🔐 数据与权限

数据位于 `data/ads_paper/`：

| 文件 | 内容 |
|---|---|
| `paper_notes.json` | 按 QQ 用户隔离的论文笔记 |
| `writing_ideas.json` | 写作灵感 |
| `research_topics.json` | 每日推荐主题 |
| `deadlines.json` | 截稿日期 |
| `references_<user_id>.bib` | 个人 BibTeX 文献库 |

JSON 使用原子写入。解析异常会保留源文件，并生成带内容摘要的 `.corrupt-*` 隔离副本；写入路径进入保护状态，数据恢复后继续服务。BibTeX 使用结构化条目扫描处理邮箱、URL、注释和字段值中的 `@`。

`/paper daily` 依据 ADS `entdate` 选择 UTC 当天记录，并按 `entdate` 降序、bibcode 升序排列。

`/paper author` 按 ADS `date` 降序展示作者的近期论文。引用网络中的“本次展示参考文献”表示当前返回的最多 5 篇记录；被引用次数使用论文的 ADS `citation_count` 字段。

---

## 🩺 排障

1. 使用 `/paper search "fast radio burst"` 验证 ADS Token 与网络。
2. 使用 `/paper cite 2401.12345` 验证 ID 解析与 BibTeX。
3. 使用 `/paper summarize 2401.12345` 查看当前摘要 route。
4. 检查日志中的 ADS 状态码、route profile、存储摘要和 request ID。
5. 对 `.corrupt-*` 文件执行人工核验与数据恢复。

---

## ✅ 开发验证

```bash
python -m pytest tests/plugins/ads_paper/test_ads_paper.py -q
python -m ruff check plugins/ads_paper
```
