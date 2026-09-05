# 🏝️ ADNMB A 岛浏览

ADNMB 插件浏览 A 岛公开时间线、板块、串、回复和匿名订阅列表。订阅由插件为每位 QQ 用户派生的匿名 UUID 标识。

---

## 🔐 使用条件

- 命令支持群聊与私聊。
- 运行依赖为 `aiohttp` 和 Pillow。
- 网络需要访问 A 岛公开 API 与图片地址。

---

## ⌨️ 命令

| 命令 | 功能 |
|---|---|
| `/adnmb -t [-p 页码]` | 时间线 |
| `/adnmb -f` | 板块列表 |
| `/adnmb -m <板块名> [-p 页码]` | 板块内容 |
| `/adnmb -c <串号> [-p 页码]` | 串与回复 |
| `/adnmb -r <回复号>` | 单条回复 |
| `/adnmb -d [-p 页码]` | 匿名订阅列表 |
| `/adnmb -a <串号>` | 订阅串 |
| `/adnmb -e <串号>` | 取消订阅 |
| `/adnmb -h` | 插件帮助 |

完整别名、参数边界和错误样例可通过 `/help adnmb` 查看。

---

## ⚙️ 配置与数据

可在 `config.plugins.adnmb.uuid` 设置部署级 UUID。插件会结合 QQ 用户派生各自订阅身份。默认身份根据插件数据目录与用户生成稳定 UUID。

图片缓存位于：

```text
data/adnmb/images/
```

缓存采用 TTL、条目数和总字节限制。远程图片经过超时、响应大小、MIME、尺寸和像素校验。

每条外部正文最多进入解析器 65,536 个字符，HTML 清理采用线性扫描。畸形标签和连续尖括号保持有界处理，展示文本继续受消息长度预算限制。

---

## 🔐 权限与边界

插件命令目录只包含公开内容浏览与匿名 Feed 管理。订阅操作写入 A 岛 API 中对应匿名 UUID 的 Feed。

---

## 🩺 排障

1. 使用 `/adnmb -f` 验证 API 连接。
2. 使用 `/adnmb -t` 验证时间线解析。
3. 检查日志中的 HTTP 状态、内容类型和图片校验结果。
4. 检查 `data/adnmb/images/` 的写入权限。

---

## ✅ 开发验证

```bash
python -m pytest tests/plugins/adnmb/test_adnmb.py -q
python -m ruff check plugins/adnmb tests/plugins/adnmb/test_adnmb.py
```
