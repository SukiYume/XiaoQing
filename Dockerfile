FROM python:3.13-slim AS builder

WORKDIR /build

# 编译工具只存在于 builder，不进入最终运行镜像。
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements/python-3.13.lock ./requirements/python-3.13.lock
RUN python -m venv /opt/xiaoqing-venv \
    && /opt/xiaoqing-venv/bin/pip install --no-cache-dir --require-hashes \
        -r requirements/python-3.13.lock


FROM python:3.13-slim AS runtime

WORKDIR /app

# 确保 Python 输出实时显示（不缓冲），并设置模块搜索路径
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app
ENV PATH=/opt/xiaoqing-venv/bin:$PATH

# 只复制 builder 中已解析、已哈希校验的 Python 环境。
COPY --from=builder /opt/xiaoqing-venv /opt/xiaoqing-venv

# 只复制运行源码和公开示例配置。.dockerignore 进一步拒绝本地密钥、
# 数据库、日志、缓存、模型产物、废弃插件与 Git 历史。
COPY main.py pyproject.toml ./
COPY core/ ./core/
COPY plugins/ ./plugins/
COPY config/*.example ./config/

# 真实 config.json / secrets.json 必须在运行时通过 volume/secret mount 注入。
RUN mkdir -p logs config

# 按项目的可信 admin 模型保留容器内 root；不要挂载 Docker socket，且仅挂载
# admin 明确允许 Bot 修改的宿主目录。完整边界见 docs/container-security.md。

# XiaoQing 默认监听端口
EXPOSE 12000

CMD ["python", "main.py"]
