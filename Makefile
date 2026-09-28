# ASHS demo operator interface. Everything the demo needs is a target here.
SHELL := /bin/bash

# Credentials and provider config live in ONE place: ./.env at the repo root.
# Compose otherwise resolves .env relative to each compose file's directory and
# would silently ignore it, which looks exactly like "my keys don't work".
ENV_FILE ?= .env
DC := docker compose --env-file $(ENV_FILE)

STACKS := services/cap-factors services/cap-calc services/cfc-product platform/obs platform

CFC  := http://localhost:8000
CALC := http://localhost:8081
FACT := http://localhost:8082
CTRL := http://localhost:8090

.DEFAULT_GOAL := help
.PHONY: help env net up build down reset reset-source demo demo-prep clear-incidents execute logs ps health smoke verify verify-phase0 verify-phase1 verify-phase2 verify-phase3 incidents llm tools restart-control check-bedrock demo-scenario-1 demo-rollback three-runs heal-off

help: ## Show available targets
	@grep -hE '^[a-zA-Z0-9_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

env: ## Create .env from the template if missing
	@test -f $(ENV_FILE) || (cp .env.example $(ENV_FILE) && \
	  echo "created $(ENV_FILE) from .env.example -- add your AWS credentials")

net: env ## Create the shared mesh network (idempotent)
	@docker network inspect ashs-mesh >/dev/null 2>&1 || docker network create ashs-mesh
	@echo "mesh network ready"

build: net ## Build all images
	@for s in $(STACKS); do echo "==> build $$s"; $(DC) -f $$s/docker-compose.yml build; done

up: net ## Bring up every stack (obs first so no telemetry is dropped)
	@$(DC) -f platform/obs/docker-compose.yml up -d
	@$(DC) -f platform/docker-compose.yml up -d ashs-db ashs-control
	@$(DC) -f services/cap-factors/docker-compose.yml up -d
	@$(DC) -f services/cap-calc/docker-compose.yml up -d
	@$(DC) -f services/cfc-product/docker-compose.yml up -d
	@$(DC) -f platform/docker-compose.yml up -d traffic-gen ashs-ui
	@echo ""
	@echo "  DASHBOARD    http://localhost:3001   <- project this"
	@echo "  Product      $(CFC)      UI  http://localhost:3000"
	@echo "  Control      $(CTRL)"
	@echo "  calc-api     $(CALC)/docs   factor-api $(FACT)/docs"
	@echo ""
	@echo "  Run 'make health' in ~30s, then 'make verify'."

down: ## Stop everything, keep volumes
	@for s in $(STACKS); do $(DC) -f $$s/docker-compose.yml down 2>/dev/null || true; done

reset-source: ## Restore service source the agent may have patched
	@cp .baseline/cfc_api_main.py services/cfc-product/api/src/cfc_api/main.py
	@cp .baseline/factor_api_main.py services/cap-factors/src/factor_api/main.py
	@rm -f services/cfc-product/api/tests/test_*.py services/cap-factors/tests/test_*.py
	@echo "service source restored to baseline"

reset: reset-source ## Full reset: source restored, volumes wiped, data reseeded
	@echo "==> tearing down with volumes"
	@for s in $(STACKS); do $(DC) -f $$s/docker-compose.yml down -v 2>/dev/null || true; done
	@$(MAKE) --no-print-directory up
	@echo "==> reset complete"

ps: ## Container status across all stacks
	@docker ps --filter "network=ashs-mesh" --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"

logs: ## Tail control-plane logs
	@$(DC) -f platform/docker-compose.yml logs -f ashs-control

health: ## Health of every service
	@printf "factor-api  "; curl -fsS $(FACT)/health   | head -c 120 || echo "DOWN"; echo
	@printf "calc-api    "; curl -fsS $(CALC)/health   | head -c 120 || echo "DOWN"; echo
	@printf "cfc-api     "; curl -fsS $(CFC)/api/health| head -c 160 || echo "DOWN"; echo
	@printf "control     "; curl -fsS $(CTRL)/health   | head -c 120 || echo "DOWN"; echo

