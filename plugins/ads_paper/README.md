# ADS Paper 插件

NASA ADS 论文管理插件，提供天文论文搜索、管理和 AI 辅助分析功能。

## 功能介绍

ADS Paper 插件集成了 NASA ADS (Astrophysics Data System) API，提供论文搜索、笔记管理、AI 摘要和分析等功能。

## 使用方法

### 论文搜索命令

```
/ads search <关键词>      # 搜索论文
/ads author <作者名>      # 按作者搜索
/ads bibcode <bibcode>    # 按 bibcode 查询
/ads recent <作者名>      # 查看作者最新论文
```

### 笔记管理命令

```
/ads note add <bibcode> <笔记>    # 添加笔记
/ads note list                     # 列出所有笔记
/ads note view <bibcode>           # 查看笔记
/ads note delete <bibcode>         # 删除笔记
```

### AI 辅助命令

```
/ads summary <bibcode>             # AI 生成论文摘要
/ads analyze <bibcode>             # AI 分析论文
/ads compare <bib1> <bib2>         # 比较两篇论文
```

### 示例

```
/ads search "black hole merger"
/ads author "Einstein, A."
/ads bibcode 2023ApJ...123..456S
/ads recent "Hawking, S."
/ads note add 2023ApJ...123..456S 这篇论文很重要
/ads summary 2023ApJ...123..456S
/ads help
```

## 配置说明

在 `config/secrets.json` 中配置：

```json
{
  "plugins": {
    "ads_paper": {
      "api_token": "your_ads_api_token",
      "llm_api_key": "your_openai_api_key",
      "llm_model": "gpt-4",
      "max_results": 10,
      "sort_by": "citation_count"
    }
  }
}
```

### 配置项说明

- `api_token` - NASA ADS API Token（必需）
- `llm_api_key` - OpenAI API Key（AI 功能必需）
- `llm_model` - 使用的语言模型（默认: gpt-3.5-turbo）
- `max_results` - 搜索最大结果数（默认: 10）
- `sort_by` - 排序方式
  - `citation_count` - 按引用数
  - `date` - 按日期
  - `relevance` - 按相关性

## 功能特性

### 论文搜索
- 全文搜索
- 作者搜索
- bibcode 精确查询
- 高级过滤（年份、期刊等）
- 引用次数排序

### 笔记管理
- 为论文添加个人笔记
- 笔记持久化存储
- 搜索笔记
- 笔记导出

### AI 辅助功能
- 自动生成论文摘要
- 深度分析论文内容
- 提取关键发现
- 论文对比分析
- 研究方向建议

### 论文信息
- 标题、作者、年份
- 摘要
- 引用次数
- 期刊信息
- bibcode
- DOI
- arXiv ID
- PDF 链接

## NASA ADS API

### 申请 API Token

1. 访问 [NASA ADS](https://ui.adsabs.harvard.edu/)
2. 注册账号
3. 在设置中生成 API Token
4. 将 Token 配置到 secrets.json

### API 限制

- 每天 5000 次请求
- 每小时 300 次请求
- 注意合理使用

## 数据存储

- 笔记数据: `data/notes.json`
- 论文缓存: `data/papers_cache.json`
- 搜索历史: `data/search_history.json`

数据格式：
```json
{
  "bibcode": {
    "title": "Paper Title",
    "authors": ["Author1", "Author2"],
    "notes": "My notes here",
    "tags": ["tag1", "tag2"],
    "timestamp": "2026-02-04T12:00:00"
  }
}
```

## 模块结构

- `main.py` - 主入口和命令路由
- `ads_client.py` - ADS API 客户端
- `note_commands.py` - 笔记管理功能
- `paper_commands.py` - 论文查询功能
- `ai_commands.py` - AI 辅助功能
- `storage.py` - 数据存储管理
- `llm_client.py` - 语言模型客户端

## 依赖

- ads (ADS API 客户端)
- openai (AI 功能)

安装依赖：
```bash
pip install ads openai
```

## 适用场景

- 天文学文献调研
- 论文管理和笔记
- 快速了解论文内容
- 跟踪研究动态
- 论文对比分析

## 注意事项

- ADS API Token 必须配置
- AI 功能需要 OpenAI API Key
- API 调用可能产生费用
- 遵守 ADS 使用条款
- 注意 API 速率限制

## 参考

- NASA ADS: https://ui.adsabs.harvard.edu/
- ADS API Documentation: https://github.com/adsabs/adsabs-dev-api
