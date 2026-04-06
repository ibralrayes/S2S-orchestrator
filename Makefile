COMPOSE=docker compose

.PHONY: up down logs build ps validate demo demo-down

up:
	$(COMPOSE) up --build

down:
	$(COMPOSE) down --remove-orphans

logs:
	$(COMPOSE) logs -f --tail=200

build:
	$(COMPOSE) build

ps:
	$(COMPOSE) ps

validate:
	python -m compileall agent token-server

demo:
	$(COMPOSE) --profile demo up --build

demo-down:
	$(COMPOSE) --profile demo down --remove-orphans