smoke: ## One real calculation through the full chain
	@curl -fsS -X POST $(CFC)/api/footprint/calculate \
	  -H 'Content-Type: application/json' \
	  -d '{"period":"monthly","region":"IN-KA","activities":{"electricity_kwh":320,"petrol_car_km":800,"rail_km":120,"flights_short_haul":1,"diet":"medium_meat","waste_kg":40,"recycling_percent":30}}' \
	  | python3 -m json.tool | head -40

verify: verify-phase0 verify-phase1 verify-phase2 verify-phase3 ## Run all phase exit tests

verify-phase0: ## PHASE 0: one calculation -> one trace across all services
	@python3 scripts/verify_phase0.py

verify-phase1: ## PHASE 1: detect + deterministic attribution, no LLM
	@python3 scripts/verify_phase1.py

verify-phase2: ## PHASE 2: plan, policy gate, and proven refusals
	@python3 scripts/verify_phase2.py

verify-phase3: ## PHASE 3: autonomous repair, and rollback on failure
	@python3 scripts/verify_phase3.py

demo-prep: ## T-2min: ready the demo. KEEPS incident history; restores source only.
	@$(MAKE) --no-print-directory reset-source
	@echo "==> waiting for cfc-api to reload the restored source"
	@until curl -fsS $(CFC)/api/health >/dev/null 2>&1; do sleep 1; done
	@python3 scripts/demo_prep.py
	@echo "  ready. history kept -- 'make clear-incidents' wipes the board."

clear-incidents: ## Clear the incident board (audit trail included). Telemetry kept.
	@docker exec ashs-ashs-db-1 psql -U ashs -d ashs -qc \
	   "TRUNCATE ash_incidents, ash_audit, ash_metrics, ash_escalations CASCADE;" >/dev/null
	@echo "  incident board cleared"

demo: ## THE 5-MINUTE DEMO: the whole loop, one screen (run demo-prep first)
	@python3 scripts/demo_5min.py

demo-scenario-1: ## Scenario 1 end to end, fully narrated (ROLLBACK=1 to revert)
	@python3 scripts/demo_scenario_1.py

three-runs: ## ACCEPTANCE: three consecutive clean runs from full reset
	@python3 scripts/three_clean_runs.py

demo-rollback: ## Scenario 1 with validation forced to fail -> revert
	@ROLLBACK=1 python3 scripts/demo_scenario_1.py

incidents: ## Show open incidents and their attribution
	@curl -fsS $(CTRL)/api/incidents | python3 -c 'import json,sys; d=json.load(sys.stdin); [print(f"{i[\"incident_id\"]}  {i[\"state\"]:<16} fault={i[\"fault_domain\"]}  conf={i[\"confidence\"]}") for i in d["incidents"]] or print("no incidents")'

restart-control: ## Reload ashs-control (picks up .env changes)
	@$(DC) -f platform/docker-compose.yml up -d --force-recreate ashs-control >/dev/null
	@echo "ashs-control restarted; give it ~15s then run 'make llm'"

check-bedrock: ## Make one REAL Bedrock call to verify credentials
	@$(DC) -f platform/docker-compose.yml cp scripts/bedrock_probe.py ashs-control:/tmp/probe.py 2>/dev/null || \
	  docker cp scripts/bedrock_probe.py ashs-ashs-control-1:/tmp/probe.py
	@docker exec ashs-ashs-control-1 python /tmp/probe.py

llm: ## Show LLM provider status
	@curl -fsS $(CTRL)/api/llm/status | python3 -m json.tool

tools: ## Show the typed tool surface
	@curl -fsS $(CTRL)/api/tools | python3 -c 'import json,sys; d=json.load(sys.stdin); print(f"{d[\"count\"]} tools"); [print(f"  {\"MUTATES\" if t[\"mutates\"] else \"read   \"}  {t[\"name\"]}") for t in d["tools"]]'

execute: ## Force the repair loop on the newest approved incident
	@bash -c 'ID=$$(curl -fsS $(CTRL)/api/incidents | python3 -c "import json,sys; print(json.load(sys.stdin)[\"incidents\"][0][\"incident_id\"])"); \
	  curl -fsS -X POST $(CTRL)/api/incidents/$$ID/execute | python3 -m json.tool'

heal-off: ## Clear all demo bug flags
	@curl -fsS -X POST $(CFC)/_demo/bug -H 'Content-Type: application/json' \
	  -d '{"name":"zero_total_division","enabled":false}' >/dev/null && echo "flags cleared"
