.PHONY: install dev test lint run clean

install:
	pip install -r requirements.txt

dev:
	pip install -e ".[dev]"

test:
	pytest

lint:
	ruff check src tests

run:
	python -m asset_selection.pipelines.run_asset_selection \
	    --config configs/default_config.yaml --limit 50 --top 20

clean:
	rm -rf data/cache/* data/processed/* reports/*.md reports/*.json
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf .pytest_cache .ruff_cache build dist *.egg-info
