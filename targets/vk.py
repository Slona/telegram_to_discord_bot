"""Relay target for a VK community wall.

Works with a *community* access key, which limits what is possible (checked
with tools/vk_check.py): text and photos are supported, video is not (video.save
is user-only), so video and anything else VK won't take is replaced by a link
to the original Telegram post.

Env:
  VK_TOKEN       community access key (rights: wall, photos, messages)
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

import aiohttp

from . import Post, Target

logger = logging.getLogger("tg_relay.vk")

API_URL = "https://api.vk.com/method/"
TEXT_LIMIT = 16000            # VK's hard limit is 16384 characters
PHOTOS_PER_POST = 10
PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".gif"}
PHOTO_MAX_BYTES = 50 * 1024 * 1024
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

    async def upload_photo(self, path):
        """Upload one image via the messages route (the only photo route a
        community key may use) and return its wall attachment string."""
        server = await self.call("photos.getMessagesUploadServer")
        form = aiohttp.FormData()
        with open(path, "rb") as fh:
            form.add_field("photo", fh, filename=os.path.basename(path))
            async with self.session.post(server["upload_url"], data=form,
                                         ssl=self.ssl_context) as resp:
                uploaded = await resp.json(content_type=None)
        if not uploaded.get("photo"):
            raise VkError(None, f"photo upload rejected: {uploaded}")
        saved = await self.call("photos.saveMessagesPhoto", photo=uploaded["photo"],
                                server=uploaded["server"], hash=uploaded["hash"])
        photo = saved[0]
        key = f"_{photo['access_key']}" if photo.get("access_key") else ""
        return f"photo{photo['owner_id']}_{photo['id']}{key}"

    async def wall_post(self, message, attachments):
        params = dict(owner_id=-self.group_id, from_group=1, message=message,
                      guid=uuid.uuid4().hex)
        if attachments:
            params["attachments"] = ",".join(attachments)
        try:
            return await self.call("wall.post", **params)
        except VkError as e:
            if e.code != ERR_HYPERLINKS_FORBIDDEN or not message:
                raise
            # The community blocks links in posts: publish without them.
            logger.warning("VK forbids hyperlinks here, retrying without URLs")
            params["message"] = URL_RE.sub("", message).strip()
            params["guid"] = uuid.uuid4().hex
            if not params["message"] and not attachments:
                raise
            return await self.call("wall.post", **params)

    async def send(self, post: Post):
        text = html_to_plain(post.html).strip()

        attachments, skipped = [], 0
        for path in post.media:
            ext = os.path.splitext(path)[1].lower()
            if ext not in PHOTO_EXTS or os.path.getsize(path) > PHOTO_MAX_BYTES:
                skipped += 1  # video, audio, stickers, huge files...
                continue
            if len(attachments) >= PHOTOS_PER_POST:
                skipped += 1
                continue
            try:
                attachments.append(await self.upload_photo(path))
            except Exception:
                logger.exception("Photo upload of %s failed, skipping", path)
                skipped += 1

        if len(text) > TEXT_LIMIT:
            text = text[:TEXT_LIMIT].rstrip() + "…"
            skipped += 1  # point readers at the full text too
        if skipped:
            text = f"{text}\n\n{post.link}".strip()

        if not text and not attachments:
            return
        logger.info("Posting to the VK wall: %s photo(s)", len(attachments))
        await self.wall_post(text, attachments)
