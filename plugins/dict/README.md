# 📖 天文学中英词典

`dict` 使用随发行版提供的“英汉天文学名词数据库”`r241020` 数据，离线完成中英双向查询。

---

## ⌨️ 命令

<!-- manifest-command-aliases:start -->
| 功能 | 推荐入口 | Manifest 等价别名 |
| --- | --- | --- |
| 天文学词典 | `/dict` | `/词典` `/字典` |
<!-- manifest-command-aliases:end -->

| 用法 | 说明 |
| --- | --- |
| `/dict galaxy` | 英译中模糊查询 |
| `/dict 星系` | 中译英模糊查询 |
| `/dict -e "fast radio burst"` | 精确匹配完整源词 |
| `/dict -n 20 star` | 设置结果数量，范围为 1～100 |
| `/dict -- -example` | 把 `--` 后的内容作为查询词 |
| `/dict help` | 显示本地帮助 |

`-e`、`--exact` 和 `-n`、`--num` 可位于查询词前后。模糊查询会按空白分词，并要求全部关键词出现在同一个源词中。包含中日韩统一表意文字的查询使用中译英数据，其余查询使用英译中数据。

---

## 🔐 输入边界

- 整体参数长度上限为 512 个字符；
- 查询词长度上限为 256 个字符；
- 结果数量接受 1～100 的 ASCII 整数；
- 引号采用 Shell 风格配对规则；
- 参数类别、重复选项、控制字符和资源字段均执行严格校验。

---

## 💾 发行资产

| 文件 | 内容 |
| --- | --- |
| `plugins/dict/assets/astrodict_ec.txt` | 英译中，30,094 行映射 |
| `plugins/dict/assets/astrodict_ce.txt` | 中译英，26,770 行映射 |
| `plugins/dict/assets/manifest.json` | 来源版本、文件名、字节数、行数和 SHA-256 |

词典文件采用 UTF-8、LF 换行和 TAB 分隔的两列格式。加载器先核对清单、文件类型、大小与 SHA-256，再校验 UTF-8、列数、行数、字段长度、控制字符和重复记录。解析结果按文件身份缓存，资源变化会建立新缓存代次。

---

## 📚 来源与使用约定

- [官方入口](https://nadc.china-vo.org/astrodict/)
- [固定来源包](https://nadc.china-vo.org/astrodict/s/2024/astrodict_241020.zip)
- [开放使用约定](https://nadc.china-vo.org/astrodict/article/download)

词库所有权归中国天文学会所有。官方约定授权社会免费下载、集成、二次开发和格式转换；商业营利、词库内容修改和整合后二次发布需要另行取得授权。引用建议注明：“英汉天文学名词数据库”由中国天文学会天文学名词审定委员会提供。实际使用范围以官方约定为准。

---

## ⏰ 生命周期

词典在首次查询时校验并加载，运行期间保持只读。插件使用标准库完成本地解析，运行时资源由发行词典文件完整提供。

---

## 🩺 排障

| 现象 | 检查项 |
| --- | --- |
| 资源校验失败 | 核对三个发行资产的完整性与版本 |
| 查询结果为空 | 检查查询方向、拼写和精确匹配选项 |
| 参数格式错误 | 检查引号、`-n` 数值、重复选项与 `--` 位置 |

---

## ✅ 开发验证

在仓库根目录运行：

```bash
python -m pytest -q tests/plugins/dict/test_dict.py \
  tests/plugins/contracts/test_dict_color_contracts.py
python -m ruff check plugins/dict
python -m mypy plugins/dict
```
