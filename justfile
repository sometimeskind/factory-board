# Everything runs in the image's dev stage with podman, so a local run uses the Python
# the image ships (the host's may be newer and hide errors CI then finds).

image := "factory-board"
pg_name := "factory-board-test-pg"
pg_port := "55434"

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

# What CI runs.
check: lint typecheck test build
