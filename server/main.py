import asyncio
import base64
import errno
import logging
import os
import pty
import threading
import time
from pathlib import Path

from dotenv import load_dotenv
from telegram import Update
from telegram.error import RetryAfter
from telegram.ext import (
    Application,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest


load_dotenv(
    Path(__file__).parent / ".env"
)

BOT_TOKEN = os.environ[
    "TELEGRAM_BOT_TOKEN"
]

CHAT_ID = int(
    os.environ["TELEGRAM_CHAT_ID"]
)


MAX_DATA_LENGTH = 2800
REALTIME_OUTPUT_COUNT = 5
OUTPUT_MIN_INTERVAL = 1.0
TELEGRAM_MIN_INTERVAL = 1.1
IDLE_POLL_TIMEOUT = 60
ACTIVE_POLL_TIMEOUT = 1
ACTIVE_IDLE_TIMEOUT = 600


logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s | "
        "%(levelname)s | "
        "%(message)s"
    ),
)

logging.getLogger("httpx").setLevel(
    logging.WARNING
)

logging.getLogger("httpcore").setLevel(
    logging.WARNING
)

logger = logging.getLogger(
    "telegram_remote"
)


master_fd = None
shell_pid = None

output_buffer = bytearray()
output_lock = threading.Lock()

telegram_queue = None
telegram_loop = None
output_event = None

current_mode = None
mode_lock = threading.Lock()

capture_output = False
terminal_closed_event = threading.Event()

poll_mode = "idle"
last_input_time = None


def log_data(prefix, data):
    logger.info(
        "%s | bytes=%d",
        prefix,
        len(data),
    )

    text = data.decode(
        "utf-8",
        errors="replace",
    )

    text = (
        text
        .replace("\x1b", "<ESC>")
        .replace("\r", "<CR>")
        .replace("\n", "<LF>")
    )

    logger.info(
        "%s | data=%s",
        prefix,
        text,
    )


def start_terminal():
    global master_fd
    global shell_pid

    logger.info(
        "Starting PTY"
    )

    pid, fd = pty.fork()

    if pid == 0:
        logger.info(
            "PTY child | configuring environment"
        )

        os.environ["TERM"] = (
            "xterm-256color"
        )

        os.environ["COLORTERM"] = (
            "truecolor"
        )

        logger.info(
            "PTY child | executing bash"
        )

        os.execvp(
            "bash",
            [
                "bash",
                "-i",
            ],
        )

    shell_pid = pid
    master_fd = fd

    logger.info(
        "PTY started | pid=%s | fd=%s",
        shell_pid,
        master_fd,
    )

    threading.Thread(
        target=read_terminal,
        daemon=True,
        name="pty-reader",
    ).start()

    logger.info(
        "PTY reader thread started"
    )

    threading.Thread(
        target=watch_terminal_mode,
        daemon=True,
        name="mode-watcher",
    ).start()

    logger.info(
        "Mode watcher thread started"
    )


def read_terminal():
    logger.info(
        "PTY reader started"
    )

    while True:
        try:
            logger.info(
                "PTY reader | waiting for output"
            )

            data = os.read(
                master_fd,
                4096,
            )

            logger.info(
                "PTY reader | read %d bytes",
                len(data),
            )

        except OSError as error:
            if error.errno == errno.EIO:
                logger.info(
                    "PTY reader | PTY closed | EIO"
                )
            else:
                logger.exception(
                    "PTY reader | read error"
                )

            break

        if not data:
            logger.info(
                "PTY reader | EOF"
            )
            break

        log_data(
            "PTY output",
            data,
        )

        logger.info(
            "PTY reader | capture_output=%s",
            capture_output,
        )

        if capture_output:
            with output_lock:
                before = len(output_buffer)

                output_buffer.extend(data)

                after = len(output_buffer)

            logger.info(
                "Output buffer | appended=%d | before=%d | after=%d",
                len(data),
                before,
                after,
            )

            if telegram_loop is not None:
                logger.info(
                    "Output event | scheduling SET"
                )

                telegram_loop.call_soon_threadsafe(
                    output_event.set
                )
            else:
                logger.warning(
                    "Output event | Telegram loop unavailable"
                )

    terminal_closed_event.set()

    logger.info(
        "Terminal closed event SET"
    )

    publish_from_thread(
        "/closed"
    )


