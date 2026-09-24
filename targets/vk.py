"""Relay target for a VK community wall.

Two credentials, because neither can do the whole job alone (checked step by
step with tools/vk_check.py):

* the community key (VK_TOKEN) publishes the post: wall.post as the community
  works with it, but it can't upload photos or video;
* an admin's user token from VK ID (tools/vk_user_auth.py) uploads photos and
  video to the community; it can't call wall.post itself, since that is only
  allowed to "standalone" apps.

Without the user token (or if it can't be refreshed) the post goes out as text
with a link to the original, so VK keeps working either way.

Env:
  VK_TOKEN            community access key (right: wall)
  VK_GROUP_ID         numeric community id, no minus sign
  VK_APP_ID           VK app id, enables media uploads with the user token
  VK_SERVICE_TOKEN    optional, the app's service key (confidential apps)
  VK_USER_TOKEN_FILE  optional, default vk_user_token.json
  VK_API_VERSION      optional, defaults to 5.199
  VK_CA_FILE          optional PEM bundle to trust instead of the system store
"""

import asyncio
import json
import logging
import os
import re
import secrets
import ssl
import time
import uuid
from html.parser import HTMLParser

import aiohttp

from . import Post, Target

logger = logging.getLogger("tg_relay.vk")

API_URL = "https://api.vk.com/method/"
VK_ID_URL = "https://id.vk.ru"
TEXT_LIMIT = 16000            # VK's hard limit is 16384 characters
ATTACHMENTS_PER_POST = 10
FLOOD_RETRIES = 3
REFRESH_MARGIN = 120          # seconds before expiry to refresh the user token

PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".gif"}
VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v"}
PHOTO_MAX_BYTES = 50 * 1024 * 1024

ERR_ACCESS_DENIED = 15
ERR_TOO_MANY_REQUESTS = 6
ERR_HYPERLINKS_FORBIDDEN = 222

URL_RE = re.compile(r"\s*\(?https?://[^\s)]*\)?")


class VkError(Exception):
    def __init__(self, code, msg):
        super().__init__(f"VK API error {code}: {msg}")
        self.code = code


class UserTokenError(Exception):
    """The admin's user token is missing or can no longer be refreshed."""


class UserTokenStore:
    """VK ID tokens in a JSON file, rewritten atomically on every refresh."""

    def __init__(self, path):
        self.path = path

    def load(self):
        with open(self.path, encoding="utf-8") as fh:
            return json.load(fh)

    def save(self, data):
        tmp = f"{self.path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp, self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    @staticmethod
    def from_response(resp, device_id, previous=None):
        return {
            "access_token": resp["access_token"],
            "refresh_token": resp.get("refresh_token") or (previous or {}).get("refresh_token"),
            "device_id": device_id,
            "user_id": resp.get("user_id"),
            "expires_at": time.time() + int(resp.get("expires_in", 3600)),
        }


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


def classify(path):
    ext = os.path.splitext(path)[1].lower()
    if ext in PHOTO_EXTS and os.path.getsize(path) <= PHOTO_MAX_BYTES:
        return "photo"
    if ext in VIDEO_EXTS:
        return "video"
    return None


