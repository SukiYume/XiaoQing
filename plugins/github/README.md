# 🐙 GitHub Trending

`github` 读取 [GitHub 官方 Trending 页面](https://github.com/trending)，展示每日、每周和每月热门仓库。

---

## 🔐 使用条件

- 命令支持群聊与私聊。
- 必需依赖为 `beautifulsoup4`（模块名 `bs4`），随 `python -m pip install -e ".[plugins]"` 安装；缺少时插件不会加载，日志记录缺失的模块名。
- 网络需要访问 `https://github.com`，也可通过可选代理，见“可选代理”。
- 定时推送需要至少一个目标群，配置方式见“定时任务与历史”。

---

## ⌨️ 命令

<!-- manifest-command-aliases:start -->
| 功能 | 推荐入口 | Manifest 等价别名 |
| --- | --- | --- |
| GitHub Trending | `/github` | `/gh` `/trending` |
<!-- manifest-command-aliases:end -->

| 用法 | 说明 |
| --- | --- |
| `/github` | 查看每日趋势 |
| `/github daily` | 查看每日趋势 |
| `/github weekly` | 查看每周趋势 |
| `/github monthly` | 查看每月趋势 |
| `/github help` | 显示本地帮助 |

`h` 和 `帮助` 是 `help` 的别名。命令接受一个完整的时间范围参数。

---

## 📌 抓取与输出

插件解析 GitHub 官方 HTML，并执行以下边界：

- 直连目标限定为 `https://github.com`；
- 响应经过 MIME、同源重定向、压缩比例和 2 MiB 解码预算校验；
- 文本编码接受 UTF-8 与 ASCII；
- 单次最多解析 50 个文章节点；
- 仓库链接采用 GitHub 的两段仓库路径；
- 描述、语言、计数字段和控制字符分别经过规范化；
- QQ 消息最多展示 10 个完整仓库块，并受 Core 单条文本长度保护。

---

## ⏰ 定时任务与历史

Manifest 每天 08:30（按 `config.timezone` 计，默认 `Asia/Shanghai`）运行 `scheduled`（任务 ID `daily`），推送每日榜单。任务使用 Core `broadcast` 模式，目标群取自部署配置 `config.plugins.github.schedules.daily.group_ids`，没写时使用 `default_group_ids`；请配置至少一个目标群，否则没有投递对象。每次成功抓取（含手动命令）后，插件写入以下文件，`<range>` 为 `daily`、`weekly` 或 `monthly`：

```text
data/github/trending_<range>_latest.json
data/github/history/trending_<range>_<YYYY-MM-DD>.json
```

`latest` 提供当前指针，日期文件提供历史快照。每个时间范围最多保留 90 份常规历史文件。写入和保留策略由进程内锁按顺序保护。

---

## 📌 可选代理

代理是插件唯一的私密配置项，可省略。在 `config/secrets.json` 中配置：

```json
{
  "plugins": {
    "github": {
      "proxy": "http://proxy.example.com:8080"
    }
  }
}
```

代理地址接受结构完整的 HTTP 或 HTTPS URL。配置代理后，GitHub 域名解析由代理完成，请只使用可信代理。日志不记录代理账号密码。

---

## ⏰ 生命周期

每次命令和定时任务执行一次完整抓取事务。直连请求使用 Core 的安全公网抓取器；代理请求复用 Core 管理的 HTTP 会话。插件运行状态由历史文件和当前事务组成。

---

## 🩺 排障

| 现象 | 检查项 |
| --- | --- |
| 页面抓取失败 | 检查 GitHub 连通性、代理配置和运行日志 |
| 返回空列表 | 检查 Trending 页面结构与 `article.Box-row` 解析规则 |
| 历史写入失败 | 检查 `data/github/` 权限和磁盘空间 |
| 时间范围错误 | 使用 `daily`、`weekly` 或 `monthly` |

---

## ✅ 开发验证

在仓库根目录运行：

```bash
python -m pytest -q tests/plugins/github/test_github.py
python -m ruff check plugins/github
python -m mypy plugins/github
```
