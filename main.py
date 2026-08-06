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
logging.getLogger("telethon").setLevel(logging.WARNING)
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

DISCORD_MSG_LIMIT = 2000
DISCORD_ATTACHMENT_LIMIT = 10

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

def is_relayable_media(message):
    # Link previews come through as media but are just the preview of a URL
    # that is already in the message text, so they are not worth mirroring.
    return message.media is not None and not isinstance(
        message.media, telethon.tl.types.MessageMediaWebPage
    )

def telegram_link(chat, message):
    return f"https://t.me/c/{chat.id}/{message.id}"

async def send_text(message, username):
    for line in textwrap.wrap(message, DISCORD_MSG_LIMIT, replace_whitespace=False):
        await webhook.send(content=line, username=username)

async def download_media(messages):
    paths = []
    for message in messages:
        path = await message.download_media(dlloc)
        if path is None:  # Telethon has nothing downloadable for this media type
            logger.warning("Skipping media of message %s: nothing to download", message.id)
            continue
        paths.append(path)
    return paths

def remove_files(paths):
    for path in paths:
        try:
            os.remove(path)
        except OSError:
            logger.exception("Could not remove temp file %s", path)

async def send_media(paths, caption, username, fallback_link):
    files = [nextcord.File(path) for path in paths]
    try:
        logger.info("Sending %s attachment(s)", len(files))
        await webhook.send(files=files, username=username)
    except Exception:
        # Usually the attachment is over Discord's size limit; link the post instead.
        logger.exception("Upload failed, linking to the Telegram post instead")
        caption = f"{caption}\n\n{fallback_link}" if caption else fallback_link
    finally:
        for f in files:
            f.close()
    if caption:
        await send_text(caption, username)

async def relay(messages, chat):
    try:
        caption = next((m.message for m in messages if m.message), "")
        media_messages = [m for m in messages if is_relayable_media(m)]

        if not media_messages:
            if caption:
                logger.info("Sending text only")
                await send_text(caption, chat.title)
            return

        paths = await download_media(media_messages[:DISCORD_ATTACHMENT_LIMIT])
        if not paths:
            if caption:
                await send_text(caption, chat.title)
            return

        try:
            await send_media(paths, caption, chat.title, telegram_link(chat, messages[0]))
        finally:
            remove_files(paths)
    except Exception:
        logger.exception("Failed to relay message from %s", getattr(chat, "title", chat))

def is_channel_post(event):
    # Ignore direct messages from users and bots.
    return event.chat is not None and not isinstance(event.chat, telethon.tl.types.User)

async def main():
    global webhook
    validate_env()

    async with aiohttp.ClientSession() as session:
        webhook = nextcord.Webhook.from_url(url, session=session)

        client = TelegramClient(apiname, appid, apihash, catch_up=True)
        await client.connect()
        if not await client.is_user_authorized():
            logger.error(
                "Telegram session is not authorized. "
                "Sign in interactively once to recreate %s.session", apiname
            )
            await client.disconnect()
            sys.exit(1)

        logger.info("Started")
        logger.info("Input channels: %s", input_channels_entities)

        @client.on(events.NewMessage(chats=input_channels_entities))
        async def on_message(event):
            if not is_channel_post(event):
                return
            if event.message.grouped_id is not None:
                return  # Part of an album, handled by on_album as one Discord message
            await relay([event.message], event.chat)

        @client.on(events.Album(chats=input_channels_entities))
        async def on_album(event):
            if not is_channel_post(event):
                return
            await relay(event.messages, event.chat)

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
