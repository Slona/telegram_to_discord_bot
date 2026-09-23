#!/usr/bin/env python3

import asyncio
import logging
import os
import signal
import sys

import aiohttp
import telethon
from telethon import TelegramClient, events
from dotenv import load_dotenv

load_dotenv()

from relay import relay
from targets import load_targets

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logging.getLogger("telethon").setLevel(logging.WARNING)
logger = logging.getLogger("tg_relay")

appid = os.environ.get("APPID")
apihash = os.environ.get("APIHASH")
apiname = os.environ.get("APINAME")
dlloc = os.environ.get("DLLOC")
input_channels_entities = os.environ.get("INPUT_CHANNELS")

REQUIRED_ENV = {
    "APPID": appid,
    "APIHASH": apihash,
    "APINAME": apiname,
    "DLLOC": dlloc,
}

def validate_env():
    missing = [name for name, value in REQUIRED_ENV.items() if not value]
    if missing:
        logger.error("Missing required environment variables: %s", ", ".join(missing))
        sys.exit(1)
    if not os.path.isdir(dlloc):
        logger.error("DLLOC directory does not exist: %s", dlloc)
        sys.exit(1)

if input_channels_entities is not None:
    input_channels_entities = list(map(int, input_channels_entities.split(',')))

def is_channel_post(event):
    # Ignore direct messages from users and bots.
    return event.chat is not None and not isinstance(event.chat, telethon.tl.types.User)

async def main():
    validate_env()

    targets = load_targets()
    if not targets:
        logger.error("No relay targets configured (set WEBHOOK and/or other target variables)")
        sys.exit(1)

    async with aiohttp.ClientSession() as session:
        for target in targets:
            await target.setup(session)

        client = TelegramClient(apiname, appid, apihash, catch_up=True)
        await client.connect()
        if not await client.is_user_authorized():
            if sys.stdin.isatty():
                # First run (or an expired session) in an interactive terminal:
                # let Telethon prompt for phone/code as usual.
                await client.start()
            else:
                logger.error(
                    "Telegram session is not authorized and there is no interactive "
                    "terminal to sign in from. Run `python3 main.py` by hand once to "
                    "recreate %s.session, then restart the service.", apiname
                )
                await client.disconnect()
                sys.exit(1)

        logger.info("Started")
        logger.info("Input channels: %s", input_channels_entities)
        logger.info("Targets: %s", ", ".join(t.name for t in targets))

        @client.on(events.NewMessage(chats=input_channels_entities))
        async def on_message(event):
            if not is_channel_post(event):
                return
            if event.message.grouped_id is not None:
                return  # Part of an album, handled by on_album as one post
            await relay([event.message], event.chat, targets, dlloc)

        @client.on(events.Album(chats=input_channels_entities))
        async def on_album(event):
            if not is_channel_post(event):
                return
            await relay(event.messages, event.chat, targets, dlloc)

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, lambda: asyncio.ensure_future(client.disconnect()))
            except NotImplementedError:
                pass  # add_signal_handler is not available on Windows

        await client.run_until_disconnected()
        logger.info("Disconnected, shutting down")

if __name__ == "__main__":
    asyncio.run(main())
