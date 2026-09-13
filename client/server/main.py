import asyncio
import base64
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from telethon import TelegramClient, events


load_dotenv(
    Path(__file__).parent / ".env"
)

os.environ.pop("HTTP_PROXY", None)
os.environ.pop("HTTPS_PROXY", None)
os.environ.pop("ALL_PROXY", None)
os.environ.pop("http_proxy", None)
os.environ.pop("https_proxy", None)
os.environ.pop("all_proxy", None)

API_ID = int(
    os.environ["TELEGRAM_API_ID"]
)

API_HASH = os.environ[
    "TELEGRAM_API_HASH"
]

BOT_USERNAME = os.environ[
    "TELEGRAM_BOT_USERNAME"
]


logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s | "
        "%(levelname)s | "
        "%(message)s"
    ),
)

logger = logging.getLogger(
    "telegram_remote_client"
)


telegram_client = TelegramClient(
    "telegram_remote",
    API_ID,
    API_HASH,
    proxy=None
)


websocket_clients: set[WebSocket] = set()

telegram_queue: asyncio.Queue | None = None

telegram_sender_task: asyncio.Task | None = None

input_sender_task: asyncio.Task | None = None

INPUT_BATCH_INTERVAL = 0.05

TELEGRAM_MIN_INTERVAL = 1.1

input_buffer = bytearray()

input_lock = asyncio.Lock()

input_event = asyncio.Event()

current_mode = "command"

current_status = "initializing"

restoring = False

restore_cutoff_id = 0

pending_live_messages = []

restored_data = []


async def broadcast(data):
    if not websocket_clients:
        return

    message = json.dumps(data)

    dead_clients = []

    for websocket in list(
        websocket_clients
    ):
        try:
            await websocket.send_text(
                message
            )
        except Exception:
            logger.exception(
                "WebSocket send error."
            )
            dead_clients.append(
                websocket
            )

    for websocket in dead_clients:
        websocket_clients.discard(
            websocket
        )


async def send_control(command: str):
    logger.info(
        "Control -> Telegram: %s",
        command,
    )

    await telegram_client.send_message(
        BOT_USERNAME,
        command,
    )


async def telegram_sender():
    logger.info(
        "Telegram sender started."
    )

    last_send_time = 0.0

    while True:
        message = await telegram_queue.get()

        try:
            now = (
                asyncio
                .get_running_loop()
                .time()
            )

            if last_send_time != 0.0:
                elapsed = (
                    now - last_send_time
                )

                if (
                    elapsed
                    < TELEGRAM_MIN_INTERVAL
                ):
                    await asyncio.sleep(
                        TELEGRAM_MIN_INTERVAL
                        - elapsed
                    )

            logger.info(
                "Telegram -> %s",
                message[:100],
            )

            await telegram_client.send_message(
                BOT_USERNAME,
                message,
            )

            last_send_time = (
                asyncio
                .get_running_loop()
                .time()
            )

        except asyncio.CancelledError:
            raise

        except Exception:
            logger.exception(
                "Telegram send error."
            )

            await asyncio.sleep(
                2.0
            )

        finally:
            telegram_queue.task_done()


async def queue_input(data: bytes):
    async with input_lock:
        input_buffer.extend(data)

    input_event.set()


async def input_sender():
    logger.info(
        "Input sender started."
    )

    while True:
        await input_event.wait()

        await asyncio.sleep(
            INPUT_BATCH_INTERVAL
        )

        async with input_lock:
            if not input_buffer:
                input_event.clear()
                continue

            data = bytes(
                input_buffer
            )

            input_buffer.clear()

            input_event.clear()

        encoded = (
            base64.b64encode(
                data
            ).decode("ascii")
        )

        message = (
            f"/input {encoded}"
        )

        logger.info(
            "Input -> Telegram: %d bytes",
            len(data),
        )

        await telegram_queue.put(
            message
        )


async def process_telegram_message(
    message,
):
    global current_mode
    global current_status

    text = message.raw_text or ""

    logger.info(
        "Telegram <- %s",
        text[:100],
    )

    if text.startswith("/data "):
        data = text[
            len("/data "):
        ]

        if restoring:
            restored_data.append(
                data
            )
        else:
            raw_data = (
                base64.b64decode(
                    data
                )
            )

            if not raw_data:
                return

            await broadcast({
                "type": "data",
                "data": (
                    base64.b64encode(
                        raw_data
                    ).decode("ascii")
                ),
            })

        return

    if text.startswith("/mode "):
        mode = text[
            len("/mode "):
        ].strip()

        if mode not in {
            "command",
            "raw",
        }:
            return

        current_mode = mode

        if not restoring:
            await broadcast({
                "type": "mode",
                "mode": mode,
            })

        return

    if text == "/ready":
        current_status = "ready"

        if not restoring:
            await broadcast({
                "type": "status",
                "status": "ready",
            })

        return

    if text == "/closed":
        current_status = "closed"

        if not restoring:
            await broadcast({
                "type": "status",
                "status": "closed",
            })

        return


