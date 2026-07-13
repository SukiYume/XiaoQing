# Python 发布产物验证

XiaoQing 的 Python 发布门禁必须同时验证 wheel 和 sdist。仅在源码目录中运行
`import` 或 `xiaoqing --help` 不能证明发布产物完整：当前工作目录可能优先提供
`main.py`、`core/` 和 `plugins/`，从而掩盖 wheel 漏文件、资源未打包或入口点错误。

## 唯一发布门禁

在仓库根目录执行：

```bash
python scripts/verify_python_release.py
```

验证器会完成以下工作：

1. 从 `pyproject.toml` 的 53 个显式 runtime package、Git 跟踪文件和
   package-data 规则推导运行时清单，并与版本控制的
   `release/python-runtime-files.txt` 做精确比对；
2. 在仓库外的临时工作目录中构建 wheel 和 sdist，避免本地 `build/` 目录遮蔽
   PyPA `build` 模块，也避免旧构建目录污染新产物；
3. 安全检查两个归档的路径、成员类型、重复成员、大小预算和项目身份；
4. 要求 wheel 与 sdist 中的 `main.py`、`core/`、`plugins/` 应用文件都与清单
   完全一致；任何测试、训练、迁移、实验或仓库脚本进入产物都会失败；
5. 将 wheel 和 sdist 分别安装进独立临时虚拟环境；
6. 清除 `PYTHONPATH`、`PYTHONHOME` 等来源污染，并使用各虚拟环境自己的
   Python 以 `-I` 运行探针；
7. 从 `site-packages` 导入 `main`、`core`、`plugins` 和全部 29 个 manifest
   声明的插件入口，确认加载来源没有回退到源码仓库；同时为 snapshot 中全部
   Python 源文件建立安装路径 module spec 并逐文件 `py_compile`；
8. 读取全部打包资源，并额外校验 JSON、词典 SHA-256、Pendo HTML/SVG 和
   demo ZIP 的语义与完整性；
9. 使用安装后生成的 `xiaoqing` 控制台入口执行 `--help`。

当前基线是 29 个插件、53 个 Python package 和 39 个运行时资源。准确文件数以
`release/python-runtime-files.txt` 为准。修改插件、入口或 package-data 时，应
同步更新显式 package 列表和 snapshot；数量变化必须来自经过审查的真实清单变化，
不能通过放宽断言绕过。

PyPI sdist 是可构建的源码发布包，不是仓库快照：它明确不包含 `tests/`、`scripts/`、
arXiv 训练代码和数据、Pendo 迁移脚本、Xiaoqing Chat 实验以及其他仓库工具。
arXiv 模型权重同样是外部运行资产；安装 `xiaoqing[arxiv-ml]` 后须通过
`ARXIV_MODEL_PATH` 或插件配置提供模型目录。

## 构建工具与依赖锁

验证器使用开发依赖中精确固定的 `build`、`setuptools` 和 `wheel`，CI 从对应
Python 版本的 `requirements/python-X.Y-ci.lock` 安装这些工具。更新任一构建工具
后，应重新生成 CI 锁并检查锁文件元数据与输入摘要：

```bash
python scripts/compile_locks.py --check
```

运行时锁不包含 `build` 和 `wheel`；它们是发布/CI 工具，不应进入生产依赖面。

## 验证边界

该门禁验证发布归档、安装来源、29 个插件入口的可导入性、全部 Python 文件的
安装后编译以及资源语义，不启动真实 QQ、网络服务、调度器或外部模型，也不宣称
执行了完整 `PluginManager` 初始化或导入每个可选依赖后端。逐文件编译刻意不执行
模块顶层代码，既覆盖 wheel/sdist 的全部 Python 源，又避免网络、凭据、持久化
副作用和未安装可选依赖造成的假失败。完整容器启动与插件加载由 Docker 发布
smoke 负责。

CI 在 Python 3.13 作业中直接执行验证器。该步骤不能用只在 pytest 中可跳过的
测试替代，因为缺少构建前端或产物校验失败都必须让发布作业硬失败。
