# GitHub Trending

当前实现抓取 GitHub 官方 Trending HTML，并支持时间范围，不提供语言过滤参数。

## 命令

- `/github` 或 `/github daily`
- `/github weekly`
- `/github monthly`
- `/github help`

每天 08:30 的 schedule 使用部署侧 `default_group_ids`。HTML 下载限制为 2 MiB/15 秒并验证公网 DNS、重定向和 `github.com` 域名；解析在线程执行。每次成功抓取会原子写入 `data/trending_<range>_latest.json` 和按日期历史文件，这些文件是历史快照，不是跳过网络请求的响应缓存。

可选的 `plugins.github.proxy` secret 仅为兼容旧部署保留；生产安全抓取路径不会把代理地址写入日志。
