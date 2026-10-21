"""Relay target for a Threads profile (Meta's Threads API).

Threads can't take uploads: pictures and videos are fetched by Threads itself
from a public URL. So media is first copied over SFTP to a small public folder
(e.g. https://slona.me/tmp-threads/, served by nginx from a chrooted SFTP-only
account), referenced by URL, and deleted again after the post is published.

Env:
  THREADS_APP_ID       Meta app id (enables the target); needed by tools/threads_auth.py
  THREADS_TOKEN_FILE   optional, default threads_token.json (tools/threads_auth.py)
  THREADS_MEDIA_SFTP   user@host of the upload account, e.g. threadsbot@slona.me
  THREADS_MEDIA_KEY    private key for that account
  THREADS_MEDIA_URL    public base URL of the upload folder, e.g. https://slona.me/tmp-threads/
  THREADS_KNOWN_HOSTS  optional known_hosts file for sftp (default threads_known_hosts)
  THREADS_GRAPH_URL    optional, default https://graph.threads.com
Without the three THREADS_MEDIA_* settings the post goes out as text with a link
to the original instead of its pictures and videos.
"""

import asyncio
import json
import logging
import os
import secrets
import struct
import time
import unicodedata

import requests

from . import Post, Target, keepalive_loop
from .vk import html_to_plain

logger = logging.getLogger("tg_relay.threads")

GRAPH_URL = "https://graph.threads.com"
API_VERSION = "v1.0"
HTTP_TIMEOUT = 60
TEXT_LIMIT = 500
CAROUSEL_MAX = 20

IMAGE_EXTS = {".jpg", ".jpeg", ".png"}
IMAGE_MAX_BYTES = 8 * 1024 * 1024
IMAGE_MAX_RATIO = 10
VIDEO_EXTS = {".mp4", ".mov"}
VIDEO_MAX_BYTES = 1024 ** 3

POLL_INTERVAL = 3
IMAGE_WAIT = 90          # seconds to wait for Threads to fetch a picture
VIDEO_WAIT = 420         # ... and to process a video

REFRESH_BEFORE = 5 * 24 * 3600     # renew a token that expires within this
KEEPALIVE_AFTER = 7 * 24 * 3600    # long-lived tokens (60 days): renew weekly


class ThreadsError(Exception):
    def __init__(self, status, body):
        super().__init__(f"Threads API error {status}: {body}")
        self.status = status
        self.body = body


class MediaRejected(Exception):
    """Threads can't use one of the media items (carries its index)."""

    def __init__(self, index, reason):
        super().__init__(f"media item {index}: {reason}")
        self.index = index


# --- text -------------------------------------------------------------------

def weight(ch):
    """Threads counts emoji by their UTF-8 length; letters count as one. Anything
    above U+2000 that isn't a letter is counted by bytes (slightly pessimistic)."""
    if ord(ch) < 0x2000 or unicodedata.category(ch)[0] == "L":
        return 1
    return len(ch.encode("utf-8"))


def text_weight(text):
    return sum(weight(c) for c in text)


def fit(text, limit):
    """`text` cut to at most `limit` weight, ending in an ellipsis when cut."""
    if text_weight(text) <= limit:
        return text
    ellipsis = weight("…")      # 3: it is not a letter and sits above U+2000
    if limit < ellipsis:
        return ""
    out, used = [], 0
    for ch in text:
        w = weight(ch)
        if used + w > limit - ellipsis:
            break
        out.append(ch)
        used += w
    return "".join(out).rstrip() + "…"


