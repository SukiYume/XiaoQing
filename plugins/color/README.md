# 🎨 Color

`color` 查询 526 种中国传统色、恒星光谱色和当前聊天作用域的管理员自定义色，并可生成 PNG 色卡。

---

## ⌨️ 命令

<!-- manifest-command-aliases:start -->
| 功能 | 推荐入口 | Manifest 等价别名 |
| --- | --- | --- |
| 颜色查询与管理 | `/color` | `/颜色` `/色彩` |
<!-- manifest-command-aliases:end -->

| 功能 | 用法 | 说明 |
| --- | --- | --- |
| 直接查询 | `/color <名称、拼音、HEX、RGB或CMYK>` | 自动识别输入；宽泛文本会转为名称/拼音搜索 |
| 浏览目录 | `/color list [页码]` | 分页列出名称、拼音和 HEX，每页 20 种 |
| 搜索 | `/color search <关键词> [--page 页码]` | 按名称或无声调拼音搜索并分页 |
| 随机颜色 | `/color random [--picture]` | 从内置色和当前聊天自定义色中随机选择 |
| 恒星颜色 | `/color star <光谱型>` | 查询恒星光谱型颜色；直接输入 `G2V` 也可 |
| 光谱型目录 | `/color stars [筛选词] [--page 页码]` | 分页列出或筛选光谱型 |
| 添加自定义色 | `/color add <名称> <RGB或HEX>` | Bot 管理员添加当前聊天作用域自定义色 |
| 删除自定义色 | `/color delete <名称>` | Bot 管理员删除当前聊天作用域自定义色 |
| 生成色卡 | 在具体颜色查询后加 `--picture` | 附加输入色或收录色的 PNG 色卡 |
| 帮助 | `/color help` | 显示精简的常用入口 |

RGB 和 CMYK 接受逗号或空格分隔。三组整数自动按 RGB 处理，四组整数自动按 CMYK 处理。`list`、`search` 和 `stars` 的回复会显示实际总页数，并给出可直接复制执行的上一页/下一页命令。

添加自定义色时，解析器优先识别末尾完整的三整数 RGB，例如 `/color add 深蓝 0 0 255`。单独的三位 HEX 仍按 HEX 处理。

`-n/-r/-x/-c/-a/-s/-t/-w/-d` 与对应长选项均可使用；`-p` 与 `--picture` 都是图片选项。分页统一使用位置页码或 `--page`，`-l/--list` 用于浏览颜色目录。

---

## 💡 示例

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

---

## 💾 数据资产

| 文件 | 内容与校验 |
| --- | --- |
| `plugins/color/color.json` | 526 条传统色；校验名称、拼音、RGB、HEX、CMYK、条目数和名称唯一性 |
| `plugins/color/stellar_colors.txt` | 105 行主序星温度网格；校验表头、行数、行长、数值范围和 HEX 格式 |

恒星色数据来自 Harre 与 Heller 的 *Digital color codes of stars*：[DOI 10.1002/asna.202113868](https://doi.org/10.1002/asna.202113868)、[arXiv:2101.06254](https://arxiv.org/abs/2101.06254)。精确光谱型查询按源表顺序显示第一条温度采样，并给出该类型的温度范围；光谱型目录每页显示 30 个。

---

## 🎨 自定义颜色

群聊使用群作用域，私聊使用当前用户作用域。Bot 全局管理员负责写入和删除，同一作用域的成员可查询：

```text
data/color/custom_colors_group_<群号>.json
data/color/custom_colors_private_<用户号>.json
```

每个作用域最多保存 200 条、256 KiB。颜色名为 1～64 个字符；包含空格时使用引号，例如 `/color add "品牌 蓝" #3366ff`。写入过程由进程锁和原子替换保护，文件结构异常时保留原文件供排障。

---

## 🔐 输入与转换

- RGB：三个 0～255 的 ASCII 整数；
- CMYK：四个 0～100 的 ASCII 整数；
- HEX：`RGB`、`#RGB`、`RRGGBB` 或 `#RRGGBB`；
- 自定义色 CMYK：按标准设备无关公式换算并四舍五入；
- RGB/HEX 未精确命中时：在 D65 CIE L\*a\*b\* 中按 CIE76 色差寻找最接近的收录色；
- CMYK 未精确命中时：先按标准设备无关近似转换到 sRGB，再执行同一感知色差匹配；
- 外部文本：执行长度、控制字符和参数冲突校验。

---

## 💾 图片缓存

色卡文件名由规范名称和 RGB 的 SHA-256 内容身份生成，目录为 `data/color/images/`。缓存最多 256 项、32 MiB，保留期为 30 天。图片渲染使用可选依赖 `matplotlib` 和 `numpy`，并在线程执行池中完成。

日志记录操作类型、缓存命中、数量和字节信息，查询文本、自定义名称与完整路径保留在日志边界之外。

---

## 🩺 排障

| 现象 | 检查项 |
| --- | --- |
| 不知道颜色名称 | 使用 `/color list` 浏览，或 `/color search <关键词>` 搜索名称/拼音 |
| 提示参数冲突 | 优先使用直接查询或一个可读子命令，并把 `-p` 放在具体颜色查询之后 |
| 页码超出范围 | 按回复中的实际总页数重试；颜色搜索使用 `--page` |
| 自定义色写入被拒绝 | 核对 Bot 全局管理员身份、作用域 ID、名称和容量上限 |
| 色卡生成失败 | 安装 `matplotlib` 与 `numpy`，检查数据目录写权限 |
| 资源校验失败 | 核对发行版中的 `color.json` 和 `stellar_colors.txt` |

---

## ✅ 开发验证

在仓库根目录运行：

```bash
python -m pytest -q tests/plugins/color/test_color_plugin.py \
  tests/plugins/contracts/test_dict_color_contracts.py
python -m ruff check plugins/color
python -m mypy plugins/color
```
