# Everything runs in the image's dev stage with podman, so a local run uses the Python
# the image ships (the host's may be newer and hide errors CI then finds).

image := "factory-board"
pg_name := "factory-board-test-pg"
pg_port := "55434"
pg_image_port := "55435"

# List the recipes.
default:
    @just --list

# Build the production image.
build:
    podman build --target prod -t {{image}}:local .

# Build the dev stage: the production image plus ruff, pyright, pytest and the tests.
dev-image:
    podman build -q --target dev -t {{image}}:dev . >/dev/null

# Ruff: lint and format check.
lint: dev-image
    podman run --rm {{image}}:dev sh -c 'ruff check --no-cache . && ruff format --no-cache --check .'

# Reformat the working tree in place.
fmt: dev-image
    podman run --rm --user 0 -v "$PWD":/app:Z {{image}}:dev sh -c 'ruff check --no-cache --fix . ; ruff format --no-cache .'

# Pyright, strict.
typecheck: dev-image
    podman run --rm {{image}}:dev pyright

# Tests against a throwaway Postgres 17 (the major the cluster runs). Extra arguments go to pytest.
# The URL is the superuser's: the fixtures create the roles from dev/roles.sql with it, then
# run the migrations as board_owner and the store as board_app.
test *args: dev-image
    #!/usr/bin/env bash
    set -euo pipefail
    podman run -d --rm --name {{pg_name}} -e POSTGRES_PASSWORD=test -e POSTGRES_DB=board_test \
      -p 127.0.0.1:{{pg_port}}:5432 docker.io/library/postgres:17 >/dev/null
    trap 'podman rm -f {{pg_name}} >/dev/null' EXIT
    until podman exec {{pg_name}} pg_isready -q -h 127.0.0.1; do sleep 1; done
    podman run --rm --network host \
      -e FACTORY_BOARD_TEST_DATABASE_URL=postgresql://postgres:test@127.0.0.1:{{pg_port}}/board_test \
      {{image}}:dev python -m pytest -p no:cacheprovider {{args}}

# The production image against a fresh database, as a deployment runs it: the roles are
# created by the superuser, then the image applies its migrations as board_owner, twice.
migrate-check: build
    #!/usr/bin/env bash
    set -euo pipefail
    podman run -d --rm --name {{pg_name}}-image -e POSTGRES_PASSWORD=test -e POSTGRES_DB=board_image \
      -p 127.0.0.1:{{pg_image_port}}:5432 docker.io/library/postgres:17 >/dev/null
    trap 'podman rm -f {{pg_name}}-image >/dev/null' EXIT
    until podman exec {{pg_name}}-image pg_isready -q -h 127.0.0.1; do sleep 1; done
    podman exec -i {{pg_name}}-image psql -q -v ON_ERROR_STOP=1 -U postgres -d board_image < dev/roles.sql
    for run in first second; do
      podman run --rm --network host --read-only \
        -e FACTORY_BOARD_MIGRATE_DATABASE_URL=postgresql://board_owner:board_owner@127.0.0.1:{{pg_image_port}}/board_image \
        {{image}}:local
    done

# What CI runs.
check: lint typecheck test migrate-check
