.PHONY: lint format install-hooks proposal

lint:
	uv run pre-commit run --all-files

format:
	uv run ruff check --fix .
	uv run ruff format .

install-hooks:
	uv run pre-commit install

proposal:
	python3 scripts/build_proposal.py
	open docs/proposal/proposal.html
