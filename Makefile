.PHONY: help install lint contracts test test-realha sim addon-lint shellcheck analyze-report ci
PY ?= python3
PORT ?= 8099
SH_FILES := $(shell git ls-files -co --exclude-standard '*.sh')

help:
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | sed 's/:.*##/\t/'

install: ## install dev dependencies
	$(PY) -m pip install -q -r requirements-dev.txt

lint: ## ruff + yamllint
	$(PY) -m ruff check .
	$(PY) -m yamllint -s -c .yamllint.yaml agent_house/config.yaml repository.yaml contracts .github

contracts: ## validate OpenAPI + JSON Schemas + examples
	$(PY) -m pytest tests/unit/test_contracts.py

test: ## hermetic unit + integration tests (simulators in-process)
	$(PY) -m pytest

test-realha: ## opt-in: test the HA client against a real headless Home Assistant Core
	$(PY) -m pytest -m realha tests/integration

sim: ## fake HA + fake LLM + fake Anthropic + fake Jetson metrics + the app behind a fake ingress
	$(PY) -m sim.run --port $(PORT)

addon-lint: ## static checks of the add-on packaging (CI also runs the app linter + hadolint)
	$(PY) tools/addon_lint.py agent_house

shellcheck: ## shellcheck every tracked shell script
	shellcheck -x $(SH_FILES)

analyze-report: ## read owner-reports/ and update docs/DECISIONS.md
	$(PY) tools/analyze_report.py owner-reports

ci: lint contracts test addon-lint shellcheck ## everything CI runs except the image build
