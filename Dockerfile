# One image for the two Cloud Run services: the API (default command) and the
# web app (command `veloflow-app`). Both only read the forecast document from
# its public URL: no model, no data and no credentials in the image.
#
# Build and deploy: infra/gcp/deploy_cloud_run.ps1 (Cloud Build, no local Docker).

FROM python:3.12-slim-bookworm

# uv, pinned to the same version as the laptop, the VM and CI.
COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependencies first, in their own layer: a code change does not reinstall them.
# Runtime dependencies and the web app group; no development tools.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-default-groups --group app --no-install-project

COPY src ./src
RUN uv sync --frozen --no-default-groups --group app

# Unprivileged user: the services write nothing.
RUN useradd --system --no-create-home veloflow
USER veloflow

# VELOFLOW_FORECASTS (where to read the document) is set at deployment.
ENV PATH="/app/.venv/bin:${PATH}" \
    PORT=8080

EXPOSE 8080
CMD ["veloflow-api"]
