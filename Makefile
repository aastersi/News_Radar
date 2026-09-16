.PHONY: install test lint typecheck check dry-run

install:
	python -m pip install -e ".[dev,telegram]"

test:
	pytest

lint:
	ruff check .

typecheck:
	mypy src

check: lint typecheck test

dry-run:
	qmemo-radar dry-run

