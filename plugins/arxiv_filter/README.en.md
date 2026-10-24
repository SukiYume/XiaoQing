# 📡 arXiv Filter

[中文](README.md) | English

arXiv Filter retrieves the `astro-ph/new` source list and selects papers with a local interest model.
It sends recommendations and passes positive papers to the Codex `astro-ph` session for Chinese Markdown summaries.

## 🔐 Requirements

- Commands support private and group chats.
- The Manifest marks page retrieval and local inference dependencies as optional modules.
- A trusted model directory is necessary for complete inference.
- The loaded `codex` plugin and its `codex.enqueue_arxiv_summary` service are necessary for Codex summaries.

Install local inference dependencies:

```bash
python -m pip install ".[arxiv-ml]"
```

## ⌨️ Commands

| Command | Function |
| --- | --- |
| `/arxiv` | Retrieve and filter the current source list |
| `/arxiv help` | Plugin help |

`/论文` is an alias. `/help arxiv_filter` shows complete parameters and invalid examples.

## 📌 Source list and Codex summaries

Replies show the actual arXiv list publication date. Inference caches are separated by source date.
Codex summary tasks use this composite identity:

```text
Source-list date + normalized set of paper links
```

The same identity reuses a successful summary or active task. A changed paper set on the same date creates a new task.
If source-date validation fails, the paper-list path still returns results. The summary path skips execution and records a log entry.

Paper lists and summaries use separate message paths. The list is sent first.
Codex completes the summary in the background and sends it separately.
`config.plugins.codex.arxiv_summary` specifies the session, working directory, and methodology file.
The default session is `astro-ph`. The default methodology file is `arxiv-summary-methodology.md`.

## ⏰ Scheduled tasks

| Time | Action |
| --- | --- |
| Weekdays 10:00, 10:30, 11:00, 11:30 | Examine the list date. Filter and send when today's list appears |
| Weekdays 12:00 | Final check and source-status notification |

`data/arxiv_filter/update_status.json` records daily delivery state.
All five tasks use Core `broadcast` mode. Targets come from each schedule's `group_ids` or, when omitted, `default_group_ids`.
At least one target group is necessary for production delivery.

After a scheduled check acquires a send reservation, date reads, inference, and cancellation share one cleanup path.
That path releases the reservation. Subsequent schedules can examine the same day's list again.

## ⚙️ Plugin configuration

`plugins/arxiv_filter/config.json`:

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

| Group | Field | Description |
| --- | --- | --- |
| `model` | `path` | Plugin-relative path or absolute model directory |
| `model` | `threshold` | Fallback threshold when model configuration omits it |
| `model` | `batch_size` | Inference batch size |
| `model` | `max_len` | Maximum token length |
| `arxiv` | `url` | arXiv list page |
| `arxiv` | `proxy` | HTTP/HTTPS proxy |
| `arxiv` | `use_ssl_verify` | TLS certificate validation switch |
| `arxiv` | `timeout` | Request time limit in seconds |

`ARXIV_PROXY` can supply the proxy address.

## 💾 Model assets

Model path priority:

1. Inference CLI `--model-path`.
2. `ARXIV_MODEL_PATH`.
3. `model.path` in `plugins/arxiv_filter/config.json`.

Production can use an external absolute path:

```bash
export ARXIV_MODEL_PATH=/srv/xiaoqing-models/arxiv
```

Source deployments can also use `plugins/arxiv_filter/best_model/`.
`scripts/sync_to_remote.sh` includes this directory in production release assets.
It examines configuration, weights, tokenizer files, and SHA-256 before and after synchronization.

| `model_type` | Runtime assets | Output |
| --- | --- | --- |
| `transformers` | Hugging Face classifier and tokenizer | Positive-class probability |
| `knn` | Encoder, positive/negative sample embeddings, and `meta.json` | k-nearest-neighbor recommendation score |
| `multi_interest` | Encoder and `artifacts.joblib` | Logistic-regression positive-class probability |

Model directories must come from trusted sources.
`artifacts.joblib` is an executable serialized asset. Deployment checks must verify its source and hash.

## 🧠 Inference CLI

```bash
python scripts/arxiv_inference_cli.py \
  --model-path /srv/xiaoqing-models/arxiv \
  --input papers.csv \
  --output predictions.csv
```

CSV output uses atomic writes. `--force` explicitly permits target-file replacement. `--test-positive` supplies a single-paper smoke check.

## 💾 Data directory

```text
data/arxiv_filter/
├── update_status.json       # Scheduled delivery state
└── ...                      # Source lists and inference caches
```

Model assets use the configured path. Runtime state uses the plugin data directory.
These file groups have separate backup and deployment procedures.

Monthly training-data caches contain `papers`, the complete `query_range`, and `completed`.
Checkpoints use the same query range.
After `date_range.json` changes, months with a different range are fetched again.
`step3` merges only completed caches with matching identities in the current range.
Another `step2` run is necessary before use of old list-format caches.

## 🛠️ Code structure

```text
plugins/arxiv_filter/
├── main.py                  # Commands and schedules
├── codex_summary.py         # Codex service calls
├── arxiv_today.py           # astro-ph/new retrieval
├── arxiv_inference.py       # Inference facade
├── inference/               # Backend dispatch and implementation
├── train_model/             # Training and data preparation
├── config.json              # Plugin configuration
└── best_model/              # Source-deployment model assets
```

## 🩺 Troubleshooting

1. Use `/arxiv` to examine the source date and paper count.
2. Use CLI `--test-positive` for a model-loading check.
3. Examine the model path, `training_config.json`, weights, and tokenizer.
4. Use `/codex status astro-ph` to examine the summary task.
5. Examine logs for source date, paper-set digest, model backend, and Codex job ID.

## ✅ Development checks

```bash
python -m pytest tests/plugins/arxiv_filter/test_arxiv_filter.py tests/plugins/arxiv_filter/test_arxiv_model_path.py -q
python -m ruff check plugins/arxiv_filter scripts/arxiv_inference_cli.py
```
