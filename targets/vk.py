"""Relay target for a VK community wall (text only).

A community access key can only publish text (checked step by step with
tools/vk_check.py): photo upload routes for the wall/albums and video.save are
user-only; photos uploaded through the messages route are accepted by wall.post
but silently dropped, and as a link-snippet picture they are rejected; documents
show as a bare file name. So media is replaced by a link to the original post.
Real photos need a *user* token with the photos/wall rights, which VK grants only
on request to its developer support.

Env:
  VK_TOKEN       community access key (right: wall)
  VK_GROUP_ID    numeric community id, no minus sign
  VK_API_VERSION optional, defaults to 5.199
  VK_CA_FILE     optional PEM bundle to trust instead of the system store
"""

import asyncio
import logging
import os
import re
import ssl
import uuid
from html.parser import HTMLParser

from . import Post, Target

logger = logging.getLogger("tg_relay.vk")

API_URL = "https://api.vk.com/method/"
TEXT_LIMIT = 16000            # VK's hard limit is 16384 characters
FLOOD_RETRIES = 3

ERR_TOO_MANY_REQUESTS = 6
ERR_HYPERLINKS_FORBIDDEN = 222

URL_RE = re.compile(r"\s*\(?https?://[^\s)]*\)?")


class VkError(Exception):
    def __init__(self, code, msg):
        super().__init__(f"VK API error {code}: {msg}")
        self.code = code


class _PlainText(HTMLParser):
    """Telegram HTML -> plain text, keeping link targets as 'text (url)'."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self._href = None
        self._anchor = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._href = dict(attrs).get("href")
            self._anchor = []
        elif tag == "br":
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)
        if self._href is not None:
            self._anchor.append(data)

    def handle_endtag(self, tag):
        if tag != "a" or self._href is None:
            return
        text = "".join(self._anchor).strip()
        href = self._href
        self._href = None
        # Skip bare links (text is the URL) and in-app links like tg://user?id=…
        if href.startswith(("http://", "https://")) and href.rstrip("/") != text.rstrip("/"):
            self.parts.append(f" ({href})")


def html_to_plain(text):
    parser = _PlainText()
    parser.feed(text)
    parser.close()
    return "".join(parser.parts)


class VkTarget(Target):
    name = "vk"

    def __init__(self, token, group_id, version, ca_file):
        self.token = token
        self.group_id = group_id
        self.version = version
        self.session = None
        self.ssl_context = ssl.create_default_context(cafile=ca_file) if ca_file else None

    @classmethod
    def from_env(cls):
        token = os.environ.get("VK_TOKEN")
        group_id = os.environ.get("VK_GROUP_ID")
        if not token or not group_id:
            return None
        return cls(token, int(group_id.lstrip("-")),
                   os.environ.get("VK_API_VERSION", "5.199"),
                   os.environ.get("VK_CA_FILE"))

    async def setup(self, session):
        self.session = session

    async def call(self, method, **params):
        params["v"] = self.version
        delay = 1.0
        for attempt in range(FLOOD_RETRIES):
            async with self.session.post(
                API_URL + method, data=params, ssl=self.ssl_context,
                headers={"Authorization": f"Bearer {self.token}"},
            ) as resp:
                data = await resp.json(content_type=None)
            error = data.get("error")
            if error is None:
                return data["response"]
            if error.get("error_code") == ERR_TOO_MANY_REQUESTS and attempt < FLOOD_RETRIES - 1:
                await asyncio.sleep(delay)
                delay *= 2
                continue
            raise VkError(error.get("error_code"), error.get("error_msg"))

    async def wall_post(self, message):
        params = dict(owner_id=-self.group_id, from_group=1, message=message,
                      guid=uuid.uuid4().hex)
        try:
            return await self.call("wall.post", **params)
        except VkError as e:
            stripped = URL_RE.sub("", message).strip()
            if e.code != ERR_HYPERLINKS_FORBIDDEN or not stripped:
                raise
            # The community blocks links in posts: publish without them.
            logger.warning("VK forbids hyperlinks here, retrying without URLs")
            params.update(message=stripped, guid=uuid.uuid4().hex)
            return await self.call("wall.post", **params)

    async def send(self, post: Post):
        text = html_to_plain(post.html).strip()
        truncated = len(text) > TEXT_LIMIT
        if truncated:
            text = text[:TEXT_LIMIT].rstrip() + "…"

        if post.media and truncated:
            note = "Полный текст и медиа — в оригинале"
        elif post.media:
            note = "Медиа — в оригинале"
        elif truncated:
            note = "Полный текст — в оригинале"
        else:
            note = None
        if note:
            text = f"{text}\n\n{note}: {post.link}".strip()

        if not text:
            return
        logger.info("Posting to the VK wall (%s media in the original)", len(post.media))
        await self.wall_post(text)
