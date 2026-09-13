#!/usr/bin/env bash

set -e

SERVER_DIR="server"

echo "==> Creating virtual environment..."

if [ ! -d "$SERVER_DIR/venv" ]; then
    python3 -m venv "$SERVER_DIR/venv"
fi

echo "==> Installing dependencies..."

"$SERVER_DIR/venv/bin/pip" install -r "$SERVER_DIR/requirements.txt"

echo "==> Checking .env..."

if [ ! -f "$SERVER_DIR/.env" ]; then
    echo "ERROR: $SERVER_DIR/.env not found"
    exit 1
fi

for var in TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID; do
    if ! grep -q "^${var}=" "$SERVER_DIR/.env"; then
        echo "ERROR: ${var} is missing in $SERVER_DIR/.env"
        exit 1
    fi
done

echo "==> Setup completed."