async def restore_session():
    global restoring
    global restore_cutoff_id
    global pending_live_messages

    logger.info(
        "Restoring Telegram session..."
    )

    latest = (
        await telegram_client.get_messages(
            BOT_USERNAME,
            limit=1,
        )
    )

    if latest:
        restore_cutoff_id = (
            latest[0].id
        )
    else:
        restore_cutoff_id = 0

    messages = []

    async for message in (
        telegram_client.iter_messages(
            BOT_USERNAME,
        )
    ):
        if message.id > restore_cutoff_id:
            continue

        if message.out:
            continue

        text = (
            message.raw_text or ""
        )

        if text == "/session":
            break

        messages.append(message)

    messages.reverse()

    restored_data.clear()

    logger.info(
        "Replaying %d Telegram message(s)",
        len(messages),
    )

    for message in messages:
        await process_telegram_message(
            message
        )

    buffered = sorted(
        pending_live_messages,
        key=lambda message: message.id,
    )

    pending_live_messages.clear()

    for message in buffered:
        if message.id <= restore_cutoff_id:
            continue

        await process_telegram_message(
            message
        )

    restoring = False

    logger.info(
        "Session restored: "
        "status=%s mode=%s data_chunks=%d",
        current_status,
        current_mode,
        len(restored_data),
    )


@telegram_client.on(
    events.NewMessage(
        chats=BOT_USERNAME,
        incoming=True,
    )
)
async def handle_telegram_message(
    event,
):
    message = event.message

    if restoring:
        pending_live_messages.append(
            message
        )
        return

    if message.id <= restore_cutoff_id:
        return

    await process_telegram_message(
        message
    )


@asynccontextmanager
async def lifespan(app):
    global telegram_queue
    global telegram_sender_task
    global input_sender_task
    global restoring

    telegram_queue = asyncio.Queue()

    restoring = True

    logger.info(
        "Starting Telegram client..."
    )

    await telegram_client.start()

    logger.info(
        "Telegram client started."
    )

    await send_control(
        "/active"
    )

    logger.info(
        "Polling -> ACTIVE"
    )

    await restore_session()

    telegram_sender_task = (
        asyncio.create_task(
            telegram_sender()
        )
    )

    input_sender_task = (
        asyncio.create_task(
            input_sender()
        )
    )

    logger.info(
        "Background tasks started."
    )

    try:
        yield

    finally:
        logger.info(
            "Stopping Telegram client..."
        )

        try:
            await send_control(
                "/idle"
            )

            logger.info(
                "Polling -> IDLE"
            )

        except Exception:
            logger.exception(
                "Failed to send /idle."
            )

        tasks = []

        for task in (
            telegram_sender_task,
            input_sender_task,
        ):
            if task is not None:
                task.cancel()
                tasks.append(task)

        if tasks:
            await asyncio.gather(
                *tasks,
                return_exceptions=True,
            )

        await telegram_client.disconnect()

        logger.info(
            "Telegram client stopped."
        )


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
    allow_methods=[
        "GET",
        "POST",
    ],
    allow_headers=[
        "*",
    ],
)


@app.websocket("/ws")
async def websocket_endpoint(
    websocket: WebSocket,
):
    await websocket.accept()

    websocket_clients.add(
        websocket
    )

    logger.info(
        "WebSocket connected. Clients=%d",
        len(websocket_clients),
    )

    await websocket.send_text(
        json.dumps({
            "type": "status",
            "status": current_status,
        })
    )

    await websocket.send_text(
        json.dumps({
            "type": "mode",
            "mode": current_mode,
        })
    )

    for data in restored_data:
        await websocket.send_text(
            json.dumps({
                "type": "data",
                "data": data,
            })
        )

    try:
        while True:
            message = (
                await websocket.receive_text()
            )

            try:
                payload = json.loads(
                    message
                )

            except Exception:
                logger.exception(
                    "Invalid WebSocket JSON."
                )
                continue

            if payload.get(
                "type"
            ) != "input":
                logger.warning(
                    "Unknown WebSocket message: %s",
                    payload,
                )
                continue

            encoded = payload.get(
                "data"
            )

            if not encoded:
                continue

            try:
                data = (
                    base64.b64decode(
                        encoded,
                        validate=True,
                    )
                )

            except Exception:
                logger.exception(
                    "Invalid input Base64."
                )
                continue

            if not data:
                continue

            logger.info(
                "WebSocket -> input: %r",
                data,
            )

            await queue_input(
                data
            )

    except WebSocketDisconnect:
        logger.info(
            "WebSocket disconnected."
        )

    except Exception:
        logger.exception(
            "WebSocket error."
        )

    finally:
        websocket_clients.discard(
            websocket
        )

        logger.info(
            "WebSocket removed. Clients=%d",
            len(websocket_clients),
        )