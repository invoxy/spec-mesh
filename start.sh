#!/bin/bash

# Startup script for API service with Caddy

set -e

echo "Starting API service with Caddy..."

# Start Caddy in background
echo "Starting Caddy..."
caddy run --config /etc/caddy/Caddyfile --adapter caddyfile &
CADDY_PID=$!

# Wait a moment for Caddy to start
sleep 2

# Check if Caddy is running
if kill -0 $CADDY_PID 2>/dev/null; then
    echo "Caddy started successfully (PID: $CADDY_PID)"
else
    echo "Warning: Caddy failed to start"
fi

# Start the API service
echo "Starting API service..."
exec /app/.venv/bin/uv run src/main.py