def take_output():
    with output_lock:
        size = len(output_buffer)

        if not output_buffer:
            logger.info(
                "take_output | buffer empty"
            )
            return b""

        data = bytes(
            output_buffer
        )

        output_buffer.clear()

    logger.info(
        "take_output | took=%d bytes | buffer_after=0",
        size,
    )

    return data


def has_output():
    with output_lock:
        result = bool(
            output_buffer
        )

        size = len(output_buffer)

    logger.info(
        "has_output | result=%s | size=%d",
        result,
        size,
    )

    return result


def clear_output():
    with output_lock:
        size = len(output_buffer)

        output_buffer.clear()

    logger.info(
        "clear_output | cleared=%d bytes",
        size,
    )


def send_to_terminal(data):
    logger.info(
        "send_to_terminal | fd=%s",
        master_fd,
    )

    log_data(
        "Input",
        data,
    )

    if master_fd is None:
        logger.warning(
            "send_to_terminal | PTY unavailable"
        )
        return

    try:
        written = os.write(
            master_fd,
            data,
        )

        logger.info(
            "send_to_terminal | wrote=%d/%d bytes",
            written,
            len(data),
        )

    except OSError:
        logger.exception(
            "PTY write error"
        )


def get_terminal_mode():
    if master_fd is None:
        logger.debug(
            "get_terminal_mode | PTY unavailable"
        )
        return None

    try:
        foreground_pgid = (
            os.tcgetpgrp(
                master_fd
            )
        )
    except OSError:
        logger.exception(
            "get_terminal_mode | tcgetpgrp failed"
        )
        return None

    try:
        shell_pgid = os.getpgid(
            shell_pid
        )
    except OSError:
        logger.exception(
            "get_terminal_mode | getpgid failed"
        )
        return None

    if foreground_pgid == shell_pgid:
        mode = "command"
    else:
        mode = "raw"

    logger.debug(
        "get_terminal_mode | foreground_pgid=%s | shell_pgid=%s | mode=%s",
        foreground_pgid,
        shell_pgid,
        mode,
    )

    return mode


async def enqueue_telegram(text):
    logger.info(
        "Telegram queue | enqueue | text=%s",
        text[:120],
    )

    await telegram_queue.put(
        text
    )

    logger.info(
        "Telegram queue | size=%d",
        telegram_queue.qsize(),
    )


def publish_from_thread(text):
    logger.info(
        "publish_from_thread | text=%s",
        text[:120],
    )

    if telegram_loop is None:
        logger.warning(
            "publish_from_thread | Telegram loop unavailable"
        )
        return

    asyncio.run_coroutine_threadsafe(
        enqueue_telegram(text),
        telegram_loop,
    )

    logger.info(
        "publish_from_thread | scheduled"
    )


def watch_terminal_mode():
    global current_mode

    logger.info(
        "Mode watcher started"
    )

    while True:
        mode = get_terminal_mode()

        if mode is not None:
            with mode_lock:
                changed = (
                    mode != current_mode
                )

                if changed:
                    previous_mode = (
                        current_mode
                    )

                    current_mode = mode
                else:
                    previous_mode = mode

            if changed:
                logger.info(
                    "Mode changed | %s -> %s",
                    previous_mode,
                    mode,
                )

                publish_from_thread(
                    f"/mode {mode}"
                )

        time.sleep(
            0.05
        )

