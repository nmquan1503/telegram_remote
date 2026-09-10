#!/usr/bin/env bash

set -e

cd client/server

if [ ! -d "venv" ]; then
    echo "==> Creating virtual environment..."
    python3 -m venv venv
fi

echo "==> Installing dependencies..."
venv/bin/pip install -r requirements.txt

echo "==> Checking .env..."

if [ ! -f ".env" ]; then
    echo "ERROR: .env not found"
    exit 1
fi

for var in TELEGRAM_API_ID TELEGRAM_API_HASH TELEGRAM_BOT_USERNAME; do
    if ! grep -q "^${var}=" .env; then
        echo "ERROR: ${var} is missing in .env"
        exit 1
    fi
done

echo "==> Logging in to Telegram..."

venv/bin/python -c '
import asyncio
import os

from dotenv import load_dotenv
from telethon import TelegramClient

load_dotenv()

client = TelegramClient(
    "telegram_remote",
    int(os.environ["TELEGRAM_API_ID"]),
    os.environ["TELEGRAM_API_HASH"],
)

async def main():
    await client.start()
    print("==> Telegram login successful.")
    await client.disconnect()

asyncio.run(main())
'

echo "==> Setup completed."