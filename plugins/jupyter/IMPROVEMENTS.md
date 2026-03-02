# Jupyter 插件改进说明

## 改进日期
2026-02-04

## 改进目标
参考 `astro_tools` 插件的编写规范，统一 `jupyter` 插件的代码风格和结构，并实现交互式 REPL 多轮对话模式。

---

## 主要改进

### 1. ✅ 添加插件初始化函数
- **改进前**: 缺少 `init()` 函数
- **改进后**: 添加标准 `init()` 函数，检查依赖并记录初始化日志
- **参考**: `astro_tools/main.py` 的 `init()` 函数

```python
def init(context=None) -> None:
    """插件初始化"""
    lazy_import_jupyter()
    if JUPYTER_AVAILABLE:
        logger.info("Jupyter plugin initialized (jupyter_client available)")
    else:
        logger.warning("Jupyter plugin initialized (jupyter_client NOT available: %s)", IMPORT_ERROR)
```

### 2. ✅ 统一日志系统
- **改进前**: 使用 `context.logger`
- **改进后**: 
  - 统一使用 `logging.getLogger(__name__)`
  - 在关键位置添加日志记录
- **参考**: 其他改进插件的日志实践

**新增日志点:**
```python
logger.info("Jupyter plugin initialized (jupyter_client available)")
logger.info("Executing Jupyter code: %s (timeout=%s)", code[:50], timeout)
logger.info("Started Jupyter REPL session: user=%s", context.current_user_id)
logger.info("Executing REPL code: %d lines, user=%s", len(code_buffer), context.current_user_id)
logger.exception("Jupyter execution failed")
```

### 3. ✅ 重构 handle() 函数
- **改进前**: 直接判断命令名称，无子命令系统
- **改进后**: 
  - 使用子命令路由
  - 支持 `/py help` 和 `/py repl`
  - 添加异常处理和日志记录
- **参考**: `astro_tools/main.py` 的 `handle()` 函数结构

**新增子命令支持:**
```python
/py help           # 显示帮助
/py repl           # 启动交互式 REPL
/py interactive    # 同上
/py 交互           # 同上
```

### 4. ✅ 添加 _show_help() 函数
- **改进前**: 使用 `_get_help()` 和 `_get_kernel_help()`，格式不统一
- **改进后**: 
  - 重命名为 `_show_help()` 和 `_show_kernel_help()`
  - 统一使用 Markdown 格式的帮助信息
  - 与其他插件保持一致的风格
- **参考**: `astro_tools/main.py` 的 `_show_help()` 函数

```python
def _show_help() -> str:
    """显示帮助信息"""
    return """
📓 **Jupyter 代码执行器**

**基本命令:**
• /py <代码> - 执行单行Python代码
• /py help - 显示此帮助
• /py repl - 启动交互式REPL模式
...
""".strip()
```

### 5. ✅ 实现交互式 REPL 模式 ⭐ 新功能

这是本次改进的核心新功能！

#### 为什么需要 REPL 模式？

**Jupyter 插件的特殊性：**
- Jupyter 内核本身是持久化的，变量会保持
- 但每次执行都需要输入完整代码
- 不适合编写多行代码或复杂逻辑

**REPL 模式的优势：**
1. **多行代码输入** - 可以逐行输入代码，构建完整程序
2. **更好的交互体验** - 类似真实的 Python REPL
3. **代码缓冲区** - 可以修改、查看、清空代码
4. **批量执行** - 输入完成后一次性执行

#### REPL 模式实现

**启动 REPL：**
```python
/py repl
```

**REPL 会话数据结构：**
```python
{
    "code_buffer": [],        # 代码缓冲区（行列表）
    "execution_count": 0,     # 执行次数
}
```

**REPL 会话命令：**
- **直接输入代码** - 添加到缓冲区
- **run / 执行 / 运行** - 执行缓冲区中的代码
- **show / 显示 / buffer** - 查看缓冲区内容
- **clear / 清空 / reset** - 清空缓冲区
- **help / 帮助 / ?** - 查看帮助
- **退出 / 取消 / exit / quit / q** - 结束会话（框架处理）

**使用示例：**
```
用户: /py repl
机器人: 📝 Jupyter 交互式 REPL 已启动
       现在可以开始输入代码...

用户: import numpy as np
机器人: ✓ 已添加 (共 1 行)

用户: import matplotlib.pyplot as plt
机器人: ✓ 已添加 (共 2 行)

用户: x = np.linspace(0, 10, 100)
机器人: ✓ 已添加 (共 3 行)

用户: plt.plot(x, np.sin(x))
机器人: ✓ 已添加 (共 4 行)

用户: show
机器人: 📄 当前缓冲区 (4 行):
       ```python
       import numpy as np
       import matplotlib.pyplot as plt
       x = np.linspace(0, 10, 100)
       plt.plot(x, np.sin(x))
       ```

用户: run
机器人: ✅ 执行完成 (#1)
       ```
       [输出结果]
       ```
       [图片]
       
用户: 退出
机器人: 已退出当前对话
```

