.PHONY: install dev test lint typecheck quality us-equities portfolio monitor app \
        binary-options binary-options-health binary-options-validate-catalog clean

install:
	pip install --upgrade pip
	pip install -e ".[dev,pdf]"

dev: install

test:
	pytest

lint:
	ruff check src tests

# Static typing is scoped to the fully annotated shared + catalog/evaluation
# layers; broadening it across the legacy modules is tracked in the roadmaps.
typecheck:
	mypy --ignore-missing-imports src/trading_common src/binary_options_selection/catalog

# Offline quality gate (mirrors .github/workflows/quality.yml)
quality: lint test
	python -c "import asset_selection, binary_options_selection, trading_common, trading_app.services; print('imports OK')"

us-equities:
	python -m asset_selection.pipelines.run_asset_selection \
	    --config configs/us_equities/default.yaml --universe sample --limit 50 --top 20

portfolio:
	python -m asset_selection.pipelines.run_portfolio_allocation \
	    --config configs/us_equities/portfolio.yaml --amount 10000

# HOLDINGS=path/to/holdings.csv (or a statement .pdf); see configs/us_equities/holdings_template.csv
HOLDINGS ?= configs/us_equities/holdings_template.csv
monitor:
	python -m asset_selection.pipelines.run_portfolio_monitor \
	    --config configs/us_equities/monitoring.yaml --holdings $(HOLDINGS)

# Local web app (needs: pip install -e ".[app]", Python >= 3.10)
app:
	python -m trading_app.cli

binary-options:
	python -m binary_options_selection.pipelines.run_binary_options_selection \
	    --config configs/binary_options/default.yaml \
	    --instrument-catalog data/binary_options/input/instruments.yaml

binary-options-health:
	python -m binary_options_selection.pipelines.run_binary_options_selection \
	    --health-check-only

binary-options-validate-catalog:
	python -m binary_options_selection.pipelines.run_binary_options_selection \
	    --validate-catalog-only \
	    --instrument-catalog data/binary_options/input/instruments.yaml

# Legacy target names (aliases)
run: us-equities
run-binary-options: binary-options

clean:
	rm -rf data/us_equities/cache/* data/binary_options/cache/*
	rm -rf data/us_equities/processed/* data/binary_options/processed/*
	rm -rf reports/us_equities/* reports/binary_options/latest
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf .pytest_cache .ruff_cache .mypy_cache build dist *.egg-info
