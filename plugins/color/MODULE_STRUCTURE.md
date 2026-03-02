# Color 插件模块结构

## 概述
color 插件已重构为模块化架构，将原本 700+ 行的 main.py 拆分为多个功能模块，提高了代码的可维护性和可读性。

## 文件结构

```
color/
├── __init__.py              # 包初始化文件
├── main.py                  # 主入口和命令路由（305 行）
├── convert.py               # 颜色转换工具（121 行）
├── data_manager.py          # 数据管理模块（110 行）
├── query.py                 # 颜色查询模块（87 行）
├── image_gen.py             # 图片生成模块（70 行）
├── stellar.py               # 恒星光谱颜色处理（139 行）
├── plugin.json              # 插件配置
├── color.json               # 内置颜色库
├── stellar_colors.txt       # 恒星光谱数据
└── data/                    # 数据目录
    └── images/              # 生成的颜色图片
```

## 模块说明

### main.py - 主入口模块
- 插件初始化
- 命令解析和路由
- 协调各模块完成请求处理
- 辅助函数（帮助信息、添加/删除自定义颜色）

### convert.py - 颜色转换模块
提供颜色格式转换和验证功能：
- `validate_rgb()` - 验证 RGB 值
- `validate_cmyk()` - 验证 CMYK 值
- `rgb_to_cmyk()` - RGB 转 CMYK
- `hex_to_rgb()` - HEX 转 RGB
- `rgb_to_hex()` - RGB 转 HEX

### data_manager.py - 数据管理模块
负责颜色数据的加载、保存和管理：
- `load_colors()` - 加载所有颜色数据（内置+自定义）
- `load_custom_colors()` - 加载用户自定义颜色
- `save_custom_colors()` - 保存用户自定义颜色
- `get_color_systems()` - 获取所有色系
- `format_color_info()` - 格式化颜色信息输出

### query.py - 颜色查询模块
提供多种颜色查询方式：
- `find_by_name()` - 按名称查找
- `find_by_rgb()` - 按 RGB 查找
- `find_by_hex()` - 按 HEX 查找
- `find_by_cmyk()` - 按 CMYK 查找
- `find_by_keyword()` - 关键词搜索

### image_gen.py - 图片生成模块
使用 matplotlib 生成颜色示例图片：
- `generate_color_image()` - 生成颜色预览图
- 支持中文字体显示
- 异步执行，不阻塞主线程

### stellar.py - 恒星光谱颜色模块
处理恒星光谱相关功能：
- `load_stellar_colors()` - 加载恒星光谱数据
- `query_stellar_color()` - 查询指定光谱型的颜色
- `list_spectral_types()` - 列举光谱型列表

## 相对导入
所有模块使用相对导入引用，符合 Python 包的最佳实践：

```python
from . import convert
from . import data_manager
from . import query
from . import image_gen
from . import stellar
```

## 对比原版
- **原版**：单一 main.py 文件，723 行
- **重构后**：6 个模块文件，共 832 行（含注释和文档字符串）
- **main.py**：从 723 行减少到 305 行，减少 58%
- **优势**：
  - 职责清晰，每个模块专注单一功能
  - 易于维护和测试
  - 代码复用性更好
  - 符合单一职责原则

## 依赖
- **必需**：core.plugin_base, core.args
- **可选**：matplotlib, numpy（图片生成）, pandas（恒星颜色）

## 参考
本次重构参考了 `astro_tools` 插件的模块化架构。
