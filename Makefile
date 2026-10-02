.PHONY: run lint format rails-selftest

run:
	.venv/bin/uvicorn app.main:app --reload

lint:
	.venv/bin/ruff check .
	.venv/bin/black --check .

format:
	.venv/bin/ruff check . --fix
	.venv/bin/black .

rails-selftest:
	.venv/bin/python -m app.services.custody.loop_selftest
