# 🔭 Astro Tools 天文工具箱

Astro Tools 通过 `/astro` 提供天文时间、坐标、单位、红移、公式、天体和常数查询。

---

## 🔐 使用条件

- 命令支持群聊与私聊。
- Astropy 提供时间、坐标、单位和宇宙学计算。
- SciPy 提供宇宙学距离与时间积分。
- SIMBAD 查询需要外部网络。

---

## ⌨️ 命令

| 命令 | 功能 |
|---|---|
| `/astro time ...` | ISO、JD、MJD、Unix 和当前时间 |
| `/astro coord ...` | ICRS、Galactic 和 geocentric true ecliptic 坐标 |
| `/astro convert <值> <源单位> <目标单位>` | Astropy 物理单位转换 |
| `/astro redshift <z>` | Planck18 距离与宇宙年龄，范围 `0 ≤ z ≤ 1100` |
| `/astro formula ...` | 公式查询与质量相关计算 |
| `/astro obj <名称>` | 太阳系资料或 SIMBAD 对象查询 |
| `/astro const [名称]` | 天文与物理常数 |
| `/astro help` | 插件帮助 |

示例：

```text
/astro time now
/astro time unix 1706616000
/astro coord 12:30:00 +15:00:00
/astro coord galactic 120 30
/astro convert 1 pc ly
/astro redshift 0.5
/astro obj M31
/astro const ly
```

完整公式名、常数别名、参数边界和错误样例可通过 `/help astro_tools` 查看。

---

## ⚙️ 配置与数据

插件使用 Core 时区和共享 HTTP Session。运行结果即时计算，持久数据目录仅用于框架统一路径所有权。

SIMBAD 请求采用客户端 timeout 与总 deadline。输入数值、坐标、单位和红移在计算前完成范围校验。

---

## 🩺 排障

1. 使用 `/astro const c` 验证基础命令。
2. 使用 `/astro time now` 验证 Astropy。
3. 使用 `/astro redshift 0.5` 验证 SciPy 宇宙学积分。
4. 使用 `/astro obj M31` 验证 SIMBAD 网络。
5. 检查日志中的解析字段、单位错误和外部请求状态。

---

## ✅ 开发验证

```bash
python -m pytest tests/plugins/astro_tools/test_astro_tools.py -q
python -m ruff check plugins/astro_tools tests/plugins/astro_tools/test_astro_tools.py
```
