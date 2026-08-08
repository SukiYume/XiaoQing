# 🧮 Wolfram|Alpha

`wolframalpha` 通过固定 Wolfram|Alpha HTTPS API 执行数学、物理、化学、单位换算和公开数据查询。Manifest 将命令限定为 Bot 管理员入口。

---

## ⌨️ 命令

```text
/alpha 1+1
/wa sin(pi/4)
/alpha --mode=step integrate x^2
/alpha --mode=complete population of China
/alpha help
```

触发词为 `/alpha`、`/wolfram`、`/wa` 和 `/计算`。

| 模式 | 接口与结果 |
| --- | --- |
| `simple` | 快速文本结果端点；默认模式 |
| `step` | XML 步骤解答 |
| `complete` | JSON Result pod |
| `cp` | `complete` 的短别名 |

模式通过开头的 `--mode=<值>` 显式指定。问题正文末尾的 `step` 或 `cp` 作为普通查询文本。查询长度上限为 500 个字符。

---

## ⚙️ App ID

在 `config/secrets.json` 中配置：

```json
{
  "plugins": {
    "wolframalpha": {
      "appid": "YOUR-WOLFRAM-APPID"
    }
  }
}
```

App ID 接受 1～128 个字母、数字、连字符或下划线。该值保存在公开配置、聊天回复和普通日志边界之外。

---

## 🌐 请求流程

- `simple` 请求 `https://api.wolframalpha.com/v1/result`；
- `step` 与 `complete` 请求 `https://api.wolframalpha.com/v2/query`；
- 三种模式均使用 GET、固定域名、30 秒总超时和有界客户端；
- App ID 作为请求参数传给客户端；
- 全插件最多同时执行 2 个 Wolfram 请求。

请求 URL 与参数保留在普通日志边界之外，日志记录模式、状态、响应长度和错误类别。

---

## 🔐 响应边界

| 项目 | 当前预算 |
| --- | ---: |
| 线上与解码响应 | 各 1 MiB |
| XML/JSON | 深度、节点、属性和字符串限制 |
| 结果项 | 最多 20 个 |
| API 文本 | 最多 2400 个字符 |
| 查询 | 最多 500 个字符 |

最终文本会保留完整结果项边界，并适配 QQ 单条消息预算。HTTP、网络、超时、格式与内部异常分别映射为稳定公开提示。

---

## 🩺 排障

| 现象 | 检查项 |
| --- | --- |
| 提示 App ID 配置异常 | 核对 secret 层级和允许字符 |
| 返回查询结果为空 | 尝试 `simple`、`step` 或 `complete` 的适用模式 |
| 请求超时 | 检查 Wolfram|Alpha 服务和网络连接 |
| XML 或 JSON 校验失败 | 检查官方接口结构变化与响应预算 |
| 结果显示省略号 | 缩小查询范围，或拆分为多个问题 |

---

## ✅ 开发验证

在仓库根目录运行：

```bash
python -m pytest -q tests/plugins/wolframalpha/test_wolframalpha.py
python -m ruff check plugins/wolframalpha tests/plugins/wolframalpha/test_wolframalpha.py
python -m mypy plugins/wolframalpha
```