class VkTarget(Target):
    name = "vk"

    def __init__(self, token, group_id, version, ca_file,
                 app_id=None, service_token=None, user_token_file=None, vk_id_url=VK_ID_URL):
        self.token = token
        self.group_id = group_id
        self.version = version
        self.app_id = app_id
        self.service_token = service_token
        self.vk_id_url = vk_id_url.rstrip("/")
        self.user_store = UserTokenStore(user_token_file) if app_id and user_token_file else None
        self.user_tokens = None
        self.session = None
        self.ssl_context = ssl.create_default_context(cafile=ca_file) if ca_file else None
        self._refresh_lock = asyncio.Lock()
        self.denied_kinds = set()  # media kinds the user token lacks the right for

    @classmethod
    def from_env(cls):
        token = os.environ.get("VK_TOKEN")
        group_id = os.environ.get("VK_GROUP_ID")
        if not token or not group_id:
            return None
        return cls(token, int(group_id.lstrip("-")),
                   os.environ.get("VK_API_VERSION", "5.199"),
                   os.environ.get("VK_CA_FILE"),
                   app_id=os.environ.get("VK_APP_ID"),
                   service_token=os.environ.get("VK_SERVICE_TOKEN"),
                   user_token_file=os.environ.get("VK_USER_TOKEN_FILE", "vk_user_token.json"),
                   vk_id_url=os.environ.get("VK_ID_URL", VK_ID_URL))

    async def setup(self, session):
        self.session = session
        if self.user_store is None:
            logger.info("VK_APP_ID not set: VK gets text and a link, no media")
            return
        try:
            self.user_tokens = self.user_store.load()
        except (OSError, ValueError):
            logger.warning("No VK user token in %s (run tools/vk_user_auth.py): "
                           "VK gets text and a link, no media", self.user_store.path)

    # --- VK API -----------------------------------------------------------

    async def call(self, method, _token=None, **params):
        params["v"] = self.version
        delay = 1.0
        for attempt in range(FLOOD_RETRIES):
            async with self.session.post(
                API_URL + method, data=params, ssl=self.ssl_context,
                headers={"Authorization": f"Bearer {_token or self.token}"},
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

    # --- admin's user token (VK ID) -----------------------------------------

    async def user_token(self):
        if not self.user_tokens:
            raise UserTokenError("no user token")
        async with self._refresh_lock:
            if self.user_tokens["expires_at"] - time.time() < REFRESH_MARGIN:
                await self._refresh_user_token()
            return self.user_tokens["access_token"]

    async def _refresh_user_token(self):
        tokens = self.user_tokens
        if not tokens.get("refresh_token") or not tokens.get("device_id"):
            self.user_tokens = None
            raise UserTokenError("the token has expired and has no refresh token")
        logger.info("Refreshing the VK user token")
        data = {
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
            "client_id": self.app_id,
            "device_id": tokens["device_id"],
            "state": secrets.token_urlsafe(32),
        }
        if self.service_token:
            data["service_token"] = self.service_token
        async with self.session.post(f"{self.vk_id_url}/oauth2/auth", data=data,
                                     ssl=self.ssl_context) as resp:
            body = await resp.json(content_type=None)
        if "access_token" not in body:
            self.user_tokens = None  # don't hammer VK ID until someone signs in again
            raise UserTokenError(f"refresh failed: {body.get('error')} "
                                 f"{body.get('error_description', '')}")
        self.user_tokens = UserTokenStore.from_response(body, tokens["device_id"], tokens)
        self.user_store.save(self.user_tokens)

    async def _upload(self, url, field, path):
        form = aiohttp.FormData()
        with open(path, "rb") as fh:
            form.add_field(field, fh, filename=os.path.basename(path))
            async with self.session.post(url, data=form, ssl=self.ssl_context) as resp:
                return await resp.json(content_type=None)

    async def upload_photo(self, path):
        token = await self.user_token()
        server = await self.call("photos.getWallUploadServer", _token=token,
                                 group_id=self.group_id)
        uploaded = await self._upload(server["upload_url"], "photo", path)
        if uploaded.get("photo") in (None, "", "[]"):
            raise VkError(None, f"photo upload rejected: {uploaded}")
        saved = await self.call("photos.saveWallPhoto", _token=token, group_id=self.group_id,
                                photo=uploaded["photo"], server=uploaded["server"],
                                hash=uploaded["hash"])
        return f"photo{saved[0]['owner_id']}_{saved[0]['id']}"

    async def upload_video(self, path, title):
        token = await self.user_token()
        info = await self.call("video.save", _token=token, group_id=self.group_id,
                               name=title[:128], wallpost=0)
        uploaded = await self._upload(info["upload_url"], "video_file", path)
        if "error" in uploaded:
            raise VkError(None, f"video upload rejected: {uploaded}")
        owner = uploaded.get("owner_id", info["owner_id"])
        video_id = uploaded.get("video_id", info["video_id"])
        return f"video{owner}_{video_id}"

    # --- posting ----------------------------------------------------------

    async def wall_post(self, message, attachments):
        params = dict(owner_id=-self.group_id, from_group=1, message=message,
                      guid=uuid.uuid4().hex)
        if attachments:
            params["attachments"] = ",".join(attachments)
        try:
            return await self.call("wall.post", **params)
        except VkError as e:
            stripped = URL_RE.sub("", message).strip()
            if e.code != ERR_HYPERLINKS_FORBIDDEN or not (stripped or attachments):
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

        title = (text.splitlines() or [""])[0].strip() or post.source or "Видео"
        attachments, skipped = [], 0
        for path in post.media:
            kind = classify(path)
            if (kind is None or kind in self.denied_kinds or self.user_tokens is None
                    or len(attachments) >= ATTACHMENTS_PER_POST):
                skipped += 1
                continue
            try:
                if kind == "photo":
                    attachments.append(await self.upload_photo(path))
                else:
                    attachments.append(await self.upload_video(path, title))
            except UserTokenError as e:
                logger.error("VK user token unusable (%s): media goes as a link until "
                             "tools/vk_user_auth.py is run again", e)
                skipped += 1
            except VkError as e:
                if e.code != ERR_ACCESS_DENIED:
                    logger.exception("VK upload of %s failed, skipping", path)
                else:
                    # The token lacks the right (e.g. VK ID didn't grant "photos"):
                    # say it once and stop trying until the bot restarts.
                    self.denied_kinds.add(kind)
                    logger.warning("VK user token may not upload %ss (%s): they go as a "
                                   "link to the original from now on", kind, e)
                skipped += 1
            except Exception:
                logger.exception("VK upload of %s failed, skipping", path)
                skipped += 1

        missing = []
        if truncated:
            missing.append("полный текст")
        if skipped:
            missing.append("остальные медиа" if attachments else "медиа")
        if missing:
            note = " и ".join(missing)
            text = f"{text}\n\n{note[0].upper()}{note[1:]} — в оригинале: {post.link}".strip()

        if not text and not attachments:
            return
        logger.info("Posting to the VK wall: %s attachment(s), %s left in the original",
                    len(attachments), skipped)
        await self.wall_post(text, attachments)