async def telegram_sender(bot):
    logger.info(
        "Telegram sender started"
    )
    last_send_time = 0.0

    while True:
        logger.info(
            "Telegram sender | waiting queue"
        )
        text = await telegram_queue.get()

        try:
            if text.startswith("/data "):
                data_parts = [
                    text[len("/data "):]
                ]

                while not telegram_queue.empty():
                    next_text = telegram_queue.get_nowait()

                    if next_text.startswith("/data "):
                        data_parts.append(
                            next_text[len("/data "):]
                        )
                        telegram_queue.task_done()
                    else:
                        await telegram_queue.put(
                            next_text
                        )
                        telegram_queue.task_done()
                        break

                data = b"".join(
                    base64.b64decode(part)
                    for part in data_parts
                )

                logger.info(
                    "Telegram sender | merged /data | "
                    "messages=%d | bytes=%d",
                    len(data_parts),
                    len(data),
                )

                offset = 0

                while offset < len(data):
                    chunk = data[
                        offset:offset + MAX_DATA_LENGTH
                    ]
                    offset += len(chunk)

                    encoded = base64.b64encode(
                        chunk
                    ).decode("ascii")

                    elapsed = (
                        time.monotonic()
                        - last_send_time
                    )

                    if (
                        elapsed
                        < TELEGRAM_MIN_INTERVAL
                    ):
                        await asyncio.sleep(
                            TELEGRAM_MIN_INTERVAL
                            - elapsed
                        )

                    while True:
                        try:
                            logger.info(
                                "Telegram sender | sending "
                                "/data | bytes=%d",
                                len(chunk),
                            )

                            await bot.send_message(
                                chat_id=CHAT_ID,
                                text=f"/data {encoded}",
                            )

                            last_send_time = (
                                time.monotonic()
                            )

                            logger.info(
                                "Telegram sender | SENT "
                                "/data | bytes=%d",
                                len(chunk),
                            )
                            break

                        except RetryAfter as error:
                            logger.warning(
                                "Telegram sender | rate limited "
                                "| retry=%.2fs",
                                error.retry_after,
                            )
                            await asyncio.sleep(
                                error.retry_after
                            )

                        except Exception:
                            logger.exception(
                                "Telegram sender | send error"
                            )
                            await asyncio.sleep(2.0)

                continue

            elapsed = (
                time.monotonic()
                - last_send_time
            )

            if (
                elapsed
                < TELEGRAM_MIN_INTERVAL
            ):
                await asyncio.sleep(
                    TELEGRAM_MIN_INTERVAL
                    - elapsed
                )

            while True:
                try:
                    logger.info(
                        "Telegram sender | sending | text=%s",
                        text[:120],
                    )

                    await bot.send_message(
                        chat_id=CHAT_ID,
                        text=text,
                    )

                    last_send_time = (
                        time.monotonic()
                    )

                    logger.info(
                        "Telegram sender | SENT | text=%s",
                        text[:120],
                    )
                    break

                except RetryAfter as error:
                    logger.warning(
                        "Telegram sender | rate limited "
                        "| retry=%.2fs",
                        error.retry_after,
                    )
                    await asyncio.sleep(
                        error.retry_after
                    )

                except Exception:
                    logger.exception(
                        "Telegram sender | send error"
                    )
                    await asyncio.sleep(2.0)

        finally:
            telegram_queue.task_done()


