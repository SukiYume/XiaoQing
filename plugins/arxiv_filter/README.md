# arXiv Filter 插件

基于 BERT 模型自动筛选每日 arXiv 天体物理论文，推荐你可能感兴趣的论文。

## 功能

- 每日定时从 arXiv `astro-ph/new` 页面获取最新论文
- 使用 SciBERT 模型（标题 + 摘要作为输入）进行二分类，筛选感兴趣的论文
- 支持定时检查 arXiv 更新，自动推送筛选结果
- 筛选出 positive 论文后，自动把全部 arXiv 链接交给 Codex `astro-ph` 会话生成中文 Markdown 摘要

## 使用方法

```
/arxiv       # 获取今日筛选的论文
/arxiv help  # 显示帮助信息
```

## 定时任务

在 `plugin.json` 中配置的定时任务：

| 时间 | 行为 |
|------|------|
| 周一~周五 10:00 / 10:30 / 11:00 / 11:30 | 检查 arXiv 是否更新到当天，更新则执行筛选并推送 |
| 周一~周五 12:00 | 最后一次检查，若仍未更新则发送停更通知 |

每天只推送一次（通过 `data/update_status.json` 去重）。

论文列表推送和 Codex 摘要是两条独立消息链路：arXiv Filter 会先发送筛选出的论文列表，然后在后台把所有 positive 论文链接交给 Codex 插件；Codex 完成后再单独回发摘要。如果 Codex 总结失败，失败消息由 Codex 插件单独发送，不会阻止论文列表消息。

如果运行环境中 Codex 摘要模块不可用或加载失败，arXiv Filter 仍会正常筛选并发送论文列表；摘要侧路只记录日志并跳过，不影响 `/arxiv` 和定时任务。

手动 `/arxiv` 会重新执行筛选并再次请求 Codex 处理当天链接。Codex 插件会自行判断当天是否已经成功总结过：成功过则重发历史摘要，失败过或没有成功记录则重新总结。

摘要会话、工作目录和方法论文件名由 `plugins.codex.arxiv_summary` 配置控制，默认使用 `astro-ph` 会话和 `arxiv-summary-methodology.md`。

## 配置

插件自带 `config.json`：

```json
{
    "model": {
        "path": "best_model",
        "threshold": 0.5,
        "batch_size": 32,
        "max_len": 256
    },
    "arxiv": {
        "url": "https://arxiv.org/list/astro-ph/new",
        "proxy": null,
        "api_days": 2,
        "use_ssl_verify": true,
        "timeout": 30
    }
}
```

### 配置项说明

| 分组 | 字段 | 说明 |
|------|------|------|
| model | `path` | 模型目录（相对于插件目录） |
| model | `threshold` | 正类概率阈值 |
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
├── arxiv_inference.py        # 模型推理
├── arxiv_today.py            # arXiv 数据获取（网页爬取 + API）
├── utils.py                  # 公共工具（配置加载）
├── config.json               # 插件配置
├── plugin.json               # 插件元数据
├── data/                     # 运行时数据（更新状态等）
└── train_model/              # 仅仓库开发使用，不进入 PyPI 产物
    ├── run_all.py            # 一键构建训练数据集
    ├── step1_extract_positive_ids.py
    ├── step2_fetch_all_astro_ph.py
    ├── step3_build_dataset.py
    ├── arxiv_class_v2.py     # 训练脚本（标题+摘要）
    └── cache/                # 月度论文缓存
```

## AI 模型

- 基座模型: SciBERT (`allenai/scibert_scivocab_cased`)
- 输入: 标题 (Segment A) + 摘要 (Segment B)，`max_len=256`
- 任务: 二分类（感兴趣 / 不感兴趣）
- 损失函数: Focal Loss (γ=4) + 类别加权 + WeightedRandomSampler
- 输出: 正类概率 (0-1)
- 发布边界: 模型权重不随 Python 包发布，必须通过 `ARXIV_MODEL_PATH` 或
  `model.path` 指向外部目录

## 依赖

```bash
pip install torch transformers pandas requests beautifulsoup4 feedparser urllib3
```

详见 `requirements.txt`。
