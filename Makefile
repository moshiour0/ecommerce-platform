.PHONY: up down init bootstrap storage connectors replication redis-check verify logs clean env test-gateway rate-limit-check

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
	$(COMPOSE) up -d postgres redis minio zookeeper kafka kafka-2 kafka-3 elasticsearch schema-registry
	@echo "Waiting for infrastructure..."
	@sleep 20
	$(COMPOSE) up -d
	@echo "Waiting for services to register..."
	@sleep 20
	$(MAKE) bootstrap
	$(MAKE) storage
	$(MAKE) connectors
	$(MAKE) replication
	$(MAKE) redis-check
	@echo ""
	@echo "Platform up. Run 'make verify' to prove it works."

# Schema from empty: model DDL, then SQL migrations, then assertions.
# Exits non-zero on any failure — a broken schema must not look like success.
bootstrap:
	python scripts/bootstrap_schema.py

# Media bucket and its access policy, generated from the purpose taxonomy.
storage:
	python scripts/bootstrap_storage.py

# One Debezium connector per service database, each with a distinct
# replication slot.
connectors:
	python fix_connectors.py

# Topics created before the third broker existed keep one replica forever.
# Reports first, then reassigns anything below RF=3.
replication:
	python scripts/kafka_replication.py --fix

# Replication and sentinel quorum for Redis. Reports only -- there is nothing
# safe to repair automatically here; a broken pair needs a person.
redis-check:
	python scripts/check_redis.py

# ---------------------------------------------------------------------------
# verification
# ---------------------------------------------------------------------------
verify:
	@cd tests/e2e && PYTHONIOENCODING=utf-8 sh -c '\
		pass=0; fail=0; \
		for t in test_01_catalog_write_to_read_sync test_02_cqrs_distributed_updates \
		         test_03_saga_orchestrator test_04_full_checkout_flow \
		         test_05_auxiliary_services test_06_intra_mesh_connectivity \
		         test_07_cart_cache_coherence test_08_rate_limit_state_loss \
		         test_11_seller_onboarding test_12_order_splitting \
		         test_13_cod_lifecycle test_14_courier_and_settlement \
		         test_15_escrow_ledger test_16_seller_dashboard \
		         test_17_ranking_and_metrics test_18_reviews \
		         test_09_broker_loss test_10_redis_failover; do \
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
# Bare `node --test` on purpose: naming the files explicitly is not portable
# across Node 18 and Node 24. See the header of the test file.
test-gateway:
	cd services/api-gateway && node --test

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

# DESTRUCTIVE: -v deletes ecommerce-platform_postgres_data, redis_data and
# every other volume -- now including the Kafka broker logs, the MinIO
# bucket and the Redis replica. All order, payment and catalog data is gone
# and is not recoverable, along with every live cart, rate-limit counter,
# uploaded media object and retained event.
# .env is deliberately NOT removed: it is gitignored and holds the generated
# JWT_SECRET, and regenerating it invalidates every issued token.
clean:
	@echo "This deletes ALL volumes. Order, payment and catalog data will be lost,"
	@echo "along with every cart and rate-limit counter held in Redis."
	@printf "Type 'yes' to continue: " && read ans && [ "$$ans" = "yes" ]
	$(COMPOSE) down -v
	@echo "Volumes removed. Run 'make init' to rebuild from empty."
