.PHONY: setup run deploy stop install uninstall build install-pre-commit monitoring

SETUP_SENTINEL := .setup-complete

setup: $(SETUP_SENTINEL)

$(SETUP_SENTINEL):
	chmod +x setup.sh
	./setup.sh

# Run locally (dev mode)
run:
	docker compose up emqx postgres -d
	conda run --no-capture-output -n hummingbot-api uvicorn main:app --host 0.0.0.0 --port 8000 --reload

# Deploy with Docker
deploy: $(SETUP_SENTINEL)
	docker compose up -d

# Stop all services
stop:
	docker compose --profile monitoring down

# Bot monitoring: start InfluxDB and print the lines to add to .env
monitoring:
	docker compose --profile monitoring up -d influxdb
	@echo ""
	@echo "Add these lines to .env, then restart the API:"
	@echo "  INFLUXDB_ENABLED=true"
	@docker compose --profile monitoring exec -T influxdb sh -c 'for i in $$(seq 1 30); do test -f "$$INFLUXDB3_ADMIN_TOKEN_FILE" && break; sleep 1; done; sed -nE "s/.*\"token\": *\"([^\"]+)\".*/  INFLUXDB_TOKEN=\1/p" "$$INFLUXDB3_ADMIN_TOKEN_FILE"'
	@echo "  COMPOSE_PROFILES=monitoring    (so make deploy starts InfluxDB too)"

# Install conda environment
install:
	@if ! command -v conda >/dev/null 2>&1; then \
		echo "Error: Conda is not found in PATH. Please install Conda or add it to your PATH."; \
		exit 1; \
	fi
	@if conda env list | grep -q '^hummingbot-api '; then \
		echo "Environment already exists."; \
	else \
		conda env create -f environment.yml; \
	fi
	$(MAKE) install-pre-commit
	$(MAKE) setup

uninstall:
	conda env remove -n hummingbot-api -y
	rm -f $(SETUP_SENTINEL)

install-pre-commit:
	conda run -n hummingbot-api pip install pre-commit
	conda run -n hummingbot-api pre-commit install

# Build Docker image
build:
	docker build -t hummingbot/hummingbot-api:latest .