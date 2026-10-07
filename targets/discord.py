import logging
import os
import textwrap

import nextcord

from . import Post, Target

logger = logging.getLogger("tg_relay.discord")

MSG_LIMIT = 2000
ATTACHMENT_LIMIT = 10


def with_buttons(post):
    """Post text plus its buttons as a line of links (webhooks can't carry buttons).
    <url> keeps Discord from expanding every link into a big embed."""
    text = post.text
    buttons = post.flat_buttons()
    if buttons:
        links = " | ".join(
            f"[{label.replace('[', '(').replace(']', ')')}](<{url}>)" for label, url in buttons)
        text = f"{text}\n\n{links}" if text else links
    return text


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
        text = with_buttons(post)
        if not post.media:
            if text:
                logger.info("Sending text only")
                await self.send_text(text, post.source)
            return

        caption = text
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
