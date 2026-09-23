import logging
import os
import textwrap

import nextcord

from . import Post, Target

logger = logging.getLogger("tg_relay.discord")

MSG_LIMIT = 2000
ATTACHMENT_LIMIT = 10


class DiscordTarget(Target):
    name = "discord"

    def __init__(self, url):
        self.url = url
        self.webhook = None

    @classmethod
    def from_env(cls):
        url = os.environ.get("WEBHOOK")
        return cls(url) if url else None

    async def setup(self, session):
        self.webhook = nextcord.Webhook.from_url(self.url, session=session)

    async def send_text(self, text, username):
        for line in textwrap.wrap(text, MSG_LIMIT, replace_whitespace=False):
            await self.webhook.send(content=line, username=username)

    async def send(self, post: Post):
        if not post.media:
            if post.text:
                logger.info("Sending text only")
                await self.send_text(post.text, post.source)
            return

        caption = post.text
        files = [nextcord.File(p) for p in post.media[:ATTACHMENT_LIMIT]]
        try:
            logger.info("Sending %s attachment(s)", len(files))
            await self.webhook.send(files=files, username=post.source)
        except Exception:
            # Usually the attachment is over Discord's size limit; link the post instead.
            logger.exception("Upload failed, linking to the Telegram post instead")
            caption = f"{caption}\n\n{post.link}" if caption else post.link
        finally:
            for f in files:
                f.close()
        if caption:
            await self.send_text(caption, post.source)