async def output_sender():
    logger.info(
        "Output sender started"
    )
    output_count = 0
    last_output_send_time = 0.0

    while True:
        logger.info(
            "Output sender | waiting event"
        )
        await output_event.wait()

        logger.info(
            "Output sender | event received"
        )

        while True:
            data = take_output()

            if not data:
                logger.info(
                    "Output sender | no data"
                )
                output_event.clear()

                logger.info(
                    "Output sender | event cleared"
                )

                if has_output():
                    logger.info(
                        "Output sender | data appeared after clear"
                    )
                    output_event.set()
                    continue

                break

            logger.info(
                "Output sender | processing %d bytes",
                len(data),
            )

            while data:
                chunk = data[:MAX_DATA_LENGTH]
                data = data[MAX_DATA_LENGTH:]

                logger.info(
                    "Output sender | chunk=%d bytes | "
                    "output_count=%d",
                    len(chunk),
                    output_count,
                )

                if (
                    output_count
                    >= REALTIME_OUTPUT_COUNT
                ):
                    elapsed = (
                        time.monotonic()
                        - last_output_send_time
                    )

                    logger.info(
                        "Output sender | elapsed=%.3f | "
                        "min_interval=%.3f",
                        elapsed,
                        OUTPUT_MIN_INTERVAL,
                    )

                    if (
                        elapsed
                        < OUTPUT_MIN_INTERVAL
                    ):
                        delay = (
                            OUTPUT_MIN_INTERVAL
                            - elapsed
                        )

                        logger.info(
                            "Output sender | sleeping %.3fs",
                            delay,
                        )

                        await asyncio.sleep(
                            delay
                        )

                encoded = (
                    base64.b64encode(
                        chunk
                    ).decode("ascii")
                )

                logger.info(
                    "Output sender | enqueue /data | "
                    "raw_bytes=%d | encoded_bytes=%d",
                    len(chunk),
                    len(encoded),
                )

                await enqueue_telegram(
                    f"/data {encoded}"
                )

                output_count += 1
                last_output_send_time = (
                    time.monotonic()
                )

                logger.info(
                    "Output sender | /data queued | "
                    "output_count=%d",
                    output_count,
                )

            if has_output():
                logger.info(
                    "Output sender | more output available"
                )
                output_event.set()

                continue

            output_event.clear()

            logger.info(
                "Output sender | buffer drained | "
                "event cleared"
            )

            if has_output():
                logger.info(
                    "Output sender | new output arrived"
                )
                output_event.set()
                continue

            break


async def handle_input(text):
    global poll_mode
    global last_input_time

    logger.info(
        "handle_input | received | text_length=%d",
        len(text),
    )

    encoded = text[
        len("/input"):
    ].strip()

    logger.info(
        "handle_input | encoded_length=%d",
        len(encoded),
    )

    if not encoded:
        logger.warning(
            "Empty input"
        )
        return

    try:
        data = base64.b64decode(
            encoded,
            validate=True,
        )

        logger.info(
            "handle_input | decoded=%d bytes",
            len(data),
        )

    except Exception:
        logger.exception(
            "Invalid input"
        )
        return

    if poll_mode != "active":
        logger.info(
            "Polling -> ACTIVE"
        )

        poll_mode = "active"

    last_input_time = (
        time.monotonic()
    )

    logger.info(
        "handle_input | last_input_time updated"
    )

    send_to_terminal(
        data
    )


async def handle_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    global poll_mode
    global last_input_time

    message = update.message

    if not message:
        logger.warning(
            "handle_message | no message"
        )
        return

    logger.info(
        "handle_message | chat_id=%s",
        message.chat_id,
    )

    if message.chat_id != CHAT_ID:
        logger.warning(
            "Ignored message from %s",
            message.chat_id,
        )
        return

    text = message.text or ""

    logger.info(
        "handle_message | text=%s",
        text[:120],
    )

    if text == "/active":
        poll_mode = "active"

        last_input_time = (
            time.monotonic()
        )

        logger.info(
            "Polling -> ACTIVE"
        )

    elif text == "/idle":
        poll_mode = "idle"

        last_input_time = None

        logger.info(
            "Polling -> IDLE"
        )

    elif text.startswith("/input"):
        logger.info(
            "handle_message | routing to handle_input"
        )

        await handle_input(
            text
        )

    else:
        logger.info(
            "handle_message | ignored command"
        )


