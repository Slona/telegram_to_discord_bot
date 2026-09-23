"""Relay target for a Tumblr blog (Neue Post Format, OAuth2).

Env:
  TUMBLR_CLIENT_ID      OAuth consumer key of your Tumblr application
  TUMBLR_CLIENT_SECRET  OAuth consumer secret
  TUMBLR_BLOG           blog identifier, e.g. "myblog" or "myblog.tumblr.com"
  TUMBLR_TOKEN_FILE     optional, where the OAuth tokens live (default
                        tumblr_token.json); create it with tools/tumblr_auth.py

Tumblr access tokens last ~42 minutes and each refresh hands out a new
refresh token, so the token file is rewritten on every refresh.
"""

import asyncio
import json
import logging
import mimetypes
import os
import time
import uuid
from html.parser import HTMLParser

import aiohttp

from . import Post, Target

logger = logging.getLogger("tg_relay.tumblr")

API_BASE = "https://api.tumblr.com/v2"
TEXT_BLOCK_LIMIT = 4096   # code points
IMAGES_PER_POST = 30
VIDEOS_PER_POST = 1       # native (uploaded) videos
LINKS_PER_POST = 100
REFRESH_MARGIN = 60       # seconds before expiry to refresh

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif"}
VIDEO_EXTS = {".mp4", ".mov"}

# Upload-related error codes: worth retrying the post without its media.
UPLOAD_ERROR_CODES = {8005, 8006, 8009, 8010, 8011, 8003, 8004}


class TumblrError(Exception):
    def __init__(self, status, body):
        super().__init__(f"Tumblr API error {status}: {body}")
        self.status = status
        self.body = body
        try:
            errors = json.loads(body).get("errors", [])
            self.codes = {e.get("code") for e in errors}
        except (ValueError, AttributeError):
            self.codes = set()


class TokenStore:
    """OAuth2 tokens kept in a JSON file, rewritten atomically on refresh."""

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

    @staticmethod
    def from_token_response(resp):
        return {
            "access_token": resp["access_token"],
            "refresh_token": resp["refresh_token"],
            "expires_at": time.time() + int(resp.get("expires_in", 2520)),
        }


