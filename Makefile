PY ?= python3.11
VENV ?= .venv
BIN := $(VENV)/bin

.PHONY: install lint format typecheck test check simulate simulate-real calibrate-real report serve demo docker clean

install:
	$(PY) -m venv $(VENV)
	$(BIN)/pip install -e ".[dev]"

lint:
	$(BIN)/ruff check src tests scripts
	$(BIN)/ruff format --check src tests scripts

format:
	$(BIN)/ruff format src tests scripts
	$(BIN)/ruff check --fix src tests scripts

typecheck:
	$(BIN)/mypy

test:
	$(BIN)/pytest

check: lint typecheck test

simulate:  ## full comparison used in the README (takes several minutes)
	$(BIN)/wfo simulate --contacts 50000 --seeds 5 --workers 2 --out docs/results

calibrate-real:  ## re-derive configs/real_calibrated.json (downloads the benchmark CSVs)
	$(BIN)/python scripts/derive_real_calibration.py --out configs/real_calibrated.json

simulate-real:  ## real-calibrated comparison + sensitivity sweep (about 8 minutes on 2 cores)
	$(BIN)/wfo simulate --world configs/real_calibrated.json --contacts 20000 --seeds 5 \
		--workers 2 --out docs/results_real_calibrated
	$(BIN)/wfo sensitivity --world configs/real_calibrated.json --contacts 10000 --seeds 5 \
		--workers 2 --out docs/results_real_calibrated

report:
	$(BIN)/wfo report --results docs/results/results.json

demo:
	$(BIN)/wfo generate --n 500 --out contacts.csv
	$(BIN)/wfo run --input contacts.csv --output enriched.csv --strategy thompson

serve:
	$(BIN)/wfo serve --host 0.0.0.0 --port 8000

docker:
	docker build -t waterfall-optimizer .

clean:
	rm -rf .wfo .pytest_cache .mypy_cache .ruff_cache .hypothesis contacts.csv enriched.csv
