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

With ffmpeg/ffprobe installed (apt install ffmpeg) media Threads wouldn't take is
converted first: videos to H.264/AAC MP4 (over 5 minutes can't be posted), other
picture formats to JPEG/PNG, big or too elongated pictures resized or padded,
animated GIFs to video. A text over 500 characters becomes a chain: the post, then
each next piece as a reply to the previous one (needs the threads_manage_replies
permission).
"""

import asyncio
import json
import logging
import os
import re
import secrets
import struct
import time
import unicodedata

import requests

import media
from . import Post, Target, keepalive_loop
from .vk import html_to_plain

logger = logging.getLogger("tg_relay.threads")

GRAPH_URL = "https://graph.threads.com"
API_VERSION = "v1.0"
HTTP_TIMEOUT = 60
TEXT_LIMIT = 500
CAROUSEL_MAX = 20
MAX_PARTS = 10            # a long post: the post and up to 9 replies
REPLY_PAUSE = 2           # seconds between a post and its continuation
MEDIA_RETRY_DELAYS = (5, 15, 30)   # Threads sometimes refuses a fetchable URL: try again

IMAGE_EXTS = {".jpg", ".jpeg", ".png"}
IMAGE_MAX_BYTES = 8 * 1024 * 1024
IMAGE_MAX_RATIO = 10
VIDEO_EXTS = {".mp4", ".mov"}
VIDEO_MAX_BYTES = 1024 ** 3
# What ffmpeg can be asked to turn into something Threads accepts.
VIDEO_LIKE = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".3gp", ".flv", ".wmv", ".ts"}
IMAGE_LIKE = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".heic", ".heif"}

POLL_INTERVAL = 3
IMAGE_WAIT = 90          # seconds to wait for Threads to fetch a picture
VIDEO_WAIT = 420         # ... and to process a video

REFRESH_BEFORE = 5 * 24 * 3600     # renew a token that expires within this
KEEPALIVE_AFTER = 7 * 24 * 3600    # long-lived tokens (60 days): renew weekly


class ThreadsError(Exception):
    def __init__(self, status, body):
        self.status = status
        self.body = body
        super().__init__(self._describe(status, body))

    @staticmethod
    def _describe(status, body):
        """Meta's JSON error as a readable line (its Russian text arrives \\u-escaped)."""
        try:
            err = json.loads(body)["error"]
        except (ValueError, KeyError, TypeError):
            return f"Threads API error {status}: {body}"
        codes = f"{err.get('code')}/{err.get('error_subcode')}" if err.get("error_subcode") \
            else str(err.get("code"))
        detail = err.get("error_user_msg") or err.get("error_user_title") or ""
        return (f"Threads API error {status}: {err.get('message')} (code {codes})"
                + (f": {detail}" if detail else ""))


def is_account_level(error):
    """True for refusals that concern the app/token/profile, not one piece of
    content (e.g. 'API access blocked', code 200): retrying other content or
    other ways of posting can't help and only hammers Meta."""
    try:
        err = json.loads(error.body).get("error", {})
    except (ValueError, AttributeError, TypeError):
        return False
    # Not "any OAuthException": Meta also answers a media it couldn't fetch with that
    # type (code 1, subcode 2207052), which says nothing about the app.
    return err.get("code") in (10, 190, 200)


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


SENTENCE_END = r"(?<=[.!?…])\s+"


def split_text(text, limit=TEXT_LIMIT):
    """Pieces of at most `limit` weight, cut at paragraph, line, sentence or word
    boundaries (a hard cut only for text with no separators at all)."""
    text = text.strip()
    if text_weight(text) <= limit:
        return [text] if text else []
    for sep, pattern in (("\n\n", r"\n\s*\n"), ("\n", r"\n"), (" ", SENTENCE_END),
                         (" ", r"\s+")):
        units = [u for u in re.split(pattern, text) if u.strip()]
        if len(units) > 1:
            out, cur = [], ""
            for unit in units:
                for piece in split_text(unit, limit):
                    candidate = f"{cur}{sep}{piece}" if cur else piece
                    if text_weight(candidate) <= limit:
                        cur = candidate
                    else:
                        out.append(cur)
                        cur = piece
            if cur:
                out.append(cur)
            return out
    out, cur, used = [], [], 0
    for ch in text:
        w = weight(ch)
        if used + w > limit:
            out.append("".join(cur))
            cur, used = [], 0
        cur.append(ch)
        used += w
    if cur:
        out.append("".join(cur))
    return out


def compose_parts(post, skipped):
    """The Threads texts: a long post becomes a chain (the first post, then each
    next piece as a reply to the previous). The buttons ('label: url' lines) and,
    if some media had to be left out, a link to the original go at the very end."""
    text = html_to_plain(post.html).strip()
    buttons = "\n".join(f"{label}: {url}" for label, url in post.flat_buttons())
    note = f"Медиа — в оригинале: {post.link}" if skipped else ""
    tail = "\n\n".join(p for p in (buttons, note) if p)

    parts = split_text(text, TEXT_LIMIT)
    if tail:
        if parts and text_weight(parts[-1] + "\n\n" + tail) <= TEXT_LIMIT:
            parts[-1] += "\n\n" + tail
        elif text_weight(tail) <= TEXT_LIMIT:
            parts.append(tail)
        else:           # too many buttons: keep the note, the links are in the original
            parts.append(note or fit(tail, TEXT_LIMIT))
    if len(parts) > MAX_PARTS:
        closing = f"Полностью — в оригинале: {post.link}"
        parts = parts[:MAX_PARTS]
        parts[-1] = fit(parts[-1], TEXT_LIMIT - text_weight(closing) - 2) + "\n\n" + closing
    return parts or [""]


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
        self._blocked_until = 0.0
        self._blocks = 0          # consecutive account-level refusals

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
            if index is not None and not is_account_level(e):
                raise MediaRejected(index, str(e))
            raise
        return result["id"]

    def _block(self, error):
        """Meta refuses the app itself: pause posting (longer each time) instead of
        trying every post in several ways."""
        self._blocks += 1
        minutes = min(30 * 2 ** (self._blocks - 1), 360)
        self._blocked_until = time.time() + minutes * 60
        logger.error("Threads refuses this app/token (%s). Posts to Threads are paused for "
                     "%s min and are not retried. Check the app dashboard (alerts, "
                     "restrictions, roles) and the profile; tools/threads_check.py shows "
                     "which call fails.", error, minutes)

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

    async def _publish(self, token, text, hosted, reply_to=None):
        """Create the container(s), wait for the media, publish; returns the post id."""
        def item_params(kind, url):
            return {"media_type": kind.upper(), f"{kind}_url": url}

        if not hosted:
            extra = {"reply_to_id": reply_to} if reply_to else {}
            container = await self._create(token, media_type="TEXT", text=text, **extra)
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

    async def _prepare(self, path, temp):
        """(kind, path-to-upload, reason): the file as Threads wants it, converted
        with ffmpeg when needed (new files are added to `temp` for deletion)."""
        if not media.available():
            kind, reason = classify(path)
            return kind, path, reason
        ext = os.path.splitext(path)[1].lower()
        try:
            if ext in VIDEO_LIKE:
                made, reason = await media.threads_video(path)
                if made:
                    temp.append(made)
                return ("video" if made else None), made, reason
            if ext in IMAGE_LIKE:
                kind, made, derived, reason = await media.threads_image(path)
                if derived:
                    temp.append(made)
                return kind, made, reason
        except Exception as e:  # noqa: BLE001 - a broken file must not stop the post
            return None, None, f"conversion failed: {e}"
        return None, None, f"unsupported type {ext or '(none)'}"

    async def send(self, post: Post):
        if time.time() < self._blocked_until:
            logger.warning("Threads is paused after a refusal from Meta: skipping %s", post.link)
            return
        token = await self.access_token()
        items, skipped, temp = [], 0, []
        for path in post.media:
            if len(items) >= CAROUSEL_MAX or self.uploader is None:
                logger.warning("Threads skips %s: %s", os.path.basename(path),
                               "no upload folder" if self.uploader is None else "too many items")
                skipped += 1
                continue
            kind, use, reason = await self._prepare(path, temp)
            if kind is None:
                logger.warning("Threads skips %s: %s", os.path.basename(path), reason)
                skipped += 1
            else:
                items.append((kind, use))

        hosted, names, rejections = [], [], {}
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
                parts = compose_parts(post, skipped)
                if not parts[0] and not hosted:
                    return
                try:
                    post_id = await self._publish(token, parts[0], hosted)
                    break
                except MediaRejected as e:
                    url = hosted[e.index][1]
                    tries = rejections.get(url, 0)
                    if tries < len(MEDIA_RETRY_DELAYS):
                        rejections[url] = tries + 1
                        delay = MEDIA_RETRY_DELAYS[tries]
                        logger.warning("Threads refused a media item (%s), trying again in %ss "
                                       "(%s of %s)", e, delay, tries + 1, len(MEDIA_RETRY_DELAYS))
                        await asyncio.sleep(delay)
                        continue
                    logger.warning("Threads can't use a media item (%s), leaving it out", e)
                    del hosted[e.index]
                    skipped += 1
                except ThreadsError as e:
                    if is_account_level(e):
                        self._block(e)
                        raise
                    if not hosted:
                        raise
                    logger.warning("Threads refused the media post (%s), posting text and a link", e)
                    skipped += len(hosted)
                    hosted = []
        finally:
            if names:
                await self.uploader.remove(names)
            for path in temp:
                try:
                    os.remove(path)
                except OSError:
                    pass

        self._blocks = 0
        first_id = post_id
        for number, part in enumerate(parts[1:], start=2):
            await asyncio.sleep(REPLY_PAUSE)
            try:
                post_id = await self._publish(token, part, [], reply_to=post_id)
            except Exception as e:  # noqa: BLE001 - the post is out, say what is missing
                logger.error("Threads continuation %s of %s failed (%s): the post %s "
                             "is up without it", number, len(parts), e, first_id)
                break

        link = ""
        try:
            info = await self.api("GET", first_id, token, fields="permalink")
            link = f", {info['permalink']}" if info.get("permalink") else ""
        except Exception:  # noqa: BLE001 - the permalink is only for the log
            pass
        logger.info("Threads post published: id %s, %s media, %s part(s)%s",
                    first_id, len(hosted), len(parts), link)
