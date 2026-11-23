FROM python:3.14-slim AS build

RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH
COPY factory_board/ /build/factory_board/
COPY pyproject.toml requirements.txt /build/
RUN pip install --no-cache-dir -r /build/requirements.txt /build

FROM python:3.14-slim AS prod

COPY --from=build /opt/venv /opt/venv
# The migrations ship with the image; `python -m factory_board migrate` applies them.
COPY migrations/ /app/migrations/
ENV PATH=/opt/venv/bin:$PATH \
    FACTORY_BOARD_MIGRATIONS_DIR=/app/migrations
WORKDIR /app

# Non-root; the pod also sets runAsNonRoot and a read-only root filesystem.
USER 65532:65532
CMD ["python", "-m", "factory_board", "migrate"]

# Lint, type check and tests, on the Python the image runs.
FROM prod AS dev
USER root
COPY requirements-dev.txt /tmp/requirements-dev.txt
RUN pip install --no-cache-dir -r /tmp/requirements-dev.txt
COPY pyproject.toml /app/
COPY factory_board/ /app/factory_board/
COPY tests/ /app/tests/
ENV HOME=/tmp \
    PYTHONDONTWRITEBYTECODE=1
USER 65532:65532
CMD ["python", "-m", "pytest", "-p", "no:cacheprovider"]
