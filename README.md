# sql-control-cli

Architecture: Python command-line application.

Implementation stack: Python 3.11+, uv, argparse, pytest, ruff, and pre-commit.

This starter keeps the command entrypoint small, testable, and usable offline.

## Local Development

```bash
uv sync --group dev
uv run pytest
uv run ruff check .
```
