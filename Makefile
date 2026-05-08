.PHONY: help dev test openapi clean

help:
	@echo "  make dev       — start dev server on http://localhost:7777"
	@echo "  make test      — run pytest"
	@echo "  make openapi   — regenerate docs/openapi.yaml from FastAPI app"
	@echo "  make clean     — remove caches"

dev:
	.venv/bin/python -m kanban_ui

test:
	.venv/bin/pytest tests/ -v

openapi:
	.venv/bin/python -c "import yaml; from kanban_ui.main import app; print(yaml.dump(app.openapi(), allow_unicode=True, sort_keys=False))" > docs/openapi.yaml
	@echo "wrote docs/openapi.yaml"

clean:
	find . -type d -name '__pycache__' -prune -exec rm -rf {} +
	rm -rf .pytest_cache