async def adaptive_polling(application):
    global poll_mode
    global last_input_time

    offset = None

    logger.info(
        "Adaptive polling started"
    )

    while True:
        if poll_mode == "active":
            timeout = ACTIVE_POLL_TIMEOUT
        else:
            timeout = IDLE_POLL_TIMEOUT

        mode = poll_mode.upper()

        logger.info(
            "Polling | %s | offset=%s | waiting up to %ss",
            mode,
            offset,
            timeout,
        )

        poll_start = time.monotonic()

        updates = await application.bot.get_updates(
            offset=offset,
            timeout=timeout,
        )

        poll_elapsed = (
            time.monotonic()
            - poll_start
        )

        logger.info(
            "Polling | %s | returned after %.2fs | updates=%d",
            mode,
            poll_elapsed,
            len(updates),
        )

        if updates:
            logger.info(
                "Polling | processing %d updates",
                len(updates),
            )

            for update in updates:
                logger.info(
                    "Polling | update_id=%s",
                    update.update_id,
                )

                offset = (
                    update.update_id
                    + 1
                )

                await application.process_update(
                    update
                )

                logger.info(
                    "Polling | update_id=%s processed",
                    update.update_id,
                )

        if (
            poll_mode == "active"
            and last_input_time is not None
            and (
                time.monotonic()
                - last_input_time
                >= ACTIVE_IDLE_TIMEOUT
            )
        ):
            poll_mode = "idle"
            last_input_time = None

            logger.info(
                "Polling -> IDLE | active timeout"
            )


async def post_init(
    application,
):
    global telegram_loop
    global telegram_queue
    global output_event
    global current_mode
    global capture_output

    logger.info(
        "post_init | starting"
    )

    telegram_loop = (
        asyncio.get_running_loop()
    )

    logger.info(
        "post_init | Telegram loop initialized"
    )

    telegram_queue = asyncio.Queue()

    logger.info(
        "post_init | Telegram queue initialized"
    )

    output_event = asyncio.Event()

    logger.info(
        "post_init | Output event initialized"
    )

    logger.info(
        "Telegram initialized"
    )

    asyncio.create_task(
        telegram_sender(
            application.bot
        )
    )

    logger.info(
        "Telegram sender task created"
    )

    asyncio.create_task(
        output_sender()
    )

    logger.info(
        "Output sender task created"
    )

    await enqueue_telegram(
        "/session"
    )

    initial_mode = (
        get_terminal_mode()
    )

    if initial_mode is not None:
        current_mode = initial_mode

        logger.info(
            "Initial mode -> %s",
            initial_mode,
        )

        await enqueue_telegram(
            f"/mode {initial_mode}"
        )

    clear_output()

    capture_output = True

    logger.info(
        "post_init | capture_output=True"
    )

    send_to_terminal(
        b"\r"
    )

    await enqueue_telegram(
        "/ready"
    )

    logger.info(
        "post_init | completed"
    )


async def run():
    logger.info(
        "Starting Telegram Remote Server"
    )

    start_terminal()

    request = HTTPXRequest(
        proxy=None,
        httpx_kwargs={
            "trust_env": False,
        }
    )

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .request(request)
        .get_updates_request(request)
        .build()
    )

    logger.info(
        "Telegram application created"
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT,
            handle_message,
        )
    )

    logger.info(
        "Message handler registered"
    )

    await application.initialize()

    logger.info(
        "Telegram application initialized"
    )

    await application.bot.delete_webhook(
        drop_pending_updates=False
    )

    logger.info(
        "Webhook deleted"
    )

    await application.start()

    logger.info(
        "Telegram application started"
    )

    await post_init(
        application
    )

    logger.info(
        "Server started"
    )

    try:
        await adaptive_polling(
            application
        )
    finally:
        logger.info(
            "Server shutting down"
        )

        await application.stop()

        await application.shutdown()

        logger.info(
            "Server stopped"
        )


def main():
    logger.info(
        "main | starting"
    )

    asyncio.run(
        run()
    )


if __name__ == "__main__":
    main()