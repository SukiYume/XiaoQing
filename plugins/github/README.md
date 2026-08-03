# GitHub Trending 插件

抓取 [GitHub 官方 Trending 页面](https://github.com/trending)，展示每日、每周或每月热门仓库。实现读取官方 HTML，而不是调用承诺稳定结构的公开 API；GitHub 改版时可能需要同步更新解析规则。

## 命令

<!-- manifest-command-aliases:start -->
| 命令别名 | 功能 |
| --- | --- |
| `/github`、`/gh`、`/trending` | 查询 GitHub Trending |
<!-- manifest-command-aliases:end -->

```text
/github
/github daily
/github weekly
/github monthly
/github help
```

空参数等同 `daily`。命令只接受一个完整参数；未知范围、多余参数、控制字符、超长输入和未闭合引号会被拒绝，不会静默回退为每日趋势。
当前不提供语言过滤参数，查询范围只由 `daily`、`weekly` 或 `monthly` 决定。

## 抓取与输出边界

- 无代理路径使用 XiaoQing 的公网 DNS 固定抓取器，只允许 `https://github.com`。
- HTML 响应必须通过 MIME、重定向、压缩比例及 2 MiB 解码预算；只接受 UTF-8/ASCII。
- 代理路径同样限制为 GitHub HTTPS 同源重定向和 2 MiB，代理 secret 只接受结构正确的 HTTP(S) URL，且不会写入日志。
- 解析最多 50 个文章节点，仓库链接必须是 GitHub 的两段仓库路径；脚本、样式、控制字符、重复仓库和畸形文章会被清理或跳过。
- 描述和语言有独立长度上限；star/fork 使用规范化计数，并识别 `today`、`this week`、`this month` 三种周期新增 star。
- 最多展示前 10 个仓库；接近 3000 字 QQ 单条文本上限时停止追加完整仓库块，不截断到半条。

## 定时任务与历史

清单在每天 08:30（调度器时区）运行 `daily`。框架会把返回消息投递到部署配置的默认群组。

成功抓取后原子写入：

- `data/trending_<range>_latest.json`：该范围最近一次成功结果；
- `data/history/trending_<range>_<YYYY-MM-DD>.json`：按日期保存的快照。

`latest` 与同一天历史文件内容相同是有意的“当前指针 + 历史快照”设计；这两类文件是历史快照，不是跳过网络请求的响应缓存。每个时间范围最多保留 90 份常规历史文件；链接和无关文件不会被跟随或删除。

## 配置

插件不需要 GitHub token。旧部署可在 secret 中设置：

```yaml
plugins:
  github:
    proxy: "http://proxy.example.com:8080"
```

代理负责目标 DNS 连接，因此属于管理员信任边界；未配置代理时使用安全性更强的本地 DNS 固定路径。
