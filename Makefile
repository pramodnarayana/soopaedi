.PHONY: build lint format typecheck test check-all dev infra-up infra-down infra-logs

# ==============================================================================
# EDI AS2 - Enterprise Task Runner
# ==============================================================================

build:
	@echo "=== Building Backend ==="
	uv sync --dev --all-packages
	@echo "=== Building Frontend ==="
	cd frontend/web && pnpm install && pnpm build

lint:
	@echo "=== Linting Backend ==="
	uv run ruff check libs/ services/
	@echo "=== Linting Frontend ==="
	cd frontend/web && pnpm lint

format:
	@echo "=== Formatting Backend ==="
	uv run ruff format libs/ services/

typecheck:
	@echo "=== Typechecking Backend ==="
	uv run mypy libs/ services/
	@echo "=== Typechecking Frontend ==="
	cd frontend/web && pnpm tsc --noEmit

test:
	@echo "=== Testing Backend (Unit) ==="
	uv run pytest libs/ services/ -m "not integration" --cov=. --cov-report=term-missing
	@echo "=== Testing Backend (Integration) ==="
	uv run pytest libs/ services/ -m "integration"
	@echo "=== Testing Frontend ==="
	# cd frontend/web && pnpm test (enable when Vitest is scaffolded)

check-all: format lint typecheck test
	@echo "All quality checks passed!"

# --- Local Development Server ---

dev:
	@echo "Starting both Frontend and Backend concurrently..."
	pnpm dlx concurrently -c "blue,magenta" -n "api,web" "make dev-api" "make dev-web"

dev-as2:
	@echo "Starting AS2 Server with hot-reload for local development..."
	uv run uvicorn as2_server.main:app --reload --port 8000

dev-api:
	@echo "Starting API Gateway with hot-reload for local development..."
	uv run uvicorn api.main:app --reload --port 8001

dev-web:
	@echo "Starting React Frontend with Vite..."
	cd frontend/web && pnpm dev

db-init:
	@echo "Waiting for databases to be ready..."
	@sleep 5
	@echo "Running database migrations..."
	PYTHONPATH=libs/database/src:libs/config/src uv run python libs/database/src/database/run_migrations.py
	@echo "Seeding the database..."
	PYTHONPATH=libs/database/src:libs/config/src uv run python services/as2_server/scripts/seed.py

db-reset:
	@echo "Wiping application databases (leaving Zitadel intact)..."
	docker compose rm -s -f -v postgres_global postgres_shard_1
	@echo "Restarting application databases..."
	docker compose up -d postgres_global postgres_shard_1
	@echo "Waiting for databases to initialize..."
	sleep 5
	@echo "Re-running migrations and seeding..."
	$(MAKE) db-init

seed: db-init

# --- Docker Infrastructure (Postgres, LocalStack, OTel) ---

infra-up:
	@echo "Starting local infrastructure (Background DB, S3, OTel)..."
	docker compose -f ../docker-compose.yml up -d
	@echo "\n=> Infrastructure started! Run 'make db-init' to apply migrations and seed data."

infra-down:
	@echo "Stopping local infrastructure..."
	docker compose -f ../docker-compose.yml down

infra-logs:
	@echo "Tailing infrastructure logs..."
	docker compose -f ../docker-compose.yml logs -f
