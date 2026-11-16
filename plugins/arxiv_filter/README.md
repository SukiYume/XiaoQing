# 📡 arXiv Filter

arXiv Filter 获取 `astro-ph/new` 源列表，使用本地兴趣模型筛选论文，发送推荐列表，并将 positive 论文交给 Codex `astro-ph` 会话生成中文 Markdown 摘要。

---

## 🔐 使用条件

- 命令支持群聊与私聊。
- 页面抓取与本地推理依赖（`requests`、`bs4`、`torch` 等）在 Manifest 中均为可选依赖：缺失时插件仍会加载，`/arxiv` 返回无法加载依赖的提示和安装命令。
- 完整推理需要可信模型目录。
- Codex 摘要需要已加载 `codex` 插件和 `codex.enqueue_arxiv_summary` 服务。

安装页面抓取与本地推理依赖：

```bash
python -m pip install ".[arxiv-ml]"
```

---

## ⌨️ 命令

| 命令 | 功能 |
|---|---|
| `/arxiv` | 获取源站当前列表并执行筛选 |
| `/arxiv help` | 插件帮助 |

别名为 `/论文`。完整参数与错误样例可通过 `/help arxiv_filter` 查看。

---

## 📌 源列表与 Codex 摘要

回复显示 arXiv 源站列表的实际发布日期。推理缓存按源日期隔离。

论文列表与摘要使用独立消息链路：列表先发送，Codex 在后台完成摘要并单独回传。摘要任务以“源列表日期 + 规范化论文链接集合”为身份。任务的复用与重建规则，以及摘要会话、工作目录和方法文件的配置（`config.plugins.codex.arxiv_summary`），见 [Codex 插件说明](../codex/README.md)的“arXiv 摘要会话”一节。

无法确认源列表日期时，论文列表照常返回，但不提交 Codex 摘要任务，日志记录原因。

---

## ⏰ 定时任务

时间按 `config.timezone`（默认 `Asia/Shanghai`）计。

| 时间 | 行为 |
|---|---|
| 工作日 10:00、10:30、11:00、11:30 | 检查源站列表日期，发现当日列表后筛选并推送 |
| 工作日 12:00 | 最终检查与源站状态通知 |

`data/arxiv_filter/update_status.json` 记录每日投递状态。五个任务采用 Core `broadcast` 模式，目标群来自部署配置 `config.plugins.arxiv_filter.schedules.<任务 ID>.group_ids`，没写时使用 `default_group_ids`。请配置至少一个目标群，否则定时检查没有投递对象。

定时检查开始发送前会先占用当天的发送权。检查中途因日期读取失败、推理失败或任务取消而结束时，占用会被释放，后续定时检查可以继续处理同一天的列表。

---

## ⚙️ 插件配置

`plugins/arxiv_filter/config.json`：

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

| 分组 | 字段 | 说明 |
|---|---|---|
| `model` | `path` | 插件相对路径或绝对模型目录 |
| `model` | `threshold` | 模型配置缺省阈值的后备值 |
| `model` | `batch_size` | 推理批大小 |
| `model` | `max_len` | 最大 token 长度 |
| `arxiv` | `url` | arXiv 列表页 |
| `arxiv` | `proxy` | HTTP/HTTPS 代理 |
| `arxiv` | `use_ssl_verify` | TLS 证书校验开关 |
| `arxiv` | `timeout` | 请求秒数预算 |

`ARXIV_PROXY` 可提供代理地址。

---

## 💾 模型资产

模型路径优先级：

1. 推理 CLI 的 `--model-path`
2. `ARXIV_MODEL_PATH`
3. `plugins/arxiv_filter/config.json` 的 `model.path`
4. 以上都没有时为 `plugins/arxiv_filter/best_model/`

模型目录可放在仓库之外，用绝对路径指定：

```bash
export ARXIV_MODEL_PATH=/srv/xiaoqing-models/arxiv
```

源码部署也可使用 `plugins/arxiv_filter/best_model/`。使用仓库的同步脚本 `scripts/sync_to_remote.sh` 部署时，该目录会随发布一并同步，并在同步前后校验配置、权重、tokenizer 与 SHA-256。

支持的后端：

| `model_type` | 运行资产 | 输出 |
|---|---|---|
| `transformers` | Hugging Face 分类模型与 tokenizer | 正类概率 |
| `knn` | 编码器、正负样本 embedding 与 `meta.json` | k 近邻推荐分数 |
| `multi_interest` | 编码器与 `artifacts.joblib` | 逻辑回归正类概率 |

模型目录来自可信来源。`artifacts.joblib` 属于可执行序列化资产，部署时应复核来源与哈希。

---

## 🧠 推理 CLI

```bash
python -m plugins.arxiv_filter.inference \
  --model-path /srv/xiaoqing-models/arxiv \
  --output predictions.csv
```

CLI 拉取当天的 arXiv 列表并推理。输出 CSV 使用原子写入。目标文件替换通过 `--force` 显式授权。单篇冒烟可使用 `--test-positive`。

---

## 💾 数据目录

```text
data/arxiv_filter/
├── update_status.json       # 调度投递状态
└── claim-<日期>             # 当天定时推送的占用标记，超过 1 小时视为遗留
```

模型资产位于配置路径，运行状态位于插件数据目录，两类文件采用独立备份与部署流程。

---

## 🏋️ 训练数据准备

`train_model/data_prep/` 的月度缓存保存 `papers`、完整 `query_range` 和 `completed`，断点也绑定相同查询范围。修改 `date_range.json` 后，范围不匹配的月份会重新抓取。`step3` 只合并当前范围内已完成且身份匹配的缓存。

---

## 🛠️ 代码结构

```text
plugins/arxiv_filter/
├── main.py                  # 命令与调度
├── codex_summary.py         # Codex 服务调用
├── arxiv_today.py           # astro-ph/new 抓取
├── numerics.py              # 训练与推理共用的数值函数
├── utils.py                 # 插件配置与推理结果格式的公共工具
├── inference/
│   ├── runner.py            # 推理入口，按 model_type 分发后端
│   ├── shared.py            # 模型路径解析、参数构建等公共逻辑
│   ├── transformers_backend.py
│   ├── knn_backend.py
│   └── multi_interest_backend.py
├── train_model/
│   ├── bert_model/          # transformers 分类模型训练
│   ├── interest_model/      # k-NN 与多兴趣模型训练
│   ├── data_prep/           # 训练数据准备（step1～step3、run_all.py）
│   └── training_common.py   # 训练共用函数
├── config.json              # 插件配置
├── best_model/              # 默认模型资产（不入库）
└── best_model_*/            # 各训练脚本的输出目录（不入库）
```

---

## 🩺 排障

1. 使用 `/arxiv` 检查源列表日期和论文数量。
2. 使用推理 CLI 的 `--test-positive` 检查模型加载。
3. 核对模型路径、`training_config.json`、权重和 tokenizer。
4. 通过 `/codex status astro-ph` 查看摘要任务。
5. 检查日志中的源日期、论文集合摘要、模型后端和 Codex job ID。

---

## ✅ 开发验证

```bash
python -m pytest tests/plugins/arxiv_filter/test_arxiv_filter.py tests/plugins/arxiv_filter/test_arxiv_model_path.py -q
python -m ruff check plugins/arxiv_filter
```
