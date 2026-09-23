import asyncio
import logging
import os

import telethon

from targets import Post

logger = logging.getLogger("tg_relay")


def is_relayable_media(message):
    # Link previews come through as media but are just the preview of a URL
    # that is already in the message text, so they are not worth mirroring.
    return message.media is not None and not isinstance(
        message.media, telethon.tl.types.MessageMediaWebPage
    )


def telegram_link(chat, message):
    return f"https://t.me/c/{chat.id}/{message.id}"


async def download_media(messages, dlloc):
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


async def relay(messages, chat, targets, dlloc):
    paths = []
    try:
        text = next((m.text for m in messages if m.message), "")
        media_messages = [m for m in messages if is_relayable_media(m)]
        paths = await download_media(media_messages, dlloc)
        if not text and not paths:
            return

        post = Post(source=chat.title, text=text,
                    link=telegram_link(chat, messages[0]), media=paths)
        results = await asyncio.gather(
            *(t.send(post) for t in targets), return_exceptions=True
        )
        for target, result in zip(targets, results):
            if isinstance(result, Exception):
                logger.error("Target %s failed for post %s", target.name, post.link,
                             exc_info=result)
    except Exception:
        logger.exception("Failed to relay message from %s", getattr(chat, "title", chat))
    finally:
        remove_files(paths)
