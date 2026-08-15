.PHONY: up down init logs clean

up:
	docker-compose up -d

down:
	docker-compose down

init:
	cp .env.example .env
	docker-compose up -d postgres redis kafka elasticsearch
	@echo "Waiting for databases to initialize..."
	sleep 10
	docker-compose up -d

logs:
	docker-compose logs -f

clean:
	docker-compose down -v
	rm -f .env
