# A岛匿名版插件
# 
# 模块化架构:
# - adapi.py    API 封装（A岛 API 异步客户端）
# - user.py     用户系统（保留但禁用）
# - main.py     主入口和命令处理

# 注意：__init__.py 不需要导入 main，因为 plugin_manager 会直接导入 adnmb.main

__all__ = []
