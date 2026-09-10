import json
import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from telethon import TelegramClient, events


load_dotenv(Path(__file__).parent / ".env")

API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]
BOT_USERNAME = os.environ["TELEGRAM_BOT_USERNAME"]

telegram_client = TelegramClient(
    "telegram_remote",
    API_ID,
    API_HASH,
)

websocket_clients: set[WebSocket] = set()

active_command = None
active_log = None


async def recover_state():
    global active_command, active_log

    messages = await telegram_client.get_messages(
        BOT_USERNAME,
        limit=100,
    )

    command_message = None

    for message in messages:
        if not message.out:
            continue

        text = message.raw_text.strip()

        if text.startswith("/command "):
            command_message = message
            break

    if command_message is None:
        return

    command = command_message.raw_text[len("/command "):].strip()

    newer_messages = [
        message
        for message in messages
        if message.id > command_message.id
    ]

    if any(
        message.raw_text.rstrip().endswith("/end")
        for message in newer_messages
    ):
        return

    bot_messages = [
        message
        for message in newer_messages
        if not message.out
    ]

    active_command = command

    if bot_messages:
        active_log = bot_messages[-1].raw_text
    else:
        active_log = ""


@telegram_client.on(events.NewMessage(chats=BOT_USERNAME))
async def handle_telegram_message(event):
    global active_log

    message = event.raw_text

    if message.rstrip().endswith("/end"):
        active_log = message[:-4].rstrip()

        for websocket in list(websocket_clients):
            try:
                await websocket.send_text(
                    json.dumps({
                        "type": "end",
                        "log": active_log,
                    })
                )
            except Exception:
                websocket_clients.discard(websocket)

        active_log = None
        return

    active_log = message

    for websocket in list(websocket_clients):
        try:
            await websocket.send_text(
                json.dumps({
                    "type": "log",
                    "log": message,
                })
            )
        except Exception:
            websocket_clients.discard(websocket)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await telegram_client.start()
    await recover_state()

    yield

    await telegram_client.disconnect()


app = FastAPI(
    title="Telegram Remote",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
    allow_methods=["POST"],
    allow_headers=["Content-Type"],
)


class CommandRequest(BaseModel):
    command: str


@app.post("/command")
async def send_command(request: CommandRequest):
    global active_command, active_log

    command = request.command.strip()

    if not command:
        return {
            "ok": False,
            "error": "Command is empty",
        }

    active_command = command
    active_log = ""

    await telegram_client.send_message(
        BOT_USERNAME,
        f"/command {command}",
    )

    return {"ok": True}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    websocket_clients.add(websocket)

    if active_command is not None:
        await websocket.send_text(
            json.dumps({
                "type": "state",
                "command": active_command,
                "log": active_log or "",
            })
        )

    try:
        while True:
            await websocket.receive_text()

    except WebSocketDisconnect:
        pass

    finally:
        websocket_clients.discard(websocket)