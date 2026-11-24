# RAG

RAG supports company, industry and person research, a company knowledge workspace, and an H5 workbench.

## Local development

Use Python 3.12 and install dependencies with `uv sync --locked`. Copy `.env.example` to `.env` and set local values. Keep credentials in the environment file. Use a separate local database and storage configuration.

On Windows, run `pwsh -File scripts/start-local.ps1` and verify the startup with `uv run python scripts/check-local.py`. With the configured services already running, start the API with `uv run uvicorn backend.app:app --host 127.0.0.1 --port 8010`. Run tests with `uv run pytest`.

For staging, database migration, release packaging, and H5 setup, see [Release workflow](docs/RELEASE_WORKFLOW.md).
