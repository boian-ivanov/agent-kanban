# Contributing to agent-kanban

Thanks for considering a contribution. This is a small project — keep PRs
focused, write a test if you can, document any new env-var.

## Dev setup

```bash
git clone <your-fork>
cd agent-kanban
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m kanban_ui      # http://localhost:7777
```

## Running tests

```bash
.venv/bin/pytest tests/ -v
```

Currently covered: PLAN.md parser (loose-mode + canonical + idempotency).
Adding tests for store / API endpoints is welcome.

## Code style

- `ruff` + `black`-compatible formatting (4-space indent, 100-char lines OK).
- Type hints encouraged; not enforced.
- Comments only when the *why* is non-obvious. The *what* is what the code says.

## Common changes

### Adding a new column / status

Three places need updating in lockstep:

1. [`kanban_store/store.py`](kanban_store/store.py) — append to `STATUSES`
   list and `status_meta()` (label + owner).
2. [`kanban_ui/automation/plan_md.py`](kanban_ui/automation/plan_md.py) —
   add aliases to `HEADING_TO_STATUS`.
3. [`kanban_ui/static/app.js`](kanban_ui/static/app.js) — `STATUS_TITLE`
   for UI label.

Schema doesn't need migration — `tasks.status` is a free-form TEXT column.

### Adding an env var

1. Read it in code.
2. Document in [`README.md`](README.md) under the "Configuration" table.
3. Mention in [`CONTRIBUTING.md`](CONTRIBUTING.md) only if it affects dev workflow.

### Adding a REST endpoint

1. Define handler in [`kanban_ui/main.py`](kanban_ui/main.py).
2. Define request/response Pydantic models above handlers.
3. Regenerate static schema — `make openapi` (or run command in
   [`Makefile`](Makefile)) — and commit `docs/openapi.yaml`.

## Commit style

[Conventional Commits](https://www.conventionalcommits.org):

```
feat: add bidirectional plan_md sync
fix(parser): strip trailing whitespace in title
docs: clarify CORS env-var
```

## License

By contributing you agree your code is MIT-licensed (see [LICENSE](LICENSE)).
DCO/CLA not required.
