VENV ?= .venv
PYTHON ?= $(if $(wildcard $(VENV)/Scripts/python.exe),$(VENV)/Scripts/python.exe,$(if $(wildcard $(VENV)/bin/python),$(VENV)/bin/python,python))
PYTHONDONTWRITEBYTECODE ?= 1
PYTHONPYCACHEPREFIX ?= /tmp/ipm-pycache
export PYTHONDONTWRITEBYTECODE
export PYTHONPYCACHEPREFIX
TYPECHECK_TARGETS := \
	app/security \
	app/ops \
	app/services/case/case_kind.py \
	app/services/case/case_parameter_service.py \
	app/services/case/canonical_field_service.py \
	app/services/automation \
	app/services/matching/auto_match_dataset.py \
	app/services/matter/matter_facts_service.py \
	app/services/uploads \
	app/services/ops/operational_metrics.py

.PHONY: fmt fmt-check lint lint-critical test test-cov test-cov-core typecheck typecheck-full audit-deps quality

runtime-reload:
	docker compose restart app scheduler worker

runtime-status:
	docker compose ps app scheduler worker

fmt:
	$(PYTHON) -m isort .
	$(PYTHON) -m black .

fmt-check:
	$(PYTHON) -m isort --check-only .
	$(PYTHON) -m black --check .

lint:
	$(PYTHON) -m ruff check .

lint-critical:
	$(PYTHON) -m ruff check --select B,S,C90 --ignore C901,S112,S608,B023 app/security app/ops app/services/uploads app/services/workflow app/services/automation app/services/matching
	$(PYTHON) -m ruff check --select F821 app tests run.py config.py

test:
	$(PYTHON) -m pytest

test-cov:
	$(PYTHON) -m pytest --cov=app --cov-report=term-missing

test-cov-core:
	$(PYTHON) -m pytest --cov=app --cov-report=term-missing --cov-report=json
	$(PYTHON) scripts/quality/check_core_coverage.py

typecheck:
	$(PYTHON) -m mypy --follow-imports=skip --ignore-missing-imports $(TYPECHECK_TARGETS)

typecheck-full:
	$(PYTHON) -m mypy app

audit-deps:
	$(PYTHON) -m pip_audit --progress-spinner off

quality: fmt-check lint lint-critical typecheck test
