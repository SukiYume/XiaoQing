# ADS Paper

[中文](README.md)

ADS Paper supplies NASA ADS searches and personal research records through `/paper`.
Searches, citation networks, and related papers work in group chats and private chats.
Notes, ideas, topics, deadlines, daily recommendations, and libraries use the private-chat user scope.

## Requirements

A NASA ADS token is necessary for searches and BibTeX requests.
`aiohttp` sends ADS API requests.
The shared AI `summary` route supplies an enhanced Chinese summary.

## Commands

| Command | Chat scope | Function |
| --- | --- | --- |
| `/paper search <keywords>` | Group and private | Search keywords. |
| `/paper author <author>` | Group and private | Search an author. |
| `/paper cite <ID>` | Group and private | Get BibTeX. |
| `/paper cite-network <ID>` | Group and private | Get references and citations. |
| `/paper related <ID>` | Group and private | Get related papers. |
| `/paper summarize <ID>` | Group and private | Get a Chinese summary or ADS abstract. |
| `/paper note ...` | Private | Add, remove, or read paper notes. |
| `/paper writing ...` | Private | Add, remove, or read ideas. |
| `/paper topics ...` | Private | Manage daily topics. |
| `/paper deadline ...` | Private | Manage deadlines. |
| `/paper daily` | Private | Get new papers for the current UTC date. |
| `/paper ref_add <ID>` | Private | Add a personal BibTeX entry. |
| `/paper refs` | Private | Show the personal library. |
| `/paper help` | Group and private | Show help. |

`ID` accepts an arXiv ID, arXiv URL, or ADS bibcode.
`/help ads_paper` shows all subcommands and error examples.

## Settings and credentials

Set the ADS token in `config/secrets.json`:

```json
{
  "plugins": {
    "ads_paper": {
      "ads_token": "NASA_ADS_TOKEN"
    }
  }
}
```

Set the summary route in `config/config.json`:

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

Project provider addresses are in `config.ai.providers`.
Model profiles are in `config.ai.models`. API keys are in `secrets.ai.providers`.
The ADS token supplies paper searches. The `summary` route supplies Chinese summaries.
The plugin uses route availability to select the summary level.

## Data and permissions

These files are in `data/ads_paper/`:

| File | Contents |
| --- | --- |
| `paper_notes.json` | Paper notes, separate for each QQ user. |
| `writing_ideas.json` | Ideas for writing. |
| `research_topics.json` | Daily topics. |
| `deadlines.json` | Deadlines. |
| `references_<user_id>.bib` | Personal BibTeX library. |

JSON writes are atomic.
A parse error keeps the source and creates a `.corrupt-*` copy with a content digest in its name.
The write path enters a protected state. Service continues after data recovery.
The BibTeX scanner parses entries structurally.
The scanner parses entries with `@` in email addresses, URLs, comments, and field values.

`/paper daily` selects records with ADS `entdate` on the current UTC date.
Results use descending `entdate` and ascending bibcode order.
`/paper author` shows recent papers in descending ADS `date` order.
The citation-network label `本次展示参考文献` refers to at most five returned records.
Citation totals come from the ADS `citation_count` field.

## Fault isolation

1. Send `/paper search "fast radio burst"` to test the ADS token and network.
2. Send `/paper cite 2401.12345` to test ID parsing and BibTeX.
3. Send `/paper summarize 2401.12345` to examine the current summary route.
4. Examine ADS status codes, route profiles, storage digests, and request IDs in the log.
5. Examine the `.corrupt-*` files manually.
6. Recover the data.

## Verification

```bash
python -m pytest tests/plugins/ads_paper/test_ads_paper.py -q
python -m ruff check plugins/ads_paper
```
