# Demo-only variant of the repo's own ../../Dockerfile, plus `git` — `canonic pack add`
# needs it on PATH to clone a --repo git URL (canonic/packs/repo.py::resolve_repo). Kept
# as a separate file rather than adding `git` to the shared root Dockerfile, which would
# grow every other image build (CI, production) for a dependency only this demo needs.
# Mirrors the root Dockerfile's build steps exactly; keep the two in sync if either
# changes.
FROM python:3.13-slim

RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN pip install uv

COPY pyproject.toml uv.lock README.md ./
COPY canonic/ ./canonic/
RUN uv sync --frozen --no-dev

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:7474/livez', timeout=2)"

ENTRYPOINT ["uv", "run", "canonic"]
