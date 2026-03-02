# Jupyter 插件

Jupyter Notebook 管理插件，提供远程 Jupyter 服务器的管理和代码执行功能。

## 功能介绍

Jupyter 插件允许通过 XiaoQing 管理 Jupyter Notebook 服务器，包括启动、停止服务器，以及执行 Python 代码等功能。

## 使用方法

### 基本命令

```
/jupyter start           # 启动 Jupyter 服务器
/jupyter stop            # 停止 Jupyter 服务器
/jupyter status          # 查看服务器状态
/jupyter exec <代码>     # 执行 Python 代码
/jupyter notebooks       # 列出所有 notebooks
/jupyter help            # 显示帮助信息
```

### 示例

```
/jupyter start
/jupyter status
/jupyter exec print("Hello World")
/jupyter exec import numpy as np; print(np.pi)
/jupyter notebooks
/jupyter stop
/jupyter help
```

## 配置说明

在 `config/secrets.json` 中配置：

```json
{
  "plugins": {
    "jupyter": {
      "jupyter_dir": "/path/to/notebooks",
      "port": 8888,
      "password": "your_password",
      "allow_remote": false,
      "allowed_users": ["user_id_1", "user_id_2"],
      "max_output_length": 2000
    }
  }
}
```

### 配置项说明

- `jupyter_dir` - Notebook 工作目录（默认: ~/jupyter）
- `port` - Jupyter 服务器端口（默认: 8888）
- `password` - Jupyter 密码（可选）
- `allow_remote` - 是否允许远程访问（默认: false）
- `allowed_users` - 允许使用此插件的用户 ID
- `max_output_length` - 最大输出长度（字符）

## 功能特性

- 远程启动/停止 Jupyter 服务器
- 执行 Python 代码片段
- 查看服务器状态
- 列出所有 notebooks
- 输出截断保护
- 权限控制
- 多用户隔离

## 代码执行

### 支持的功能
- 执行任意 Python 代码
- 导入第三方库
- 数据计算和分析
- 结果输出

### 限制
- 输出长度限制
- 执行超时保护
- 不支持交互式输入
- 不支持图形显示（仅文本输出）

## 安全特性

- 用户权限控制
- 执行超时保护
- 输出长度限制
- 错误捕获和处理
- 本地访问模式（默认）

## 注意事项

⚠️ **安全警告**：
- 代码执行功能具有系统权限，极其危险
- 必须配置 `allowed_users` 严格限制使用
- 建议仅在受控环境使用
- 定期审查代码执行日志
- 不要在生产服务器上运行

⚠️ **使用建议**：
- 仅在开发/测试环境使用
- 不要执行不信任的代码
- 注意系统资源消耗
- 定期备份 notebooks

## 依赖

- jupyter (Jupyter Notebook)
- notebook (Jupyter Server)

安装依赖：
```bash
pip install jupyter notebook
```

## 适用场景

- 远程数据分析
- 代码快速测试
- 教学演示
- 科学计算
- 原型开发

## 不适用场景

- 生产环境
- 多用户共享环境
- 需要高安全性的场景
- 长时间运行的任务

## 改进计划

查看 [IMPROVEMENTS.md](IMPROVEMENTS.md) 了解计划中的功能和改进。

## 故障排查

### 服务器无法启动
- 检查端口是否被占用
- 确认 Jupyter 已正确安装
- 查看日志文件

### 代码执行失败
- 检查语法错误
- 确认所需库已安装
- 查看错误信息

### 无法连接
- 确认服务器已启动
- 检查防火墙设置
- 验证网络连接

## 参考

- Jupyter Documentation: https://jupyter.org/documentation
- Jupyter Notebook: https://jupyter-notebook.readthedocs.io/
