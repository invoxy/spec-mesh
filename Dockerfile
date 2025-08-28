# Multi-stage build for faster dependency installation
FROM python:3.13.3-alpine AS builder

WORKDIR /app

# Copy requirements and install Python dependencies
COPY pyproject.toml .
RUN pip install uv && uv sync

# Production stage with Caddy
FROM python:3.13.3-alpine AS work

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv

RUN apk add caddy

# Copy source code
COPY src/ ./src/
COPY static/ ./static/

# Copy startup script
COPY start.sh ./
RUN chmod +x start.sh

# Create directory for Caddy config
RUN mkdir -p /etc/caddy

EXPOSE 8000 

ENV CADDY_AVAILABLE=true

CMD ["sh", "start.sh"]
