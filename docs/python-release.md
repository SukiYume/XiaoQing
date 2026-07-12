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

1. 从 Git 跟踪文件与 `pyproject.toml` 的 package-data 规则生成精确运行时清单；
2. 在仓库外的临时工作目录中构建 wheel 和 sdist，避免本地 `build/` 目录遮蔽
   PyPA `build` 模块，也避免旧构建目录污染新产物；
3. 安全检查两个归档的路径、成员类型、重复成员、大小预算和项目身份；
4. 要求 wheel 的应用文件与清单完全一致，并要求 sdist 至少包含完整运行时清单；
5. 将 wheel 和 sdist 分别安装进独立临时虚拟环境；
6. 清除 `PYTHONPATH`、`PYTHONHOME` 等来源污染，并使用各虚拟环境自己的
   Python 以 `-I` 运行探针；
7. 从 `site-packages` 导入 `main`、`core`、`plugins` 和全部 29 个 manifest
   声明的插件入口，确认加载来源没有回退到源码仓库；
8. 读取全部打包资源，并额外校验 JSON、词典 SHA-256、Pendo HTML/SVG 和
   demo ZIP 的语义与完整性；
9. 使用安装后生成的 `xiaoqing` 控制台入口执行 `--help`。

当前基线是 29 个插件、393 个运行时文件和 39 个运行时资源。修改插件、入口或
package-data 时，应同步更新打包规则；测试中的精确数量变化必须来自经过审查的
真实清单变化，不能通过放宽断言绕过。

## 构建工具与依赖锁

验证器使用开发依赖中精确固定的 `build`、`setuptools` 和 `wheel`，CI 从对应
Python 版本的 `requirements/python-X.Y-ci.lock` 安装这些工具。更新任一构建工具
后，应重新生成 CI 锁并检查锁文件元数据与输入摘要：

```bash
python scripts/compile_locks.py --check
```

运行时锁不包含 `build` 和 `wheel`；它们是发布/CI 工具，不应进入生产依赖面。

## 验证边界

该门禁验证发布归档、安装来源、29 个插件入口的可导入性以及资源语义，不启动
真实 QQ、网络服务、调度器或外部模型，也不宣称执行了完整 `PluginManager`
初始化。完整容器启动与插件加载由 Docker 发布 smoke 负责；Python 发布门禁刻意
避免插件初始化的网络、凭据和持久化副作用。

CI 在 Python 3.13 作业中直接执行验证器。该步骤不能用只在 pytest 中可跳过的
测试替代，因为缺少构建前端或产物校验失败都必须让发布作业硬失败。
