.PHONY: lint format install-hooks design raycast

lint:
	uv run pre-commit run --all-files

format:
	uv run ruff check --fix .
	uv run ruff format .

install-hooks:
	uv run pre-commit install

design:
	uv run scripts/check_design.py

raycast:
	cd raycast && npm install && npm run build
	@echo "$(CURDIR)/raycast/dist"
