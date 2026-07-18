PREFIX     ?= $(HOME)/.local
BIN_DIR    := $(PREFIX)/bin
PROJECT    := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
SOURCE     := $(PROJECT)/bin/pg
TARGET     := $(BIN_DIR)/pg

.PHONY: install uninstall reinstall

install:
	@if [ -e "$(TARGET)" ] && [ ! -L "$(TARGET)" ]; then \
	  echo "refusing to overwrite non-symlink: $(TARGET)"; exit 1; \
	fi
	@mkdir -p "$(BIN_DIR)"
	@ln -sf "$(SOURCE)" "$(TARGET)"
	@echo "linked $(TARGET) -> $(SOURCE)"

uninstall:
	@if [ -L "$(TARGET)" ]; then \
	  rm "$(TARGET)"; echo "removed $(TARGET)"; \
	else \
	  echo "no symlink at $(TARGET)"; \
	fi

reinstall: uninstall install

# ─── Dev stack (epic #61) ──────────────────────────────────────────────
# Isolated Django + brain dev environment. Never touches live data.
DEV_COMPOSE := docker compose -f docker-compose.dev.yml

.PHONY: dev-up dev-down dev-nuke dev-logs dev-migrate dev-test dev-test-integration

dev-up:           ## Bring up the isolated dev stack (postgres+mcp+web+mailpit+caddy)
	$(DEV_COMPOSE) up -d --build

dev-down:         ## Stop the dev stack, KEEPING volumes (safe)
	$(DEV_COMPOSE) down

dev-nuke:         ## Remove dev stack AND its volumes (only ever touches pgdata_dev)
	$(DEV_COMPOSE) down -v

dev-logs:
	$(DEV_COMPOSE) logs -f

dev-migrate:      ## Apply Django migrations in the running web container
	$(DEV_COMPOSE) exec web python manage.py migrate

dev-test:         ## Run the web unit tests inside the web container
	$(DEV_COMPOSE) exec web python -m pytest

dev-test-integration:  ## Run brain.* + mcp integration tests against the dev Postgres
	$(DEV_COMPOSE) exec web python -m pytest \
		openbrain/brain/tests/integration openbrain/mcp/tests/integration \
		openbrain/core/tests/integration \
		-m integration --ds=config.settings.test_integration
