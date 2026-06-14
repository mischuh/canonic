# canon — Tech Stack Constitution
version: "1.0"
python: "3.13+"
package_manager: "uv"

core_libraries:
  cli:
    - typer: "~0.15"
    - rich: ">=13"
  
  config:
    - pydantic: ">=2.0,<3"
    - pydantic-settings: ">=2.14,<3"
    - ruamel-yaml: ">=0.18"
  
  database:
    - sqlalchemy: ">=2.0.50,<3"
    - sqlglot: ">=25.0"
    - asyncpg: ">=0.30"
    - aiosqlite: ">=0.20"
    - alembic: ">=1.13"
  
  llm:
    - litellm: ">=1.0"
    - fastmcp: ">=3.0,<4"
  
  search:
    - tantivy-py: ">=0.22"
    - numpy: ">=2.0"
    - sentence-transformers: ">=3.0"  # optional
  
  async:
    - asyncio: "built-in"

server:
  - fastapi: ">=0.115"
  - uvicorn: ">=0.32"

dev_tools:
  - pytest: ">=8"
  - pytest-asyncio: ">=0.24"
  - hypothesis: ">=6"
  - mypy: "latest"
  - ruff: "latest"

key_decisions:
  sql_parser: "SQLGlot (not Calcite)"
  async_orm: "SQLAlchemy 2.0 Core"
  mcp_framework: "FastMCP"
  config_validation: "Pydantic v2"
  search_backend: "tantivy-py (BM25) + numpy (vectors)"