"""Relay target for a MAX channel (https://dev.max.ru/docs-api).

Env:
  MAX_TOKEN    bot token (bot must be an admin of the channel)
  MAX_CHAT_ID  numeric chat_id of the channel (see tools/max_chat_id.py)
  MAX_API_URL  optional, defaults to https://platform-api2.max.ru
  MAX_CA_FILE  optional PEM file with the Russian Ministry of Digital
               Development root CA, if it is not in the system trust store
"""

import asyncio
import dataclasses
import logging
import os
import ssl
import textwrap

import aiohttp

from . import Post, Target

logger = logging.getLogger("tg_relay.max")

DEFAULT_API_URL = "https://platform-api2.max.ru"
TEXT_LIMIT = 4000
ATTACHMENTS_PER_MESSAGE = 12
SEND_INTERVAL = 0.6  # API allows at most 2 messages per second per channel
NOT_READY_RETRIES = 6

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".tif", ".tiff", ".bmp", ".heic"}
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm"}
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".ogg", ".oga", ".flac"}
IMAGE_MAX_BYTES = 50 * 1024 * 1024
VIDEO_MAX_BYTES = 250 * 1024 * 1024
AUDIO_MAX_BYTES = 256 * 1024 * 1024


class MaxError(Exception):
    def __init__(self, status, body):
        super().__init__(f"MAX API error {status}: {body}")
        self.status = status
        self.body = body


def classify(path):
    """Return (upload type, size limit) for a file, or None if MAX can't take it."""
    ext = os.path.splitext(path)[1].lower()
    size = os.path.getsize(path)
    for kind, exts, limit in (
        ("image", IMAGE_EXTS, IMAGE_MAX_BYTES),
        ("video", VIDEO_EXTS, VIDEO_MAX_BYTES),
        ("audio", AUDIO_EXTS, AUDIO_MAX_BYTES),
    ):
        if ext in exts:
            return kind if size <= limit else None
    return None


class MaxTarget(Target):
    name = "max"

    def __init__(self, token, chat_id, api_url, ca_file):
        self.token = token
        self.chat_id = chat_id
        self.api_url = api_url.rstrip("/")
        self.session = None
        self.ssl_context = ssl.create_default_context()
        if ca_file:
            self.ssl_context.load_verify_locations(cafile=ca_file)

    @classmethod
    def from_env(cls):
        token = os.environ.get("MAX_TOKEN")
        chat_id = os.environ.get("MAX_CHAT_ID")
        if not token or not chat_id:
            return None
        return cls(token, int(chat_id),
                   os.environ.get("MAX_API_URL", DEFAULT_API_URL),
                   os.environ.get("MAX_CA_FILE"))

    async def setup(self, session):
        self.session = session

    @property
    def auth(self):
        return {"Authorization": self.token}

    async def _json(self, method, url, **kwargs):
        async with self.session.request(
            method, url, ssl=self.ssl_context, headers=self.auth, **kwargs
        ) as resp:
            body = await resp.text()
            if resp.status != 200:
                raise MaxError(resp.status, body)
            return await resp.json(content_type=None) if body else {}

    async def upload(self, path, kind):
        """Upload one file and return the attachment token."""
        info = await self._json("POST", f"{self.api_url}/uploads", params={"type": kind})
        form = aiohttp.FormData()
        with open(path, "rb") as fh:
            form.add_field("data", fh, filename=os.path.basename(path))
            # Per the docs, only the image upload endpoint takes the bot token.
            headers = self.auth if kind == "image" else {}
            async with self.session.post(
                info["url"], data=form, headers=headers, ssl=self.ssl_context
            ) as resp:
                body = await resp.text()
                if resp.status != 200:
                    raise MaxError(resp.status, body)
                if kind == "image":
                    photos = (await resp.json(content_type=None))["photos"]
                    return next(iter(photos.values()))["token"]
        return info["token"]

    async def post_message(self, body):
        """POST /messages, retrying while MAX is still processing an upload."""
        delay = 1.0
        for attempt in range(NOT_READY_RETRIES):
            try:
                return await self._json(
                    "POST", f"{self.api_url}/messages",
                    params={"chat_id": self.chat_id}, json=body,
                )
            except MaxError as e:
                if "attachment.not.ready" not in e.body or attempt == NOT_READY_RETRIES - 1:
                    raise
                logger.info("Attachment not ready yet, retrying in %.0fs", delay)
                await asyncio.sleep(delay)
                delay *= 2

    async def send_text(self, post):
        if len(post.html) <= TEXT_LIMIT:
            await self.post_message({"text": post.html, "format": "html"})
            return
        # Too long for one message: split the unformatted text so no tag is cut in half.
        for chunk in textwrap.wrap(post.plain, TEXT_LIMIT, replace_whitespace=False):
            await self.post_message({"text": chunk})
            await asyncio.sleep(SEND_INTERVAL)

    async def send(self, post: Post):
        attachments, unsupported = [], 0
        for path in post.media:
            kind = classify(path)
            if kind is None:
                logger.warning("MAX can't take %s (type or size), skipping", path)
                unsupported += 1
                continue
            try:
                token = await self.upload(path, kind)
            except Exception:
                logger.exception("Upload of %s failed, skipping", path)
                unsupported += 1
                continue
            attachments.append({"type": kind, "payload": {"token": token}})

        if unsupported and post.link not in post.plain:
            # Something didn't make it; point readers at the full post.
            suffix = f"\n\n{post.link}"
            post = dataclasses.replace(
                post,
                text=(post.text + suffix).strip(),
                html=(post.html + suffix).strip(),
                plain=(post.plain + suffix).strip(),
            )

        if not attachments:
            if post.plain:
                await self.send_text(post)
            return

        # Attachments go in batches; the caption rides on the first batch when it fits.
        caption_fits = len(post.html) <= TEXT_LIMIT
        batches = [attachments[i:i + ATTACHMENTS_PER_MESSAGE]
                   for i in range(0, len(attachments), ATTACHMENTS_PER_MESSAGE)]
        for i, batch in enumerate(batches):
            body = {"attachments": batch}
            if i == 0 and post.html and caption_fits:
                body.update(text=post.html, format="html")
            await self.post_message(body)
            await asyncio.sleep(SEND_INTERVAL)
        if post.plain and not caption_fits:
            await self.send_text(post)
