.PHONY: run lint format

run:
	.venv/bin/uvicorn app.main:app --reload

lint:
	.venv/bin/ruff check .
	.venv/bin/black --check .

format:
	.venv/bin/ruff check . --fix
	.venv/bin/black .
