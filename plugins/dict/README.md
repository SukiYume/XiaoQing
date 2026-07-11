# 天文学中英词典

词典数据随发行包安装，无需用户另行下载：

- `assets/astrodict_ec.txt`：英译中
- `assets/astrodict_ce.txt`：中译英
- `assets/manifest.json`：代码和文档共享的数据文件、格式、来源与许可说明

命令：

- `/dict galaxy`：模糊查询
- `/dict 星系`：自动中译英
- `/dict -e galaxy`：精确匹配
- `/dict -n 20 galaxy`：最多显示 20 条（上限 100）
- `/dict help`

文件是 UTF-8、无表头、TAB 分隔的两列文本。发行包缺失 manifest 中任一文件时，插件会返回明确的安装资源错误。
