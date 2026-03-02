FROM python:3.13-slim

WORKDIR /app

# 确保 Python 输出实时显示（不缓冲），并设置模块搜索路径
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

# 安装编译依赖（部分 Python 包需要）
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# 先复制依赖文件，利用 Docker 层缓存
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制项目代码
COPY . .

# 创建日志目录
RUN mkdir -p logs

# XiaoQing 默认监听端口
EXPOSE 12000

CMD ["python", "main.py"]