def compose(post, skipped):
    """The Threads text: post text, then the buttons as 'label: url' lines, and a
    note with the original's link when something didn't fit or didn't go along."""
    text = html_to_plain(post.html).strip()
    buttons = "\n".join(f"{label}: {url}" for label, url in post.flat_buttons())
    note = ""
    full = "\n\n".join(p for p in (text, buttons) if p)
    if skipped:
        note = f"Медиа — в оригинале: {post.link}"
    elif text_weight(full) > TEXT_LIMIT:
        note = f"Полностью — в оригинале: {post.link}"

    tail = "\n\n".join(p for p in (buttons, note) if p)
    if text_weight(tail) > TEXT_LIMIT - 20:     # no room for the buttons themselves
        tail = note
    room = TEXT_LIMIT - text_weight(tail) - (2 if text and tail else 0)
    head = fit(text, max(room, 0)) if text else ""
    return "\n\n".join(p for p in (head, tail) if p)


# --- media ------------------------------------------------------------------

def image_size(path):
    """(width, height) of a JPEG or PNG, or None when it can't be read."""
    try:
        with open(path, "rb") as f:
            head = f.read(24)
            if head[:8] == b"\x89PNG\r\n\x1a\n":
                return struct.unpack(">II", head[16:24])
            if head[:2] != b"\xff\xd8":
                return None
            f.seek(2)
            while True:
                marker = f.read(2)
                if len(marker) < 2 or marker[0] != 0xFF:
                    return None
                while marker[1] == 0xFF:                  # fill bytes
                    marker = marker[1:] + f.read(1)
                code = marker[1]
                if code == 0x01 or 0xD0 <= code <= 0xD8:  # markers without a length
                    continue
                length = struct.unpack(">H", f.read(2))[0]
                if 0xC0 <= code <= 0xCF and code not in (0xC4, 0xC8, 0xCC):
                    _, height, width = struct.unpack(">BHH", f.read(5))
                    return width, height
                f.seek(length - 2, 1)
    except (OSError, struct.error):
        return None


def classify(path):
    """('image'|'video', None) if Threads can take the file, else (None, reason)."""
    ext = os.path.splitext(path)[1].lower()
    size = os.path.getsize(path)
    if ext in IMAGE_EXTS:
        if size > IMAGE_MAX_BYTES:
            return None, "over 8 MB"
        dims = image_size(path)
        if dims and max(dims) / max(min(dims), 1) > IMAGE_MAX_RATIO:
            return None, "aspect ratio beyond 10:1"
        return "image", None
    if ext in VIDEO_EXTS:
        if size > VIDEO_MAX_BYTES:
            return None, "over 1 GB"
        return "video", None
    return None, f"unsupported type {ext or '(none)'}"


