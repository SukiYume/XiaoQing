# 📖 Chinese–English Astronomy Dictionary

[中文](README.md) | English

`dict` uses the supplied `r241020` release of the “英汉天文学名词数据库” for offline Chinese–English queries in both directions.

## ⌨️ Commands

| Function | Primary command | Manifest aliases |
| --- | --- | --- |
| Astronomy dictionary | `/dict` | `/词典` `/字典` |

| Usage | Description |
| --- | --- |
| `/dict galaxy` | English to Chinese. Complete words and prefixes first |
| `/dict 星系` | Chinese to English. Complete words and prefixes first |
| `/dict "fast radio burst" --exact` | Complete source terms only. Suggestions when no match exists |
| `/dict galaxy --page 2` | Page 2 |
| `/dict star --size 20 --page 3` | Page 3, with 20 entries per page |
| `/dict -- -example` | Text after `--` is the query term |
| `/dict help` | Local help |

Enter a term directly. Options limit results or select pages.
`--exact`, `--page`, and `--size` can precede or follow the term.
`-e` means `--exact`.

`-p` means `--page`. `-n`, `--num`, and `--page-size` mean `--size`.
The default page size is 10 entries.
Results show query direction, total count, current page, and page count.

They include commands for the previous and next pages.
Each page command includes all query conditions. It operates independently of user or group session state.

Fuzzy queries split input at whitespace. All keywords must occur in the same source term.
Results use this priority: literal complete match, case-insensitive complete match, prefix match, word-boundary match, and substring match.
Shorter terms come first within each rank. For example, `/dict galaxy` first returns `galaxy → 星系`.
An exact query without a complete match shows up to five fuzzy suggestions and a normal query command.

Queries with CJK unified ideographs use Chinese-to-English data. All other queries use English-to-Chinese data.
`help` and `帮助` open help. `list`, `l`, and `h` are ordinary query terms.
For a term that starts with a hyphen, put `--` before the term to end option parsing.

## 🔐 Input limits

- Complete arguments: 512 characters.
- Query term: 256 characters.
- Page size: ASCII integer from 1 to 100.
- Page number: ASCII integer from 1 to 50,000. Validated again against the actual page count.
- Quotes: paired shell-style rules.
- Strict checks: argument categories, repeated options, control characters, and resource fields.

## 💾 Release assets

| File | Content |
| --- | --- |
| `plugins/dict/assets/astrodict_ec.txt` | English to Chinese. 30,094 mapping lines |
| `plugins/dict/assets/astrodict_ce.txt` | Chinese to English. 26,770 mapping lines |
| `plugins/dict/assets/manifest.json` | Source version, filenames, byte counts, line counts, and SHA-256 |

Dictionary files use UTF-8, LF line endings, and two TAB-separated columns.
The loader first examines the manifest, file type, size, and SHA-256.
It then validates UTF-8, column count, line count, field lengths, control characters, and duplicate records.
Parsed results are cached by file identity. Resource changes create a new cache generation.

## 📚 Source and terms of use

- [Official entry point](https://nadc.china-vo.org/astrodict/)
- [Fixed source package](https://nadc.china-vo.org/astrodict/s/2024/astrodict_241020.zip)
- [Terms of use](https://nadc.china-vo.org/astrodict/article/download)

The Chinese Astronomical Society owns the dictionary.
The published terms permit free downloads, integration, further development, and format conversion.
Separate permission is necessary for commercial use, dictionary-content changes, and redistribution after integration.
The recommended credit identifies “英汉天文学名词数据库” as supplied by the Chinese Astronomical Society's astronomy terminology committee.
The official terms determine the permitted scope of use.

## ⏰ Lifecycle

The first query validates and loads the dictionary. Runtime access is read-only.
The plugin uses the standard library for local parsing. The supplied dictionary files contain all runtime resources.

## 🩺 Troubleshooting

| Symptom | Check |
| --- | --- |
| Resource validation failure | Examine the integrity and versions of all three release assets. |
| Empty results | Shorten the term, examine spelling, or remove `--exact`, as the reply suggests. |
| Page outside range | Copy the last-page command from the reply. |
| Invalid arguments | Examine quotes, `--page`/`--size` values, repeated options, and the position of `--`. |

## ✅ Development checks

Run these commands from the repository root:

```bash
python -m pytest -q tests/plugins/dict/test_dict.py
python -m ruff check plugins/dict
python -m mypy plugins/dict
```
