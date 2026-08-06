# arXiv Filter 插件

自动筛选每日 arXiv 天体物理论文。运行时根据模型目录中的
`training_config.json` 自动选择 Transformer、k-NN 或多兴趣后端。

## 功能

- 每日定时从 arXiv `astro-ph/new` 页面获取最新论文
- 支持标题模型、标题+摘要模型、k-NN 兴趣库和多兴趣聚类模型
- 支持定时检查 arXiv 更新，自动推送筛选结果
- 筛选出 positive 论文后，自动把全部 arXiv 链接交给 Codex `astro-ph` 会话生成中文 Markdown 摘要

## 使用方法

```
/arxiv       # 获取 arXiv 当前最新列表的筛选结果
/arxiv help  # 显示帮助信息
```

## 定时任务

在 `plugin.json` 中配置的定时任务：

| 时间 | 行为 |
|------|------|
| 周一~周五 10:00 / 10:30 / 11:00 / 11:30 | 检查 arXiv 是否更新到当天，更新则执行筛选并推送 |
| 周一~周五 12:00 | 最后一次检查，若仍未更新则发送停更通知 |

每天只推送一次（通过 `data/update_status.json` 去重）。
`plugin.json` 不写死群号，定时任务使用部署配置中的 `default_group_ids`；未配置投递目标时会安全跳过。

论文列表推送和 Codex 摘要是两条独立消息链路：arXiv Filter 会先发送筛选出的论文列表，然后在后台把所有 positive 论文链接交给 Codex 插件；Codex 完成后再单独回发摘要。如果 Codex 总结失败，失败消息由 Codex 插件单独发送，不会阻止论文列表消息。

如果运行环境中 Codex 摘要模块不可用或加载失败，arXiv Filter 仍会正常筛选并发送论文列表；摘要侧路只记录日志并跳过，不影响 `/arxiv` 和定时任务。

手动 `/arxiv` 会读取并显示 arXiv 源站当前最新列表的发布日期。每天源站更新前，返回上一发布日的列表属于正常行为；更新后，推理缓存会按新的源列表日期失效，不会把更新前结果继续当成当天结果。

Codex 摘要的复用身份由“源列表日期 + 规范化后的论文链接集合”共同组成。只有两者都相同才会重发历史摘要或复用队列中任务；同一日期的列表内容发生变化时会重新投递。若无法确认源列表日期，论文列表仍可返回，但不会用本地日期猜测并投递 Codex 摘要。

摘要会话、工作目录和方法论文件名由 `plugins.codex.arxiv_summary` 配置控制，默认使用 `astro-ph` 会话和 `arxiv-summary-methodology.md`。

## 配置

插件自带 `config.json`：

```json
{
    "model": {
        "path": "best_model",
        "threshold": 0.3826,
        "batch_size": 256,
        "max_len": 512
    },
    "arxiv": {
        "url": "https://arxiv.org/list/astro-ph/new",
        "proxy": null,
        "use_ssl_verify": true,
        "timeout": 30
    }
}
```

### 配置项说明

| 分组 | 字段 | 说明 |
|------|------|------|
| model | `path` | 模型目录（相对于插件目录） |
| model | `threshold` | 模型目录未提供 `optimal_threshold` 时的后备阈值；显式调用参数优先级最高 |
| model | `batch_size` | 推理批大小 |
| model | `max_len` | 最大 token 长度 |
| arxiv | `url` | arXiv 列表页 URL |
| arxiv | `proxy` | HTTP/HTTPS 代理（也可通过 `ARXIV_PROXY` 环境变量设置） |
| arxiv | `use_ssl_verify` | 是否验证 SSL 证书 |
| arxiv | `timeout` | 请求超时（秒） |

模型权重属于外部运行资产，不包含在 PyPI 的 wheel 或 sdist 中。干净的
`pip install xiaoqing[arxiv-ml]` 只安装推理代码和依赖，部署时还必须提供一个
完整模型目录。推荐把模型放在包目录之外，并设置绝对路径：

```bash
export ARXIV_MODEL_PATH=/srv/xiaoqing-models/arxiv
```

Windows PowerShell 可使用
`$env:ARXIV_MODEL_PATH = 'D:\models\xiaoqing-arxiv'`。环境变量优先于
`config.json` 的 `model.path`；仓库工具
`python scripts/arxiv_inference_cli.py --model-path <目录>` 的命令行参数优先级最高。
显式参数或环境变量一旦设置即为权威路径：路径不存在时推理会明确失败，不会静默
回退到另一套模型。模型目录必须来自可信来源；部分后端会读取可执行的序列化模型
文件。Python 发布门禁只验证推理代码，不验证外部模型权重的来源或完整性。

## 项目结构

```
arxiv_filter/
├── main.py                   # 插件入口（命令处理、定时任务）
├── codex_summary.py          # Codex 摘要侧路投递
├── arxiv_inference.py        # 对外兼容 facade
├── arxiv_today.py            # arXiv 数据获取（网页爬取 + API）
├── numerics.py               # 稳定数值计算
├── utils.py                  # 公共工具（配置加载）
├── inference/                # 参数解析、后端分发和三种推理后端
├── config.json               # 插件配置
├── plugin.json               # 插件元数据
└── train_model/              # 仅仓库开发使用，不进入 PyPI 产物
    ├── training_common.py    # 各训练入口共享的轻量工具
    ├── bert_model/           # 标题、标题+摘要 Transformer 训练
    ├── interest_model/       # k-NN 与多兴趣模型训练
    └── data_prep/            # 三步数据构建脚本及月度缓存
```

## AI 模型

| `model_type` | 运行资产 | 输出 |
|---|---|---|
| `transformers`（默认） | Hugging Face 分类模型与 tokenizer | 正类概率 |
| `knn` | 编码器、正/负样本 embedding 与 `meta.json` | k 近邻推荐分数 |
| `multi_interest` | 编码器与可信的 `artifacts.joblib` | 逻辑回归正类概率 |

模型权重不随 Python 包发布，必须通过 `ARXIV_MODEL_PATH` 或 `model.path`
指向外部目录。`artifacts.joblib` 只能从可信来源加载。

## 依赖

仓库的基础环境使用 `python -m pip install -r requirements.txt`。本地模型推理是
可选功能，还需执行 `python -m pip install ".[arxiv-ml]"` 并提供模型文件。
