import asyncio
import logging
import os
import re
import uuid

import telethon
from telethon.extensions import html as tg_html

from targets import Post

logger = logging.getLogger("tg_relay")


def is_relayable_media(message):
    # Link previews come through as media but are just the preview of a URL
    # that is already in the message text, so they are not worth mirroring.
    return message.media is not None and not isinstance(
        message.media, telethon.tl.types.MessageMediaWebPage
    )


def telegram_link(chat, message):
    # Public channels have a link anyone can open; private ones only work for members.
    username = getattr(chat, "username", None)
    if username:
        return f"https://t.me/{username}/{message.id}"
    return f"https://t.me/c/{chat.id}/{message.id}"


def safe_name(path):
    """Rename a downloaded file so its name is plain ASCII without spaces or
    brackets: VK and Tumblr choke on names like 'photo (1).jpg' in uploads."""
    directory, name = os.path.split(path)
    stem, ext = os.path.splitext(name)
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("_") or "file"
    new_path = os.path.join(directory, safe + ext.lower())
    if new_path == path:
        return path
    if os.path.exists(new_path):
        new_path = os.path.join(directory, f"{safe}_{uuid.uuid4().hex[:6]}{ext.lower()}")
    os.rename(path, new_path)
    return new_path


async def download_media(messages, dlloc):
    paths = []
    for message in messages:
        path = await message.download_media(dlloc)
        if path is None:  # Telethon has nothing downloadable for this media type
            logger.warning("Skipping media of message %s: nothing to download", message.id)
            continue
        paths.append(safe_name(path))
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
        text_message = next((m for m in messages if m.message), None)
        text = text_message.text if text_message else ""
        html = (tg_html.unparse(text_message.raw_text, text_message.entities or [])
                if text_message else "")
        plain = text_message.raw_text if text_message else ""
        media_messages = [m for m in messages if is_relayable_media(m)]
        paths = await download_media(media_messages, dlloc)
        if not text and not paths:
            return

        post = Post(source=chat.title, text=text, html=html, plain=plain,
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
