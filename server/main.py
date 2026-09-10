import asyncio
import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application,
    ContextTypes,
    MessageHandler,
    filters,
)


load_dotenv()

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = int(os.environ["TELEGRAM_CHAT_ID"])
TERMINAL_TITLE = os.getenv(
    "TERMINAL_TITLE",
    "telegram-remote",
)

LOG_DIR = Path(tempfile.gettempdir()) / "telegram_remote"
LOG_DIR.mkdir(exist_ok=True)

MAX_MESSAGE_LENGTH = 4000

running = False


def open_terminal():
    subprocess.Popen(
        [
            "gnome-terminal",
            "--title",
            TERMINAL_TITLE,
        ]
    )

    time.sleep(1)


def find_terminal():
    result = subprocess.run(
        [
            "xdotool",
            "search",
            "--name",
            TERMINAL_TITLE,
        ],
        capture_output=True,
        text=True,
    )

    windows = result.stdout.strip().splitlines()

    if not windows:
        raise RuntimeError(
            f"Terminal '{TERMINAL_TITLE}' not found"
        )

    return windows[0]


def run_command(command, log_file, end_file):
    window = find_terminal()

    subprocess.run(
        [
            "xdotool",
            "windowactivate",
            "--sync",
            window,
        ],
        check=True,
    )

    shell_command = (
        f"{command} > '{log_file}' 2>&1; "
        f"echo $? > '{end_file}'"
    )

    subprocess.run(
        [
            "xdotool",
            "type",
            "--clearmodifiers",
            shell_command,
        ],
        check=True,
    )

    subprocess.run(
        [
            "xdotool",
            "key",
            "Return",
        ],
        check=True,
    )


def read_log(log_file):
    if not log_file.exists():
        return "Running..."

    log = log_file.read_text(
        errors="replace"
    )

    return log[-MAX_MESSAGE_LENGTH:] or "Running..."


async def send_message(bot, text):
    await bot.send_message(
        chat_id=CHAT_ID,
        text=text,
    )


def execute_command(bot, loop, message_id, command):
    global running

    log_file = LOG_DIR / f"{message_id}.log"
    end_file = LOG_DIR / f"{message_id}.exit"

    try:
        run_command(
            command,
            log_file,
            end_file,
        )

        while not end_file.exists():
            time.sleep(1)

            log = read_log(log_file)

            asyncio.run_coroutine_threadsafe(
                send_message(bot, log),
                loop,
            ).result()

        log = read_log(log_file)

        if log == "Running...":
            log = "Command finished."

        asyncio.run_coroutine_threadsafe(
            send_message(
                bot,
                log + "\n/end",
            ),
            loop,
        ).result()

    except Exception as e:
        asyncio.run_coroutine_threadsafe(
            send_message(
                bot,
                f"ERROR: {e}",
            ),
            loop,
        ).result()

    finally:
        log_file.unlink(missing_ok=True)
        end_file.unlink(missing_ok=True)
        running = False


async def handle_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    global running

    message = update.message

    if not message:
        return

    if message.chat_id != CHAT_ID:
        return

    text = message.text or ""

    if not text.startswith("/command"):
        return

    command = text[len("/command"):].strip()

    if not command:
        return

    if running:
        await context.bot.send_message(
            chat_id=CHAT_ID,
            text="A command is already running.",
        )
        return

    running = True

    loop = asyncio.get_running_loop()

    threading.Thread(
        target=execute_command,
        args=(
            context.bot,
            loop,
            message.message_id,
            command,
        ),
        daemon=True,
    ).start()


def main():
    print("==> Opening terminal...")
    open_terminal()

    print("==> Starting Telegram bot...")

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT,
            handle_message,
        )
    )

    print("==> Server started.")

    app.run_polling()


if __name__ == "__main__":
    main()