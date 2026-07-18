.PHONY: install dev test lint run run-binary-options clean

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

run-binary-options:
	python -m binary_options_selection.pipelines.run_binary_options_selection \
	    --config configs/binary_options_config.yaml --top 20

clean:
	rm -rf data/cache/* data/processed/* reports/*.md reports/*.json
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf .pytest_cache .ruff_cache build dist *.egg-info
