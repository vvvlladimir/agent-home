.PHONY: help install lint contracts test test-realha sim polygon polygon-claude qa-claude addon-lint shellcheck analyze-report gui gui-check ci
VENV ?= .venv
PY ?= $(VENV)/bin/python
PORT ?= 8099
SH_FILES := $(shell git ls-files -co --exclude-standard '*.sh')

help:
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | sed 's/:.*##/\t/'

install: ## create .venv and install dev dependencies into it
	test -x $(PY) || python3 -m venv $(VENV)
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

sim: ## the app in a simulated home (:8100) + the sim panel (:8090) to steer the house and the model backends
	$(PY) -m sim.run --port $(PORT)

polygon: ## headless scenario runs with invariant checks; ARGS="--room-llm openai" for a real model
	$(PY) -m sim.polygon run --scenario $(or $(SCENARIO),all) $(ARGS)

polygon-claude: ## all scenarios with every model as `claude -p` (no API keys, D30)
	$(PY) -m sim.polygon run --scenario $(or $(SCENARIO),all) --room-llm claude --orchestrator claude --no-pace $(ARGS)

qa-claude: ## AI testers + coordinator + verifier, all on Claude Code (no API keys, ~1 h, D30)
	$(PY) -m sim.qa claude $(ARGS)

addon-lint: ## static checks of the add-on packaging (CI also runs the app linter + hadolint)
	$(PY) tools/addon_lint.py agent_house

shellcheck: ## shellcheck every tracked shell script
	shellcheck -x $(SH_FILES)

analyze-report: ## read owner-reports/ and update docs/DECISIONS.md
	$(PY) tools/analyze_report.py owner-reports

gui: ## build the Vue GUI (agent_house/frontend) into agent_house/app/web/static
	cd agent_house/frontend && npm ci --no-audit --no-fund && npm run build

gui-check: gui ## the committed GUI build matches its sources
	@git add -N agent_house/app/web/static
	@git diff --exit-code --stat -- agent_house/app/web/static || (echo "run 'make gui' and commit agent_house/app/web/static"; exit 1)

ci: lint contracts test addon-lint shellcheck ## everything CI runs except the image build
