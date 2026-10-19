PY ?= python3
VENV = .venv
BIN = $(VENV)/bin

.PHONY: setup run test report ingest

setup:
	$(PY) -m venv $(VENV)
	$(BIN)/pip install -r requirements.txt

run:
	$(BIN)/streamlit run app.py

test:
	$(BIN)/python -m pytest -q

report:
	$(BIN)/python scripts/report.py

ingest:
	$(BIN)/python scripts/ingest.py
