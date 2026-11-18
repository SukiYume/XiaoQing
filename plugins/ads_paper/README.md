# 🔭 ADS Paper

ADS Paper 通过 [SciX](https://scixplorer.org/)（NASA ADS 的继任服务）做论文检索和文献调研，并把个人科研资料管理整合到 `/paper` 命令。检索、摘要、统计、引用和相关论文支持群聊与私聊；笔记、灵感、主题、截稿日期、每日推荐和文献库使用私聊用户作用域。

插件名、数据目录和凭据键沿用 `ads_paper` 与 `ads_token`。

---

## 🔐 使用条件

- SciX API Token 用于检索、统计、期刊查询与引用导出，在 [SciX 账户设置](https://scixplorer.org/user/settings/token) 生成；原 NASA ADS Token 在 SciX 继续有效。
- `aiohttp` 提供 HTTP 请求，属于核心依赖，随基础安装；缺少时插件不会加载，日志记录缺失的模块名。
- 统一 AI `summary` route 提供中文摘要；未配置时 `/paper summarize` 给出摘要原文。
- 摘要的补充来源 arXiv 与 Europe PMC 是公开接口，不需要凭据。

---

## ⌨️ 命令

| 命令 | 场景 | 功能 |
|---|---|---|
| `/paper search <检索式> [选项]` | 群聊、私聊 | 按 SciX 检索式检索，给出命中总数和分页结果 |
| `/paper author <作者> [选项]` | 群聊、私聊 | 作者的论文，默认新的在前 |
| `/paper abstract <ID>...` | 群聊、私聊 | 一到多篇论文的完整信息与摘要原文 |
| `/paper stats <检索式>` | 群聊、私聊 | 命中总数、被引统计与年份、类型、期刊、关键词分布 |
| `/paper journal <刊名或缩写>` | 群聊、私聊 | 查期刊缩写 bibstem |
| `/paper cite <ID>... [--format 格式]` | 群聊、私聊 | 导出 BibTeX 等引用 |
| `/paper cite-network <ID>` | 群聊、私聊 | 被引与参考文献概览 |
| `/paper related <ID>` | 群聊、私聊 | 内容相近的论文 |
| `/paper summarize <ID>` | 群聊、私聊 | 中文摘要或摘要原文 |
| `/paper note ...` | 私聊 | 论文笔记增删查 |
| `/paper writing ...` | 私聊 | 写作灵感增删查 |
| `/paper topics ...` | 私聊 | 每日推荐主题管理 |
| `/paper deadline ...` | 私聊 | 截稿日期管理 |
| `/paper daily` | 私聊 | UTC 当天新论文推荐 |
| `/paper ref_add <ID>` | 私聊 | 添加个人 BibTeX 条目 |
| `/paper refs` | 私聊 | 查看个人文献库 |
| `/paper help` | 群聊、私聊 | 插件帮助 |

完整子命令与错误样例可通过 `/help ads_paper` 查看。`/paper abs` 是 `/paper abstract` 的别名。

### 论文标识

`ID` 支持以下写法，带 `...` 的命令一次接受最多 8 个，用空格分隔：

| 写法 | 示例 |
|---|---|
| Bibcode | `2018NatAs...2..206B` |
| arXiv 编号（版本号忽略） | `2401.12345`、`2401.12345v2`、`astro-ph/0701089`、`arXiv:2401.12345` |
| DOI | `10.1038/s41550-017-0377-9`、`doi:10.1038/…` |
| 链接 | `arxiv.org` 的 abs/pdf/html、`doi.org`、SciX 与 ADS 的摘要页 |

所有标识经 SciX 的 `identifier` 字段一次查出，预印本编号和曾用 bibcode 会落到已发表的那条记录上。写法认不出的标识不会进入检索式，回复里逐个说明“无法识别”或“SciX 未收录”。

### 检索式

`search` 与 `stats` 的参数是 SciX 检索式，原样交给 SciX。多个条件空格相连表示同时满足，可用 `AND`、`OR`、`NOT` 和括号；裸词在标题、摘要、关键词里匹配，引号内按整句短语匹配。

| 写法 | 含义 |
|---|---|
| `title:"…"`、`abs:"…"`、`full:"…"` | 标题；标题+摘要+关键词；全文 |
| `author:"Zhang, B"`、`author:"^Zhang, B"` | 作者；第一作者 |
| `year:2020-2024` | 年份范围 |
| `bibstem:NatAs` | 期刊缩写，用 `/paper journal` 查 |
| `property:refereed`、`doctype:article` | 同行评议；文献类型 |
| `collection:planetary` | 学科库，另有 `astronomy`、`heliophysics`、`earthscience` |
| `object:"M31"`、`uat:"exoplanets"` | 天体；UAT 主题词 |
| `citations(bibcode:X)`、`references(bibcode:X)` | 引用 X 的论文；X 的参考文献 |
| `similar(bibcode:X)` | 摘要内容与 X 相近的论文 |
| `reviews(…)`、`useful(…)`、`trending(…)` | 引用这批论文最多的文献（多为综述）；这批论文最常引用的文献；读这批论文的人近期还在读的文献 |

刊名写进检索式只会当作普通词匹配，限定期刊要用 `bibstem:`。SciX 解析不了的检索式（括号或引号不成对、字段名后缺内容等）返回 HTTP 400，命令回复说明是检索式的问题。

### 列表选项

`search` 与 `author` 接受下列选项，位置不限：

| 选项 | 作用 |
|---|---|
| `--rows N` | 每页篇数，1–25，默认 10；带 `--abs` 或 `--hl` 时每页最多 10 篇，其余用 `--page` 翻页 |
| `--page N` | 页码，最深到第 2000 条 |
| `--sort relevance\|citations\|date\|reads` | 排序；`search` 默认 `relevance`，`author` 默认 `date` |
| `--abs` | 每篇附 400 字以内的摘要节选 |
| `--hl` | 每篇附最多 3 条摘要和正文里的命中原句，命中词用【】标出 |

`--hl` 标出检索式里带引号的短语，没有短语时按整条检索式匹配；配合 `full:"…"` 可以直接看到正文怎么写的。不认识的 `--选项` 和越界的取值在调用 SciX 之前报错。

每页开头给出命中总数、本页范围和排序方式，每篇列出标题、前三位作者与作者总数、年份、期刊、文献类型（期刊论文不标）、是否同行评议、被引次数、Bibcode、arXiv 编号和 SciX 链接；DOI 在 `abstract` 的单篇信息里给出。还有后续页时回复末尾给出下一页命令，并作为命令结果的续页命令交给 Agent。

### 摘要来源

`abstract`、`summarize` 和 `--abs` 按同一顺序取摘要，并标明来源：

1. SciX 记录里的摘要。
2. 论文有 arXiv 编号时取 arXiv 预印本的摘要，一次请求取回多篇。
3. 论文有出版商 DOI 时查 Europe PMC。

SciX 接口不返回部分出版商（Springer Nature 各刊、Ap&SS、Experimental Astronomy 等）的摘要，这些论文靠后两处补上。预印本摘要可能与发表版措辞不同。后两处请求失败时记一条 `abstract.source_failed` 日志，这篇论文按没有摘要处理，命令照常完成。三处都没有时回复明说没有可用的公开摘要。SciX Token 只发给 `api.scixplorer.org`。

### 统计与其他命令

`/paper stats` 对一条检索式并发发出 6 个只取计数的请求，给出命中总数，被引合计、篇均与单篇最高，同行评议与开放获取篇数，按年份（升序，超过 40 个年份时把更早的合并）、文献类型、期刊前 8 和关键词前 8 的分布。比较不同方向、期刊或年份时对每个范围各跑一次。

`/paper cite` 的 `--format` 可选 `bibtex`（默认）、`bibtexabs`、`aastex`、`mnras`、`icarus`、`soph`、`ieee`、`ris`、`endnote`。

`/paper cite-network` 给出被引总数与参考文献总数，以及两边各自被引最多的 5 篇；完整清单用 `citations(…)`、`references(…)` 检索。`/paper related` 用 `similar(…)` 按摘要文本相似度取 8 篇，SciX 计算需要十几秒。

`/paper daily` 依据 SciX `entdate` 选择 UTC 当天记录，并按 `entdate` 降序、bibcode 升序排列。

`/paper summarize` 按配置给出三种结果：`summary` route 可用时输出中文摘要并注明依据的摘要来源；未配置 AI 能力时输出摘要原文并提示配置；中文摘要生成失败时输出摘要原文和错误说明。

---

## ⚙️ 配置与凭据

`config/secrets.json`：

```json
{
  "plugins": {
    "ads_paper": {
      "ads_token": "SCIX_API_TOKEN"
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
          "summary": ["deepseek-pro", "glm-5.2"]
        }
      }
    }
  }
}
```

Provider 地址位于项目级 `config.ai.providers`，模型 profile 位于 `config.ai.models`，API Key 位于 `secrets.ai.providers`。SciX Token 负责论文检索，AI `summary` route 负责中文摘要。

---

## 💾 数据

数据位于 `data/ads_paper/`：

| 文件 | 内容 |
|---|---|
| `paper_notes.json` | 按 QQ 用户隔离的论文笔记 |
| `writing_ideas.json` | 写作灵感 |
| `research_topics.json` | 每日推荐主题 |
| `deadlines.json` | 截稿日期 |
| `references_<user_id>.bib` | 个人 BibTeX 文献库 |

JSON 使用原子写入。某个数据文件解析异常时，插件保留源文件，另存一份文件名带内容摘要的 `.corrupt-*` 副本，并拒绝读写该文件，对应命令返回错误。人工修复源文件后再次执行命令即可恢复。

检索结果和摘要不落盘，每次命令现取。

---

## 🩺 排障

1. 使用 `/paper search abs:"fast radio burst" --rows 3` 验证 SciX Token 与网络。Token 无效时回复 HTTP 401 和重新生成的地址，额度用完时回复 HTTP 429。
2. 使用 `/paper cite 2401.12345` 验证标识解析与 BibTeX 导出。
3. 使用 `/paper abstract 2018NatAs...2..206B` 验证摘要的补充来源。
4. 使用 `/paper summarize 2401.12345` 查看当前摘要 route。
5. 查看日志：SciX 返回 400、401、429 时记 `scix.request_rejected` 和状态码，其他 HTTP 状态码在 `public_error` 的 `error.status` 里；补充来源失败记 `abstract.source_failed` 和来源名；route 与 profile 在 Core 的 `ai.request.settings`（DEBUG 级）和 `ai.route.fallback` 里；数据文件损坏时 `storage.corrupt` 带内容摘要。同一条命令的日志共用一个 request ID。
6. 对 `.corrupt-*` 文件执行人工核验与数据恢复。

---

## ✅ 开发验证

```bash
python -m pytest tests/plugins/ads_paper -q
python -m ruff check plugins/ads_paper
```
