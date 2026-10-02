.PHONY: lint format install-hooks design

lint:
	uv run pre-commit run --all-files

format:
	uv run ruff check --fix .
	uv run ruff format .

install-hooks:
	uv run pre-commit install

design:
	uv run scripts/check_design.py
