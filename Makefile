COMPOSE=docker compose

.PHONY: up down logs build ps validate

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

