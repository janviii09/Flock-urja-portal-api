VENV ?= .venv
PYTHON ?= $(if $(wildcard $(VENV)/bin/python3),$(VENV)/bin/python3,python3)
UVICORN ?= $(if $(wildcard $(VENV)/bin/uvicorn),$(VENV)/bin/uvicorn,uvicorn)
RUFF ?= $(if $(wildcard $(VENV)/bin/ruff),$(VENV)/bin/ruff,ruff)

.PHONY: install run test lint format openapi

install:
	$(PYTHON) -m pip install -e ".[dev]"

run:
	$(UVICORN) urja.api.app:app --reload --port 8000

test:
	$(PYTHON) -m pytest -v

lint:
	$(RUFF) check src/ tests/
	$(RUFF) format --check src/ tests/

format:
	$(RUFF) check --fix src/ tests/
	$(RUFF) format src/ tests/

openapi:
	$(PYTHON) -c "import json; from urja.api.app import app; print(json.dumps(app.openapi(), indent=2))" > openapi.json