#### handle_session() 实现

```python
async def handle_session(
    text: str,
    event: dict[str, Any],
    context: PluginContext,
    session,
) -> list[dict[str, Any]]:
    """
    处理会话消息
    
    框架已自动处理退出命令（退出/取消/exit/quit/q），插件无需再处理。
    """
    user_input = text.strip()
    code_buffer = session.get("code_buffer", [])
    
    # 特殊命令
    if user_input.lower() in {"run", "执行", "运行"}:
        # 执行缓冲区中的代码
        ...
    elif user_input.lower() in {"clear", "清空", "reset"}:
        # 清空缓冲区
        ...
    elif user_input.lower() in {"show", "显示", "buffer"}:
        # 显示缓冲区
        ...
    else:
        # 默认：添加到代码缓冲区
        code_buffer.append(user_input)
        session.set("code_buffer", code_buffer)
```

**特点：**
1. ✅ 使用框架的 Session API
2. ✅ 退出由框架统一处理
3. ✅ 10分钟超时自动结束
4. ✅ 支持查看、清空、执行缓冲区
5. ✅ 执行完成后缓冲区自动清空

### 6. ✅ 优化代码结构

#### 模块职责更清晰：
```
jupyter/
├── main.py                 # 入口、路由、帮助、REPL（优化后）
│   ├── init()             # 新增
│   ├── handle()           # 重构：使用子命令路由
│   ├── _handle_execute()  # 优化日志
│   ├── _handle_kernel()   # 优化日志
│   ├── _start_repl_session()  # 新增：启动 REPL
│   ├── handle_session()   # 新增：处理 REPL 会话
│   ├── _show_help()       # 重命名 + 优化格式
│   ├── _show_kernel_help() # 重命名 + 优化格式
│   └── shutdown()         # 优化日志
│
├── jupyter_manager.py      # 内核管理器（未修改）
├── jupyter_models.py       # 数据模型（未修改）
└── jupyter_config.py       # 配置（未修改）
```

---

## 与其他插件的统一性

| 特性 | astro_tools | guess_number | qingssh | jupyter | 状态 |
|------|-------------|--------------|---------|---------|------|
| init() | ✅ | ✅ | ✅ | ✅ | ✅ |
| _show_help() | ✅ | ✅ | ✅ | ✅ | ✅ |
| 使用 parse() | ✅ | ✅ | ✅ | ✅ | ✅ |
| 子命令路由 | ✅ | ✅ | ✅ | ✅ | ✅ |
| logger | ✅ | ✅ | ✅ | ✅ | ✅ |
| 异常处理 | ✅ | ✅ | ✅ | ✅ | ✅ |
| 多轮对话 | ❌ | ✅ | ✅ | ✅ | - |
| 框架退出处理 | - | ✅ | ✅ | ✅ | ✅ |

---

## Jupyter 插件的多轮对话特殊性

### 与其他插件的对比

#### guess_number（简单状态机）
- **用途**: 游戏状态管理
- **数据**: target, attempts, history
- **交互**: 输入数字进行猜测

#### qingssh（复杂资源管理）
- **用途**: SSH 连接管理
- **数据**: server_name, cwd, history
- **交互**: 执行 Shell 命令
- **资源**: 需要清理孤儿连接

#### jupyter（代码缓冲管理）
- **用途**: 多行代码编辑
- **数据**: code_buffer, execution_count
- **交互**: 构建和执行代码
- **资源**: Jupyter 内核本身是持久化的

### Jupyter 的双重持久化

**内核级持久化（全局）：**
- Jupyter 内核本身的变量是持久化的
- 跨越多次命令执行
- 通过 `/kernel restart` 清除

**REPL 级持久化（会话）：**
- REPL 会话的代码缓冲区
- 只在当前会话中有效
- 会话结束后清除

**示例：**
```python
# 第一次执行（无 REPL）
/py x = 10

# 第二次执行（无 REPL）
/py print(x)  # 输出: 10（变量保留在内核中）

# 启动 REPL
/py repl

# REPL 中
用户: y = 20
机器人: ✓ 已添加

用户: print(x, y)  # x 来自内核，y 来自缓冲区
机器人: ✓ 已添加

用户: run
机器人: 10 20（执行成功）

用户: 退出

# REPL 结束后
/py print(y)  # 输出: 20（y 已保存到内核）
```

---

## 改进总结

### 版本更新
- **版本号**: 2.0.0 → 3.0.0
- **重大改进**: 添加交互式 REPL 模式

### 主要成果

1. **✅ 标准化改进**
   - 添加 init() 函数
   - 统一 logger 使用
   - 重构 handle() 使用子命令路由
   - 统一 _show_help() 格式

2. **✅ 新增 REPL 模式**
   - 支持多行代码输入
   - 代码缓冲区管理
   - 交互式执行体验
   - 符合框架多轮对话规范

