# 🎨 Color

[中文](README.md) | English

`color` queries 526 traditional Chinese colors, stellar spectral colors, and administrator-defined colors in the current chat scope.
It can also create PNG color cards.

## ⌨️ Commands

| Function | Primary command | Manifest aliases |
| --- | --- | --- |
| Color queries and management | `/color` | `/颜色` `/色彩` |

| Function | Usage | Description |
| --- | --- | --- |
| Direct query | `/color <name, pinyin, HEX, RGB, or CMYK>` | Detect input type. Broad text becomes a name/pinyin search |
| Catalog | `/color list [page]` | Names, pinyin, and HEX. 20 colors per page |
| Search | `/color search <keyword> [--page number]` | Search names or toneless pinyin with pagination |
| Random color | `/color random [--picture]` | Select from built-in and current-scope custom colors |
| Stellar color | `/color star <spectral-type>` | Query a spectral type. Direct `G2V` input also works |
| Spectral catalog | `/color stars [filter] [--page number]` | List or filter spectral types with pagination |
| Add custom color | `/color add <name> <RGB-or-HEX>` | Bot administrators add a color in the current chat scope |
| Delete custom color | `/color delete <name>` | Bot administrators delete a color in the current chat scope |
| Color card | Add `--picture` after a specific color query | Attach a PNG for the input or stored color |
| Help | `/color help` | Show common entry points |

RGB and CMYK accept comma or space separators. Three integers select RGB. Four integers select CMYK.
`list`, `search`, and `stars` show the actual page count and commands for the previous and next pages.

For custom-color additions, the parser first detects a complete final three-integer RGB value, such as `/color add 深蓝 0 0 255`.
A single three-digit HEX value keeps HEX interpretation.

`-n/-r/-x/-c/-a/-s/-t/-w/-d` and their corresponding long options are available.
`-p` and `--picture` both select images. Pagination uses a positional page number or `--page`.
`-l/--list` opens the color catalog.

## 💡 Examples

```text
/color 乳白
/color rubai
/color #F9F4DC
/color 249 244 221 --picture
/color list 2
/color search 红
/color search hong --page 2
/color random
/color G2V
/color stars M
/color add "品牌 蓝" 45 134 255
/color delete "品牌 蓝"
```

## 💾 Data assets

| File | Content and validation |
| --- | --- |
| `plugins/color/color.json` | 526 traditional colors. Name, pinyin, RGB, HEX, CMYK, count, and unique-name checks |
| `plugins/color/stellar_colors.txt` | 105-row main-sequence temperature grid. Header, count, row length, range, and HEX checks |

Stellar colors come from Harre and Heller, *Digital color codes of stars*:
[DOI 10.1002/asna.202113868](https://doi.org/10.1002/asna.202113868), [arXiv:2101.06254](https://arxiv.org/abs/2101.06254).
An exact spectral-type query shows the first temperature sample in source-table order and the temperature range for that type.
The spectral catalog shows 30 types per page.

## 🎨 Custom colors

Group chats use group scope. Private chats use the current user's scope.
Only `add` and `delete`, including aliases such as `-w` and `-d`, change stored colors.
Their `plugin.json` declarations specify Bot administrator permission.

Core examines this permission.
A `-w` after other arguments keeps argument status. Members of the same scope can query its colors:

```text
data/color/custom_colors_group_<group-id>.json
data/color/custom_colors_private_<user-id>.json
```

Each scope permits 200 entries and 256 KiB. Color names contain 1–64 characters.
Quotes are necessary for names with spaces, for example `/color add "品牌 蓝" #3366ff`.
A process lock and atomic replacement protect writes. Invalid file structures remain available for troubleshooting.

## 🔐 Input and conversion

- RGB: three ASCII integers from 0 to 255.
- CMYK: four ASCII integers from 0 to 100.
- HEX: `RGB`, `#RGB`, `RRGGBB`, or `#RRGGBB`.
- Custom-color CMYK: rounded conversion with the standard device-independent formula.
- RGB/HEX without an exact match: nearest stored color by CIE76 difference in D65 CIE L\*a\*b\*.
- CMYK without an exact match: standard device-independent approximation to sRGB, followed by the same perceptual color-difference match.
- External text: length, control-character, and conflicting-argument checks.

## 💾 Image cache

Color-card filenames use a SHA-256 content identity from the canonical name and RGB.
Files are in `data/color/images/`. The cache permits 256 entries and 32 MiB, with a 30-day retention period.
Image rendering uses optional `matplotlib` and `numpy` dependencies in the thread executor.

Logs contain operation type, cache hits, counts, and bytes.
Log filtering excludes query text, custom names, and complete paths.

## 🩺 Troubleshooting

| Symptom | Check |
| --- | --- |
| Unknown color name | Browse `/color list`, or search names/pinyin with `/color search <keyword>`. |
| Conflicting arguments | Use a direct query or one readable subcommand. Put `-p` after a specific color query. |
| Page outside range | Use the actual page count in the reply. Color searches use `--page`. |
| Rejected custom-color write | Examine administrator identity, scope ID, name, and capacity limits. |
| Color-card failure | Install `matplotlib` and `numpy`. Examine data-directory write permissions. |
| Resource validation failure | Examine the supplied `color.json` and `stellar_colors.txt`. |

## ✅ Development checks

Run these commands from the repository root:

```bash
python -m pytest -q tests/plugins/color/test_color_plugin.py
python -m ruff check plugins/color
python -m mypy plugins/color
```
