# APOD 插件

每日一天文图插件，提供每日天文图片的获取和展示功能。

## 功能介绍

APOD (Astronomy Picture of the Day) 插件可以从 UCL 的 APOD 镜像站获取每日天文图片，包括图片、标题、说明和版权信息。

## 使用方法

### 基本命令

```
/apod [日期]
```

- 不带参数：获取今天的天文图
- 带日期参数：获取指定日期的天文图（格式：YYMMDD）

### 示例

```
/apod                # 获取今天的天文图
/apod 231225         # 获取 2023年12月25日的天文图
/apod help           # 显示帮助信息
```

## 配置说明

在 `config/secrets.json` 中配置：

```json
{
  "plugins": {
    "apod": {
      "proxy": "http://proxy.example.com:8080"  // 可选，如需代理
    }
  }
}
```

## 功能特性

- 自动获取图片并缓存
- 支持指定日期查询历史图片
- 提供中英文混合的说明文本
- 代理支持
- 错误处理和日志记录

## 依赖

- aiohttp
- beautifulsoup4

## 数据来源

- UCL APOD Mirror: http://www.star.ucl.ac.uk/~apod/apod