3. **✅ 用户体验提升**
   - 更友好的帮助信息
   - 清晰的子命令系统
   - 灵活的执行方式
   - 统一的退出机制

---

## 使用场景

### 场景 1: 快速执行单行代码
```
/py print("Hello, World!")
/py 1 + 1
/py import math; math.pi
```

### 场景 2: 执行多行代码（传统方式）
```
/py import numpy as np
x = np.linspace(0, 10, 100)
y = np.sin(x)
print(f"Min: {y.min()}, Max: {y.max()}")
```
注意：这种方式需要在一条消息中输入所有代码

### 场景 3: 交互式 REPL（新功能）⭐
```
/py repl

> import numpy as np
> x = np.linspace(0, 10, 100)
> y = np.sin(x)
> print(f"Min: {y.min()}, Max: {y.max()}")
> run
```
优势：可以逐行输入，修改更方便

### 场景 4: 数据分析工作流
```
/py repl

> import pandas as pd
> import matplotlib.pyplot as plt
> 
> df = pd.read_csv('data.csv')
> show  # 查看当前代码
> 
> df.describe()
> run  # 执行查看数据
> 
> plt.figure(figsize=(10, 6))
> df['column'].plot()
> plt.title('Analysis')
> run  # 执行绘图
> 
> 退出
```

---

## 测试建议

### 基本功能测试
1. `/py print("test")` - 执行单行代码
2. `/py help` - 查看帮助
3. `/kernel status` - 查看内核状态
4. `/kernel restart` - 重启内核

### REPL 模式测试 ⭐
1. `/py repl` - 启动 REPL
2. 输入多行代码
3. `show` - 查看缓冲区
4. `run` - 执行代码
5. `clear` - 清空缓冲区
6. `退出` - 结束会话

### 变量持久化测试
1. `/py x = 10` - 定义变量
2. `/py print(x)` - 验证变量保留
3. `/py repl` - 启动 REPL
4. `y = 20` - 定义新变量
5. `print(x, y)` - 验证两个变量都可用
6. `run` - 执行
7. `退出` - 结束 REPL
8. `/py print(y)` - 验证 REPL 中的变量也保留

### 边界情况测试
1. REPL 中输入空代码并执行
2. REPL 超时（等待10分钟）
3. REPL 中执行出错后继续
4. 在 REPL 中重复输入 `show`
5. 内核未启动时执行代码

---

## 后续优化建议

1. **代码编辑功能**
   - 支持删除缓冲区中的某一行
   - 支持修改特定行
   - 支持插入行

2. **代码历史**
   - 保存执行过的代码
   - 支持查看历史
   - 支持重新执行历史代码

3. **变量查看**
   - `/py vars` 查看当前内核中的变量
   - `/py locals` 查看局部变量
   - `/py globals` 查看全局变量

4. **魔法命令**
   - 支持 Jupyter 魔法命令（如 `%timeit`）
   - 支持 Shell 命令（如 `!ls`）

5. **代码格式化**
   - 自动格式化代码
   - 语法高亮提示
   - 代码补全建议

6. **并发执行**
   - 支持多个用户独立的内核
   - 每个用户有自己的变量空间

---

## 技术要点

### REPL 实现最佳实践

1. **代码缓冲区管理**
   ```python
   # 使用列表存储，便于添加、删除、合并
   code_buffer = []  # 每行一个元素
   ```

2. **执行后清空**
   ```python
   # 执行成功后清空缓冲区，避免重复执行
   if success:
       session.set("code_buffer", [])
   ```

3. **错误处理**
   ```python
   # 执行失败时保留缓冲区，允许修改后重试
   except Exception as e:
       return segments(f"执行失败: {e}\n缓冲区已保留")
   ```

4. **退出处理**
   ```python
   # 依赖框架统一处理，无需插件处理
   # 框架会自动结束会话并清理数据
   ```

---

## 总结

通过本次改进，`jupyter` 插件已经与其他插件保持一致的代码风格和结构规范。最重要的新增功能是**交互式 REPL 模式**，极大地提升了编写多行代码的用户体验。

### 主要成果：

- ✅ 标准的初始化和入口函数
- ✅ 完整的帮助系统
- ✅ 清晰的子命令路由
- ✅ 完善的日志记录
- ✅ **全新的 REPL 交互模式** ⭐
- ✅ 符合框架的多轮对话规范
- ✅ 统一的代码风格

### 插件对比总结

| 插件 | 主要功能 | 多轮对话用途 | 特殊性 |
|------|---------|------------|--------|
| astro_tools | 天文计算 | 无 | 纯函数式命令 |
| guess_number | 猜数字游戏 | 游戏状态 | 简单状态机 |
| qingssh | SSH管理 | SSH会话 | 外部资源管理 |
| jupyter | 代码执行 | REPL缓冲区 | 双重持久化 |

所有插件现在都遵循统一的编码规范！🎉
