#!/usr/bin/env bash

set -e

cd client/server

echo "==> Starting server..."
venv/bin/uvicorn main:app --host 127.0.0.1 --port 8765 &

SERVER_PID=$!

echo "==> Starting client..."
cd ..

python3 -m http.server 5173 --bind 127.0.0.1 &

CLIENT_PID=$!

trap 'kill $SERVER_PID $CLIENT_PID 2>/dev/null' EXIT

sleep 1

xdg-open http://127.0.0.1:5173

wait