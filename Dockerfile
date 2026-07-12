FROM python:3.13-slim AS builder

WORKDIR /build

# Compilation tools remain in the builder stage and never enter the runtime image.
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements/python-3.13-runtime.lock ./requirements/python-3.13-runtime.lock
RUN python -m venv /opt/xiaoqing-venv \
    && /opt/xiaoqing-venv/bin/pip install --no-cache-dir --require-hashes \
        -r requirements/python-3.13-runtime.lock

# The context builder fixes the validated release artifact at this exact path.
# Dependencies are installed only from the hashed lock above, never from wheel metadata.
COPY artifacts/xiaoqing.whl ./artifacts/xiaoqing.whl
RUN /opt/xiaoqing-venv/bin/pip install --no-cache-dir --no-deps \
        --target /opt/xiaoqing-app artifacts/xiaoqing.whl


FROM python:3.13-slim AS runtime

WORKDIR /app

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app
ENV PATH=/opt/xiaoqing-venv/bin:$PATH

COPY --from=builder /opt/xiaoqing-venv /opt/xiaoqing-venv
COPY --from=builder /opt/xiaoqing-app /app
COPY config/config.json.example ./config/config.json.example
COPY config/secrets.json.example ./config/secrets.json.example

# Real config.json and secrets.json must be injected at runtime via a volume or
# secret mount. The trusted-admin model intentionally retains container root;
# never mount the Docker socket or host paths outside the admin's authority.
RUN mkdir -p logs config

EXPOSE 12000

CMD ["python", "/app/main.py"]
