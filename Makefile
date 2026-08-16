.PHONY: up down init bootstrap connectors verify logs clean env test-gateway rate-limit-check

COMPOSE = docker compose -f docker-compose.yml -f docker-compose.apps.yml

# ---------------------------------------------------------------------------
# env
# ---------------------------------------------------------------------------
# Never clobber an existing .env. It holds a generated JWT_SECRET and is
# gitignored, so overwriting it silently swaps the signing key out from under
# every issued token. The previous `init` ran `cp .env.example .env`
# unconditionally and `clean` deleted it outright.
env:
	@if [ -f .env ]; then \
		echo ".env exists — leaving it alone"; \
	else \
		cp .env.example .env; \
		SECRET=$$(openssl rand -hex 32 2>/dev/null || python -c "import secrets;print(secrets.token_hex(32))"); \
		sed -i.bak "s|^JWT_SECRET=.*|JWT_SECRET=$$SECRET|" .env && rm -f .env.bak; \
		echo ".env created with a generated JWT_SECRET"; \
	fi

# ---------------------------------------------------------------------------
# full bring-up from nothing
# ---------------------------------------------------------------------------
# Ordering matters: infrastructure, then services (so their containers exist
# for the schema bootstrap), then schema, then CDC connectors. Debezium can
# only capture tables that already exist, so connectors must come last.
init: env
	$(COMPOSE) up -d postgres redis kafka zookeeper elasticsearch schema-registry
	@echo "Waiting for infrastructure..."
	@sleep 20
	$(COMPOSE) up -d
	@echo "Waiting for services to register..."
	@sleep 20
	$(MAKE) bootstrap
	$(MAKE) connectors
	@echo ""
	@echo "Platform up. Run 'make verify' to prove it works."

# Schema from empty: model DDL, then SQL migrations, then assertions.
# Exits non-zero on any failure — a broken schema must not look like success.
bootstrap:
	python scripts/bootstrap_schema.py

# One Debezium connector per service database, each with a distinct
# replication slot.
connectors:
	python fix_connectors.py

# ---------------------------------------------------------------------------
# verification
# ---------------------------------------------------------------------------
verify:
	@cd tests/e2e && PYTHONIOENCODING=utf-8 sh -c '\
		pass=0; fail=0; \
		for t in test_01_catalog_write_to_read_sync test_02_cqrs_distributed_updates \
		         test_03_saga_orchestrator test_04_full_checkout_flow \
		         test_05_auxiliary_services test_06_intra_mesh_connectivity; do \
			if python $$t.py >/dev/null 2>&1; then \
				echo "  PASS  $$t"; pass=$$((pass+1)); \
			else \
				echo "  FAIL  $$t"; fail=$$((fail+1)); \
			fi; \
		done; \
		echo "  ---------------------------------"; \
		echo "  passed: $$pass  failed: $$fail"; \
		[ $$fail -eq 0 ]'

# Unit tests for the gateway's rate-limit decisions. node:test ships with
# Node 18+, so there is no dev dependency to install and no stack to bring up.
test-gateway:
	cd services/api-gateway && node --test "test/**/*.test.js"

# Burst check against a running gateway. The script is piped in over stdin
# rather than bind-mounted: no service image ships curl, and MSYS rewrites the
# container half of a -v mount on Windows. tests/integration/README.md has the
# per-shell invocations for machines without make.
rate-limit-check:
	@docker run --rm -i --network ecommerce-platform_mesh curlimages/curl:8.5.0 \
	  sh -s < tests/integration/rate_limit_burst.sh

# ---------------------------------------------------------------------------
# day to day
# ---------------------------------------------------------------------------
up:
	$(COMPOSE) up -d

down:
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f

# DESTRUCTIVE: -v deletes ecommerce-platform_postgres_data and every other
# volume. All order, payment and catalog data is gone and is not recoverable.
# .env is deliberately NOT removed: it is gitignored and holds the generated
# JWT_SECRET, and regenerating it invalidates every issued token.
clean:
	@echo "This deletes ALL database volumes. Order, payment and catalog data will be lost."
	@printf "Type 'yes' to continue: " && read ans && [ "$$ans" = "yes" ]
	$(COMPOSE) down -v
	@echo "Volumes removed. Run 'make init' to rebuild from empty."
