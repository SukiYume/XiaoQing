# 🏝️ ADNMB A 岛浏览

ADNMB 插件展示 A 岛公开时间线、板块、串、回复和匿名订阅列表。插件为每位 QQ 用户派生一个匿名 UUID。该 UUID 标识用户的订阅。

---

## 🔐 使用条件

- 命令支持群聊与私聊。
- 必需依赖为 `aiohttp`（核心依赖，随基础安装）和 Pillow（`python -m pip install -e ".[plugins]"`）。缺少必需依赖时插件不会加载，日志记录缺失的模块名。
- 网络需要访问 A 岛公开 API 与图片地址。

---

## ⌨️ 命令

触发词为 `/adnmb`、`/a岛` 和 `/岛`。

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

每个选项都有长选项（如 `--timeline`、`--feed`）。完整别名、参数边界和错误样例可通过 `/help adnmb` 查看。

---

## ⚙️ 配置与数据

每位 QQ 用户的订阅 UUID 由“种子”和该用户的 QQ 号派生，互不可见。种子可在 `config/secrets.json` 中设置，可省略：

```json
{
  "plugins": {
    "adnmb": {
      "uuid": "your-deployment-seed"
    }
  }
}
```

未设置时，种子取插件图片缓存目录（`data/adnmb/images/`）的绝对路径，因此移动数据目录会改变订阅身份；需要固定订阅身份时请设置 `uuid`。

图片缓存位于 `data/adnmb/images/`，最多 256 项、256 MiB，保留 7 天。远程图片经过超时、响应大小、MIME、尺寸和像素校验。展示文本受单条消息长度限制。

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
