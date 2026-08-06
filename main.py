#!/usr/bin/env python3

import asyncio
import logging
import os
import signal
import sys
import textwrap

import aiohttp
import nextcord
import telethon
from telethon import TelegramClient, events
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("tg_discord")

url = os.environ.get("WEBHOOK")
appid = os.environ.get("APPID")
apihash = os.environ.get("APIHASH")
apiname = os.environ.get("APINAME")
dlloc = os.environ.get("DLLOC")
input_channels_entities = os.environ.get("INPUT_CHANNELS")

REQUIRED_ENV = {
    "WEBHOOK": url,
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

webhook: nextcord.Webhook = None  # set in main() once the aiohttp session exists

async def pic(filem, message, username):  # Send media to webhook
    try:
        logger.info("Sending with media")
        try:  # Try sending to discord
            f = nextcord.File(filem)
            await webhook.send(file=f, username=username)
        except Exception:
            logger.exception("Failed to send media")
        for line in textwrap.wrap(message, 2000, replace_whitespace=False):
            await webhook.send(content=line, username=username)
    except Exception:
        logger.exception("Failed to send message with media")

async def send_to_webhook(message, username):  # Send message to webhook
    logger.info("Sending without media")
    for line in textwrap.wrap(message, 2000, replace_whitespace=False):
        await webhook.send(content=line, username=username)

async def main():
    global webhook
    validate_env()

    async with aiohttp.ClientSession() as session:
        webhook = nextcord.Webhook.from_url(url, session=session)

        client = TelegramClient(apiname, appid, apihash)
        await client.start()
        logger.info("Started")
        logger.info("Input channels: %s", input_channels_entities)

        @client.on(events.NewMessage(chats=input_channels_entities))
        async def handler(event):
            if type(event.chat) == telethon.tl.types.User:
                return  # Ignore Messages from Users or Bots
            msg = event.message.message
            if event.message.media is not None:  # If message has media
                path = await event.message.download_media(dlloc)
                try:
                    await pic(path, msg, event.chat.title)
                finally:
                    os.remove(path)
            else:  # No media text message
                await send_to_webhook(msg, event.chat.title)

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
