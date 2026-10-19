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

# --- Docker: нужен только Docker, Python ставить не надо
.PHONY: up down docker-test

up:
	docker compose up --build -d
	@echo "Dashboard: http://localhost:8501"

down:
	docker compose down

docker-test:
	docker compose run --rm --no-deps dashboard python -m pytest -q -p no:cacheprovider
