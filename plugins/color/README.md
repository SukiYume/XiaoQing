# 🎨 Color

`color` 查询 526 种中国传统色、恒星光谱色和当前聊天作用域的管理员自定义色，并可生成 PNG 色卡。

---

## ⌨️ 命令

<!-- manifest-command-aliases:start -->
| 功能 | 推荐入口 | Manifest 等价别名 |
| --- | --- | --- |
| 颜色查询与管理 | `/color` | `/颜色` `/色彩` |
<!-- manifest-command-aliases:end -->

| 参数 | 用法 | 说明 |
| --- | --- | --- |
| `-n`、`--name` | `/color -n <名称> [-p]` | 按完整名称查询 |
| `-r`、`--rgb` | `/color -r <R,G,B> [-p]` | 按 RGB 查询最接近的传统色 |
| `-x`、`--hex` | `/color -x <HEX> [-p]` | 按三位或六位 HEX 查询 |
| `-c`、`--cmyk` | `/color -c <C,M,Y,K> [-p]` | 按 CMYK 查询最接近的传统色 |
| `-a`、`--accord` | `/color -a <关键词>` | 按名称子串搜索，最多展示 20 条 |
| `-s`、`--stellar` | `/color -s <光谱型>` | 查询恒星光谱型颜色 |
| `-t`、`--spectype` | `/color -t [前缀]` | 列出或筛选光谱型 |
| `-w`、`--write` | `/color -w <名称> <RGB或HEX>` | Bot 管理员添加当前作用域自定义色 |
| `-d`、`--delete` | `/color -d <名称>` | Bot 管理员删除当前作用域自定义色 |
| `-p`、`--picture` | 与名称、RGB、HEX、CMYK 查询组合 | 附加 PNG 色卡 |
| `help`、`-h`、`-l` | `/color help` | 显示本地帮助 |

一次命令选择一个主操作。RGB 和 CMYK 接受逗号或空格分隔。

---

## 💡 示例

```text
/color -n 乳白
/color -r 255,87,51 -p
/color -x #FF5733
/color -c 0 100 100 0
/color -a 红
/color -s G2V
/color -t M
/color -w 品牌色 45 134 255
/color -d 品牌色
```

---

## 💾 数据资产

| 文件 | 内容与校验 |
| --- | --- |
| `plugins/color/color.json` | 526 条传统色；校验名称、拼音、RGB、HEX、CMYK、条目数和名称唯一性 |
| `plugins/color/stellar_colors.txt` | 105 行主序星温度网格；校验表头、行数、行长、数值范围和 HEX 格式 |

恒星色数据来自 Harre 与 Heller 的 *Digital color codes of stars*：[DOI 10.1002/asna.202113868](https://doi.org/10.1002/asna.202113868)、[arXiv:2101.06254](https://arxiv.org/abs/2101.06254)。精确光谱型查询按源表顺序显示第一条温度采样，并给出该类型的温度范围。

---

## 🎨 自定义颜色

群聊使用群作用域，私聊使用当前用户作用域。Bot 全局管理员负责写入和删除，同一作用域的成员可查询：

```text
data/color/custom_colors_group_<群号>.json
data/color/custom_colors_private_<用户号>.json
```

每个作用域最多保存 200 条、256 KiB。颜色名使用单个 1～64 字符 token。写入过程由进程锁和原子替换保护，文件结构异常时保留原文件供排障。

---

## 🔐 输入与转换

- RGB：三个 0～255 的 ASCII 整数；
- CMYK：四个 0～100 的 ASCII 整数；
- HEX：`RGB`、`#RGB`、`RRGGBB` 或 `#RRGGBB`；
- 自定义色 CMYK：按标准设备无关公式换算并四舍五入；
- 外部文本：执行长度、控制字符和参数冲突校验。

---

## 💾 图片缓存

色卡文件名由规范名称和 RGB 的 SHA-256 内容身份生成，目录为 `data/color/images/`。缓存最多 256 项、32 MiB，保留期为 30 天。图片渲染使用可选依赖 `matplotlib` 和 `numpy`，并在线程执行池中完成。

日志记录操作类型、缓存命中、数量和字节信息，查询文本、自定义名称与完整路径保留在日志边界之外。

---

## 🩺 排障

| 现象 | 检查项 |
| --- | --- |
| 提示参数冲突 | 保留一个主操作，并把 `-p` 与支持图片的查询组合 |
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
