FROM python:3.13-slim

WORKDIR /app

RUN pip install uv

COPY pyproject.toml uv.lock README.md ./
COPY canonic/ ./canonic/
RUN uv sync --frozen --no-dev

ENTRYPOINT ["uv", "run", "canonic"]
