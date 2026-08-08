# 🐙 GitHub Trending

`github` 读取 [GitHub 官方 Trending 页面](https://github.com/trending)，展示每日、每周和每月热门仓库。

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

Trending 页面结构变化时，维护者需要同步更新解析规则和测试夹具。

---

## ⏰ 定时任务与历史

Manifest 每天 08:30 按调度器时区运行 `scheduled`，Core 负责把结果投递到默认群。成功抓取后会写入：

```text
data/github/trending_<range>_latest.json
data/github/history/trending_<range>_<YYYY-MM-DD>.json
```

`latest` 提供当前指针，日期文件提供历史快照。每个时间范围最多保留 90 份常规历史文件。写入和保留策略由进程内锁按顺序保护。

---

## 📌 可选代理

插件凭据字段只有可选代理。在 `config/secrets.json` 中配置：

```json
{
  "plugins": {
    "github": {
      "proxy": "http://proxy.example.com:8080"
    }
  }
}
```

代理地址接受结构完整的 HTTP 或 HTTPS URL。代理负责目标 DNS 连接，属于管理员信任边界。日志会保留代理凭据之外的请求状态信息。

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
