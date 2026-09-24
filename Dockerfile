FROM python:3.13-slim

WORKDIR /app

RUN pip install uv

COPY pyproject.toml uv.lock README.md ./
COPY canonic/ ./canonic/
RUN uv sync --frozen --no-dev

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:7474/livez', timeout=2)"

ENTRYPOINT ["uv", "run", "canonic"]
