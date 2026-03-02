# GitHub Trending 插件

获取 GitHub 每日/每周/每月趋势项目。

## 功能介绍

GitHub Trending 插件可以获取 GitHub 上的热门趋势项目，支持每日、每周、每月三种时间范围。

## 使用方法

### 基本命令

```
/github [时间范围] [语言]
/github help
```

### 时间范围

- `daily` - 每日趋势（默认）
- `weekly` - 每周趋势
- `monthly` - 每月趋势

### 示例

```
/github                    # 获取每日趋势（所有语言）
/github daily              # 获取每日趋势
/github weekly python      # 获取每周 Python 趋势
/github monthly javascript # 获取每月 JavaScript 趋势
/github help               # 显示帮助信息
```

## 配置说明

在 `config/secrets.json` 中配置（可选）：

```json
{
  "plugins": {
    "github": {
      "proxy": "http://proxy.example.com:8080"  // 如需代理
    }
  }
}
```

## 功能特性

- 支持三种时间范围（每日/每周/每月）
- 可指定编程语言筛选
- 展示项目名称、描述、星标数等信息
- 自动缓存减少请求
- 代理支持

## 数据来源

- GitHub Trending: https://github.com/trending

## 依赖

- beautifulsoup4
- aiohttp

## 注意事项

- 数据直接从 GitHub Trending 页面抓取
- 网络不稳定时可能需要使用代理
- 结果会在一定时间内缓存
