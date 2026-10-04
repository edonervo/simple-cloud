PYTHON ?= python3

.PHONY: help install-hooks secrets-check secrets-all test lint

help:
	@echo "make install-hooks   activate the secret-guard pre-commit hook (run once per clone)"
	@echo "make secrets-check   scan the tracked tree and the boundary rules"
	@echo "make secrets-all     scan staged, tree, history and boundaries"
	@echo "make test            run the test suite"
	@echo "make lint            run ruff if it is installed"

install-hooks:
	$(PYTHON) scripts/secret_guard.py install-hook

secrets-check:
	$(PYTHON) scripts/secret_guard.py check

secrets-all:
	$(PYTHON) scripts/secret_guard.py check --scope all

test:
	$(PYTHON) -m unittest discover -s test -t . -v

lint:
	@command -v ruff >/dev/null 2>&1 && ruff check . || echo "ruff not installed; skipping"
