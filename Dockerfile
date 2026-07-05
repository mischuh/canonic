FROM python:3.13-slim

WORKDIR /app

RUN pip install uv

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev

COPY canonic/ ./canonic/

ENTRYPOINT ["uv", "run", "canonic"]