class _NpfText(HTMLParser):
    """Telegram HTML -> plain text plus NPF inline formatting ranges.

    NPF counts positions in Unicode code points, which is what len() of a
    Python str gives (Telegram's own offsets are UTF-16, hence going via HTML).
    """

    SIMPLE = {"strong": "bold", "b": "bold", "em": "italic", "i": "italic",
              "del": "strikethrough", "s": "strikethrough", "strike": "strikethrough"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.text = ""
        self.formatting = []
        self._open = []   # (tag, npf type, start, extra)

    def handle_starttag(self, tag, attrs):
        if tag == "br":
            self.text += "\n"
        elif tag in self.SIMPLE:
            self._open.append((tag, self.SIMPLE[tag], len(self.text), {}))
        elif tag == "a":
            href = dict(attrs).get("href") or ""
            if href.startswith(("http://", "https://")):
                self._open.append((tag, "link", len(self.text), {"url": href}))

    def handle_data(self, data):
        self.text += data

    def handle_endtag(self, tag):
        for i in range(len(self._open) - 1, -1, -1):
            if self._open[i][0] == tag:
                _, kind, start, extra = self._open.pop(i)
                if len(self.text) > start:
                    self.formatting.append({"start": start, "end": len(self.text),
                                            "type": kind, **extra})
                return


def html_to_npf_text(html_text):
    parser = _NpfText()
    parser.feed(html_text)
    parser.close()
    return parser.text, parser.formatting


def text_blocks(text, formatting):
    """One NPF text block per non-empty line (<= 4096 code points each)."""
    blocks, offset, links = [], 0, 0
    for line in text.split("\n"):
        line_start = offset
        offset += len(line) + 1
        if not line.strip():
            continue
        for c in range(0, len(line), TEXT_BLOCK_LIMIT):
            chunk = line[c:c + TEXT_BLOCK_LIMIT]
            start = line_start + c
            end = start + len(chunk)
            fmts = []
            for f in formatting:
                fs, fe = max(f["start"], start), min(f["end"], end)
                if fs >= fe:
                    continue
                if f["type"] == "link":
                    if links >= LINKS_PER_POST:
                        continue
                    links += 1
                fmts.append({**f, "start": fs - start, "end": fe - start})
            block = {"type": "text", "text": chunk}
            if fmts:
                block["formatting"] = fmts
            blocks.append(block)
    return blocks


def link_block(url):
    return {"type": "text", "text": url,
            "formatting": [{"start": 0, "end": len(url), "type": "link", "url": url}]}


class TumblrTarget(Target):
    name = "tumblr"

    def __init__(self, client_id, client_secret, blog, token_file):
        self.client_id = client_id
        self.client_secret = client_secret
        self.blog = blog
        self.store = TokenStore(token_file)
        self.tokens = None
        self.session = None
        self._refresh_lock = asyncio.Lock()

    @classmethod
    def from_env(cls):
        client_id = os.environ.get("TUMBLR_CLIENT_ID")
        secret = os.environ.get("TUMBLR_CLIENT_SECRET")
        blog = os.environ.get("TUMBLR_BLOG")
        if not (client_id and secret and blog):
            return None
        return cls(client_id, secret, blog,
                   os.environ.get("TUMBLR_TOKEN_FILE", "tumblr_token.json"))

    async def setup(self, session):
        self.session = session
        try:
            self.tokens = self.store.load()
        except (OSError, ValueError):
            logger.error("No Tumblr tokens in %s: run tools/tumblr_auth.py first",
                         self.store.path)
            raise

    async def access_token(self):
        async with self._refresh_lock:
            if self.tokens["expires_at"] - time.time() < REFRESH_MARGIN:
                await self._refresh()
            return self.tokens["access_token"]

    async def _refresh(self):
        logger.info("Refreshing the Tumblr access token")
        async with self.session.post(f"{API_BASE}/oauth2/token", data={
            "grant_type": "refresh_token",
            "refresh_token": self.tokens["refresh_token"],
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }) as resp:
            body = await resp.text()
            if resp.status != 200:
                raise TumblrError(resp.status, body + " (rerun tools/tumblr_auth.py?)")
            self.tokens = TokenStore.from_token_response(json.loads(body))
        self.store.save(self.tokens)

    async def create_post(self, content, files):
        """POST the NPF post; `files` maps identifier -> (path, mime type)."""
        form = aiohttp.FormData()
        form.add_field("json", json.dumps({"content": content, "state": "published"}),
                       content_type="application/json")
        handles = []
        try:
            for identifier, (path, mime) in files.items():
                fh = open(path, "rb")
                handles.append(fh)
                form.add_field(identifier, fh, filename=os.path.basename(path),
                               content_type=mime)
            token = await self.access_token()
            async with self.session.post(
                f"{API_BASE}/blog/{self.blog}/posts", data=form,
                headers={"Authorization": f"Bearer {token}"},
            ) as resp:
                body = await resp.text()
                if resp.status not in (200, 201):
                    raise TumblrError(resp.status, body)
        finally:
            for fh in handles:
                fh.close()

    async def send(self, post: Post):
        images, videos, skipped = [], [], 0
        for path in post.media:
            ext = os.path.splitext(path)[1].lower()
            if ext in IMAGE_EXTS and len(images) < IMAGES_PER_POST:
                images.append(path)
            elif ext in VIDEO_EXTS and len(videos) < VIDEOS_PER_POST:
                videos.append(path)
            else:
                skipped += 1  # audio, stickers, extra videos...

        text, formatting = html_to_npf_text(post.html)
        text_part = text_blocks(text, formatting)

        media_blocks, files = [], {}
        for path, kind in [(p, "image") for p in images] + [(p, "video") for p in videos]:
            identifier = uuid.uuid4().hex
            mime = mimetypes.guess_type(path)[0] or (
                "video/mp4" if kind == "video" else "image/jpeg")
            files[identifier] = (path, mime)
            media = {"type": mime, "identifier": identifier}
            media_blocks.append({"type": kind,
                                 "media": media if kind == "video" else [media]})

        def assemble(blocks, extra_link):
            return blocks + text_part + ([link_block(post.link)] if extra_link else [])

        content = assemble(media_blocks, skipped > 0)
        if not content:
            return
        logger.info("Posting to Tumblr: %s image(s), %s video(s)", len(images), len(videos))
        try:
            await self.create_post(content, files)
        except TumblrError as e:
            if not files or not (e.codes & UPLOAD_ERROR_CODES):
                raise
            # Tumblr refused the media (format, quota, transcoding...): keep the text.
            logger.warning("Tumblr rejected the media (%s), posting text and a link", e.codes)
            await self.create_post(assemble([], True), {})
