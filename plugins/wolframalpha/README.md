# Wolfram|Alpha 插件

## 功能简介

Wolfram|Alpha 是一个强大的计算知识引擎插件，可以进行各种计算、查询和数据分析。

## 使用方法

### 基本查询
```
/alpha <问题>
```

### 特殊模式

1. **步骤解答模式** - 显示详细的求解步骤
   ```
   /alpha integrate x^2 step
   /alpha solve x^2+2x+1=0 step
   ```

2. **完整结果模式** - 返回更详细的结果
   ```
   /alpha 1+1 cp
   ```

## 使用示例

### 数学计算
```
/alpha 1+1
/alpha sin(pi/4)
/alpha sqrt(144)
/alpha integrate x^2
/alpha solve x^2+2x+1=0
/alpha derivative of sin(x)
/alpha limit of (sin(x)/x) as x->0
```

### 物理化学
```
/alpha speed of light
/alpha atomic mass of carbon
/alpha density of water
```

### 单位转换
```
/alpha convert 100 USD to CNY
/alpha convert 10 miles to km
/alpha convert 100 fahrenheit to celsius
```

### 数据查询
```
/alpha population of China
/alpha weather in Beijing
/alpha GDP of USA
/alpha distance from Earth to Moon
```

### 日期时间
```
/alpha days until Christmas
/alpha what day is 2026-12-25
/alpha age of someone born 1990-01-01
```

## 配置说明

需要在 `config/secrets.json` 中配置 Wolfram|Alpha App ID：

```json
{
  "plugins": {
    "wolframalpha": {
      "appid": "YOUR_WOLFRAM_ALPHA_APP_ID"
    }
  }
}
```

### 如何获取 App ID

1. 访问 [Wolfram|Alpha Developer Portal](https://developer.wolframalpha.com/)
2. 注册或登录账号
3. 创建一个新的应用
4. 获取 App ID 并配置到 secrets.json

## 技术细节

### API 端点

- **快速查询**: `/v1/result` - 返回简短答案
- **步骤解答**: `/v2/query` - 返回详细步骤
- **完整结果**: `/v2/query` - 返回结构化数据

### 错误处理

插件包含完善的错误处理机制：
- 超时处理（30秒）
- 网络错误捕获
- API 状态码检查
- 结果解析异常处理

### 日志记录

所有查询和错误都会记录到日志系统，便于调试和监控。

## 版本历史

### v0.1.0
- 基础查询功能
- 步骤解答支持
- 完整结果模式

### v0.2.0 (当前)
- 统一代码结构，遵循 astro_tools 模式
- 改进帮助信息展示
- 增强错误处理和日志记录
- 优化用户交互体验
- 添加更详细的示例说明
