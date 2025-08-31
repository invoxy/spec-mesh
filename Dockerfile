FROM python:3.13.3-alpine AS maturin

RUN apk add --no-cache \
    rust \
    cargo \
    musl-dev \
    openssl-dev \
    pkgconfig \
    gcc \
    libc-dev \
    linux-headers

RUN pip install maturin patchelf

WORKDIR /module

COPY openapi-merge/ .

RUN maturin build --release

FROM ghcr.io/astral-sh/uv:python3.13-alpine AS dependencies

WORKDIR /app

COPY pyproject.toml .
RUN uv sync

COPY --from=maturin /module/target/wheels/* ./modules/

RUN uv pip install modules/*

# Fix permissions for virtual environment
RUN chmod -R 755 /app/.venv/bin && \
    ls -la /app/.venv/bin/

FROM python:3.13.3-alpine AS production

RUN apk add --no-cache caddy
WORKDIR /app

COPY --from=dependencies /app /app
COPY src/ ./src/

# Create Caddy config directory
RUN mkdir -p /etc/caddy

# Expose port
EXPOSE 8000

COPY ./static ./static/
COPY start.sh .

ENV PATH="/app/.venv/bin:$PATH"


# Environment variable
ENV CADDY_AVAILABLE=true

CMD ["sh","start.sh"]
