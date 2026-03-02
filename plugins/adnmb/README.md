# ADnmb 插件

A岛匿名版（ADnmb）浏览插件，提供 A岛论坛的浏览和互动功能。

## 功能介绍

ADnmb 插件允许用户通过 XiaoQing 浏览 A岛匿名版的内容，包括查看时间线、串内容、发布新串、回复等功能。

## 使用方法

### 基本命令

```
/adnmb timeline [版块]    # 查看时间线
/adnmb thread <串号>      # 查看串内容
/adnmb post <内容>        # 发布新串
/adnmb reply <串号> <内容> # 回复串
/adnmb feed               # 查看订阅源
/adnmb help               # 显示帮助信息
```

### 示例

```
/adnmb timeline           # 查看综合版时间线
/adnmb timeline tech      # 查看技术版时间线
/adnmb thread 12345678    # 查看串号 12345678
/adnmb post 测试发串
/adnmb reply 12345678 回复内容
/adnmb feed
/adnmb help
```

## 配置说明

在 `config/secrets.json` 中配置：

```json
{
  "plugins": {
    "adnmb": {
      "cookie": "your_cookie_here",
      "user_hash": "your_user_hash",
      "default_board": "4",
      "image_proxy": "http://proxy.example.com",
      "allowed_users": ["user_id_1"]
    }
  }
}
```

### 配置项说明

- `cookie` - A岛 Cookie（用于认证）
- `user_hash` - 用户 Hash（用于发串）
- `default_board` - 默认版块（默认: 4 综合版）
- `image_proxy` - 图片代理（可选）
- `allowed_users` - 允许使用此插件的用户 ID

### 获取 Cookie

1. 访问 A岛网站并登录
2. 打开浏览器开发者工具
3. 在网络标签页找到请求
4. 复制 Cookie 值

## 功能特性

- 查看版块时间线
- 查看串详情和回复
- 发布新串
- 回复已有串
- 图片展示（通过代理）
- 订阅源管理
- 多版块支持

## 版块列表

常用版块：
- `4` - 综合版1
- `5` - 综合版2
- `20` - 欢乐恶搞
- `11` - 技术宅
- `17` - 文学
- 更多版块请在 A岛网站查看

## 数据缓存

- 串缓存: `cache/threads/`
- 用户数据: `data/user_data.json`
- 订阅源: `data/feeds.json`

## 注意事项

⚠️ **使用须知**：
- 需要有效的 A岛账号
- 遵守 A岛社区规则
- 不要滥发内容
- 注意隐私保护
- Cookie 会过期，需定期更新
- 建议配置 `allowed_users` 限制使用

## 隐私说明

- Cookie 包含账号信息，请妥善保管
- 不要将配置文件分享给他人
- 发布的内容会与账号关联
- 使用代理注意数据安全

## API 信息

- A岛 API 端点（非官方）
- 可能随时失效
- 请遵守 API 使用限制

## 依赖

- requests
- beautifulsoup4

安装依赖：
```bash
pip install requests beautifulsoup4
```

## 适用场景

- 浏览 A岛内容
- 快速发串回复
- 订阅感兴趣的串
- 移动端浏览替代

## 法律声明

此插件仅供学习交流使用，使用者需遵守：
- A岛社区规则
- 相关法律法规
- 尊重他人隐私
- 不传播违法内容

使用此插件产生的任何后果由使用者自行承担。
