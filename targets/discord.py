import logging
import os
import textwrap
import time

import nextcord

from . import Post, Target, with_poll_text

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
        self.session = None
        self.native_polls = True     # switched off after Discord refuses one

    @classmethod
    def from_env(cls):
        url = os.environ.get("WEBHOOK")
        return cls(url) if url else None

    async def setup(self, session):
        self.session = session
        self.webhook = nextcord.Webhook.from_url(self.url, session=session)

    async def send_poll(self, post):
        """A native poll through the webhook (nextcord can't, so a raw request).
        Returns False if Discord won't take it, and the caller sends text instead."""
        poll = post.poll
        if not (self.native_polls and 2 <= len(poll.options) <= 10
                and len(poll.question) <= 300 and all(1 <= len(o) <= 55 for o in poll.options)):
            return False
        hours = 168
        if poll.close_date is not None:
            hours = max(1, min(768, int((poll.close_date.timestamp() - time.time()) // 3600) + 1))
        body = {"username": post.source, "poll": {
            "question": {"text": poll.question},
            "answers": [{"poll_media": {"text": o}} for o in poll.options],
            "duration": hours, "allow_multiselect": poll.multiple}}
        async with self.session.post(self.url, params={"wait": "true"}, json=body) as resp:
            if resp.status < 300:
                return True
            reply = (await resp.text())[:300]
        logger.warning("Discord refused the poll (HTTP %s: %s): sending it as text from now on",
                       resp.status, reply)
        self.native_polls = False
        return False

    async def send_text(self, text, username):
        for line in textwrap.wrap(text, MSG_LIMIT, replace_whitespace=False):
            await self.webhook.send(content=line, username=username)

    async def send(self, post: Post):
        if post.poll is not None:
            if await self.send_poll(post):
                logger.info("Sent a poll")
                return
            post = with_poll_text(post)
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
