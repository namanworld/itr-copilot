$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

uv run ruff check .
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

uv run ruff format --check .
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

uv run mypy
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

uv run lint-imports
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

uv run pytest
exit $LASTEXITCODE
