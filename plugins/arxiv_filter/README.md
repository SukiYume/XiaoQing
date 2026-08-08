# 📡 arXiv Filter

arXiv Filter 获取 `astro-ph/new` 源列表，使用本地兴趣模型筛选论文，发送推荐列表，并将 positive 论文交给 Codex `astro-ph` 会话生成中文 Markdown 摘要。

---

## 🔐 使用条件

- 命令支持群聊与私聊。
- 页面抓取与本地推理依赖按 Manifest 标记为可选模块。
- 完整推理需要可信模型目录。
- Codex 摘要需要已加载 `codex` 插件和 `codex.enqueue_arxiv_summary` 服务。

安装本地推理依赖：

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

Codex 摘要任务使用以下复合身份：

```text
源列表日期 + 规范化论文链接集合
```

相同身份可复用成功摘要或运行中任务；同日论文集合变化会创建新任务。源日期校验异常时，论文列表路径继续返回结果，摘要侧路进入跳过状态并记录日志。

论文列表与摘要使用独立消息链路：列表先发送，Codex 在后台完成摘要并单独回传。摘要会话、工作目录和方法文件由 `config.plugins.codex.arxiv_summary` 配置，默认会话为 `astro-ph`，默认方法文件为 `arxiv-summary-methodology.md`。

---

## ⏰ 定时任务

| 时间 | 行为 |
|---|---|
| 工作日 10:00、10:30、11:00、11:30 | 检查源站列表日期，发现当日列表后筛选并推送 |
| 工作日 12:00 | 最终检查与源站状态通知 |

`data/arxiv_filter/update_status.json` 记录每日投递状态。调度目标来自 `default_group_ids`，生产推送请配置至少一个目标群。

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

生产环境可使用外置绝对路径：

```bash
export ARXIV_MODEL_PATH=/srv/xiaoqing-models/arxiv
```

源码部署也可使用 `plugins/arxiv_filter/best_model/`。`scripts/sync_to_remote.sh` 将该目录列为生产发布资源，并在同步前后校验配置、权重、tokenizer 与 SHA-256。

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
python scripts/arxiv_inference_cli.py \
  --model-path /srv/xiaoqing-models/arxiv \
  --input papers.csv \
  --output predictions.csv
```

输出 CSV 使用原子写入。目标文件替换通过 `--force` 显式授权。单篇冒烟可使用 `--test-positive`。

---

## 💾 数据目录

```text
data/arxiv_filter/
├── update_status.json       # 调度投递状态
└── ...                      # 源列表与推理缓存
```

模型资产位于配置路径，运行状态位于插件数据目录，两类文件采用独立备份与部署流程。

---

## 🛠️ 代码结构

```text
plugins/arxiv_filter/
├── main.py                  # 命令与调度
├── codex_summary.py         # Codex 服务调用
├── arxiv_today.py           # astro-ph/new 抓取
├── arxiv_inference.py       # 推理 facade
├── inference/               # 后端分发与实现
├── train_model/             # 训练与数据准备
├── config.json              # 插件配置
└── best_model/              # 源码部署模型资产
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
python -m ruff check plugins/arxiv_filter scripts/arxiv_inference_cli.py
```