class SftpUploader:
    """Copies files to the public folder with the sftp command-line client."""

    def __init__(self, target, key, base_url, known_hosts):
        self.target = target
        self.key = key
        self.base_url = base_url.rstrip("/") + "/"
        self.known_hosts = known_hosts

    async def _sftp(self, commands):
        proc = await asyncio.create_subprocess_exec(
            "sftp", "-i", self.key, "-b", "-",
            "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", f"UserKnownHostsFile={self.known_hosts}", "-o", "ConnectTimeout=20",
            self.target,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, err = await asyncio.wait_for(
                proc.communicate("\n".join(commands).encode() + b"\n"), timeout=900)
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError("sftp timed out")
        if proc.returncode != 0:
            raise RuntimeError(f"sftp failed ({proc.returncode}): {err.decode(errors='replace').strip()}")

    async def put(self, paths):
        """Upload files under unguessable names; returns [(name, public url)]."""
        uploaded = []
        for path in paths:
            ext = os.path.splitext(path)[1].lower()
            name = f"{secrets.token_urlsafe(18)}{ext}"
            uploaded.append((path, name))
        await self._sftp([f'put "{path}" "{name}"' for path, name in uploaded])
        return [(name, self.base_url + name) for _, name in uploaded]

    async def remove(self, names):
        for name in names:      # one by one: a missing file must not stop the rest
            try:
                await self._sftp([f'rm "{name}"'])
            except RuntimeError:
                logger.warning("Could not remove %s from the upload folder "
                               "(the server's cleanup job will)", name)


class TokenStore:
    """The long-lived token in a JSON file, rewritten atomically on every renewal."""

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
    def make(access_token, user_id, expires_in):
        return {"access_token": access_token, "user_id": str(user_id),
                "expires_at": time.time() + int(expires_in), "refreshed_at": time.time()}


# --- the target ---------------------------------------------------------------

class ThreadsTarget(Target):
    name = "threads"

    def __init__(self, token_file, graph_url=GRAPH_URL, uploader=None):
        self.store = TokenStore(token_file)
        self.graph_url = graph_url.rstrip("/")
        self.uploader = uploader
        self.tokens = None
        self._refresh_lock = asyncio.Lock()

    @classmethod
    def from_env(cls):
        if not os.environ.get("THREADS_APP_ID"):
            return None
        token_file = os.environ.get("THREADS_TOKEN_FILE", "threads_token.json")
        uploader = None
        host = os.environ.get("THREADS_MEDIA_SFTP")
        key = os.environ.get("THREADS_MEDIA_KEY")
        url = os.environ.get("THREADS_MEDIA_URL")
        if host and key and url:
            known = os.environ.get(
                "THREADS_KNOWN_HOSTS",
                os.path.join(os.path.dirname(os.path.abspath(token_file)), "threads_known_hosts"))
            uploader = SftpUploader(host, key, url, known)
        elif host or key or url:
            logger.warning("THREADS_MEDIA_SFTP, THREADS_MEDIA_KEY and THREADS_MEDIA_URL "
                           "must all be set: Threads gets text only")
        return cls(token_file, os.environ.get("THREADS_GRAPH_URL", GRAPH_URL), uploader)

    async def setup(self, session):
        try:
            self.tokens = self.store.load()
        except (OSError, ValueError):
            logger.error("No Threads token in %s: run tools/threads_auth.py first",
                         self.store.path)
            raise
        if self.uploader is None:
            logger.info("No media upload folder configured: Threads gets text and a link")
        self._keepalive = asyncio.create_task(keepalive_loop("Threads", self._keepalive_step))

    # --- API ----------------------------------------------------------------

    def _call(self, method, path, token, **params):
        params["access_token"] = token
        url = f"{self.graph_url}/{API_VERSION}/{path}" if not path.startswith("/") \
            else f"{self.graph_url}{path}"
        if method == "GET":
            resp = requests.get(url, params=params, timeout=HTTP_TIMEOUT)
        else:
            resp = requests.post(url, data=params, timeout=HTTP_TIMEOUT)
        if resp.status_code != 200:
            raise ThreadsError(resp.status_code, resp.text)
        return resp.json()

    async def api(self, method, path, token, **params):
        return await asyncio.to_thread(self._call, method, path, token, **params)

    # --- token --------------------------------------------------------------

    @staticmethod
    def _due(tokens, force):
        if force:
            return time.time() - tokens.get("refreshed_at", 0) >= KEEPALIVE_AFTER
        return tokens["expires_at"] - time.time() < REFRESH_BEFORE

    async def access_token(self):
        async with self._refresh_lock:
            if self._due(self.tokens, force=False):
                await self._refresh()
            return self.tokens["access_token"]

    async def _keepalive_step(self):
        async with self._refresh_lock:
            if self._due(self.tokens, force=True):
                await self._refresh(force=True)

    async def _refresh(self, force=False):
        try:  # another process (a re-run of tools/threads_auth.py) may have renewed it
            on_disk = self.store.load()
        except (OSError, ValueError):
            on_disk = None
        if on_disk and on_disk.get("refreshed_at", 0) > self.tokens.get("refreshed_at", 0):
            logger.info("Threads token was updated on disk, using that one")
            self.tokens = on_disk
            if not self._due(on_disk, force):
                return
        logger.info("Renewing the Threads token")
        body = await asyncio.to_thread(
            lambda: self._refresh_request(self.tokens["access_token"]))
        self.tokens = TokenStore.make(body["access_token"], self.tokens["user_id"],
                                      body.get("expires_in", 5183944))
        self.store.save(self.tokens)

    def _refresh_request(self, token):
        resp = requests.get(f"{self.graph_url}/refresh_access_token",
                            params={"grant_type": "th_refresh_token", "access_token": token},
                            timeout=HTTP_TIMEOUT)
        if resp.status_code != 200:
            raise ThreadsError(resp.status_code, resp.text + " (rerun tools/threads_auth.py?)")
        return resp.json()

    # --- publishing -----------------------------------------------------------

    async def _create(self, token, index=None, **params):
        try:
            result = await self.api("POST", f"{self.tokens['user_id']}/threads", token, **params)
        except ThreadsError as e:
            if index is not None:
                raise MediaRejected(index, str(e))
            raise
        return result["id"]

    async def _wait(self, token, container, kind, index):
        deadline = time.monotonic() + (VIDEO_WAIT if kind == "video" else IMAGE_WAIT)
        while True:
            info = await self.api("GET", container, token, fields="status,error_message")
            status = info.get("status")
            if status in ("FINISHED", "PUBLISHED"):
                return
            if status in ("ERROR", "EXPIRED"):
                raise MediaRejected(index, f"{status}: {info.get('error_message', '')}")
            if time.monotonic() > deadline:
                raise MediaRejected(index, f"still {status} after the waiting time")
            await asyncio.sleep(POLL_INTERVAL)

    async def _publish(self, token, text, hosted):
        """Create the container(s), wait for the media, publish; returns the post id."""
        def item_params(kind, url):
            return {"media_type": kind.upper(), f"{kind}_url": url}

        if not hosted:
            container = await self._create(token, media_type="TEXT", text=text)
        elif len(hosted) == 1:
            kind, url = hosted[0]
            container = await self._create(token, index=0, text=text, **item_params(kind, url))
            await self._wait(token, container, kind, 0)
        else:
            children = [await self._create(token, index=i, is_carousel_item="true",
                                           **item_params(kind, url))
                        for i, (kind, url) in enumerate(hosted)]
            for i, ((kind, _), child) in enumerate(zip(hosted, children)):
                await self._wait(token, child, kind, i)
            container = await self._create(token, media_type="CAROUSEL",
                                           children=",".join(children), text=text)
        result = await self.api("POST", f"{self.tokens['user_id']}/threads_publish", token,
                                creation_id=container)
        return result["id"]

    async def send(self, post: Post):
        token = await self.access_token()
        items, skipped = [], 0
        for path in post.media:
            kind, reason = classify(path)
            if kind is None or len(items) >= CAROUSEL_MAX or self.uploader is None:
                logger.warning("Threads skips %s: %s", os.path.basename(path),
                               reason or "no upload folder / too many items")
                skipped += 1
            else:
                items.append((kind, path))

        hosted, names = [], []
        try:
            if items:
                try:
                    uploaded = await self.uploader.put([path for _, path in items])
                    hosted = [(kind, url) for (kind, _), (_, url) in zip(items, uploaded)]
                    names = [name for name, _ in uploaded]
                except Exception:
                    logger.exception("Uploading media for Threads failed")
                    skipped += len(items)
            while True:
                text = compose(post, skipped)
                try:
                    post_id = await self._publish(token, text, hosted)
                    break
                except MediaRejected as e:
                    logger.warning("Threads can't use a media item (%s), leaving it out", e)
                    del hosted[e.index]
                    skipped += 1
                except ThreadsError as e:
                    if not hosted:
                        raise
                    logger.warning("Threads refused the media post (%s), posting text and a link", e)
                    skipped += len(hosted)
                    hosted = []
        finally:
            if names:
                await self.uploader.remove(names)

        link = ""
        try:
            info = await self.api("GET", post_id, token, fields="permalink")
            link = f", {info['permalink']}" if info.get("permalink") else ""
        except Exception:  # noqa: BLE001 - the permalink is only for the log
            pass
        logger.info("Threads post published: id %s, %s media%s", post_id, len(hosted), link)
