# Contributing

Thank you for considering an improvement to `radar-core`.

## The admission rule

A change belongs here only when it is reusable infrastructure and does not need
to understand an application's domain. Provider adapters, product contracts,
matching rules, schemas and rendered alert text belong in the consuming app.

When in doubt, keep the behavior in the application until at least three
independent consumers need the same abstraction.

## Development workflow

Requirements: Python 3.12+ and `uv`.

```bash
uv sync --locked --dev
uv run pytest -q
uv run ruff check .
uv run mypy radar_core
uv build
```

Before opening a pull request:

1. Add or update tests for observable behavior.
2. Keep public APIs typed and narrowly scoped.
3. Preserve graceful shutdown and failure-handling guarantees.
4. Do not add credentials, production data or real account identifiers.
5. Update `CHANGELOG.md` when the change affects consumers.

## Pull requests

Keep pull requests focused. Explain the failure mode or repeated pattern being
addressed, the guarantee the change introduces, and how the tests prove it.

Security vulnerabilities must follow [`SECURITY.md`](SECURITY.md) and must not
be reported in a public issue.
