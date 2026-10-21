"""Relay targets. Each target is enabled only when its env vars are set."""

import asyncio
import logging
from dataclasses import dataclass, field

logger = logging.getLogger("tg_relay")

# OAuth tokens that only change hands when a post needs them could expire from
# disuse, so every target that holds a refresh token renews it at least this often.
KEEPALIVE_AFTER = 24 * 3600     # renew a token pair older than this
KEEPALIVE_CHECK = 3600          # how often to look
KEEPALIVE_BACKOFF = 6 * 3600    # wait this long after a failed attempt


async def keepalive_loop(name, step):
    """Call `step()` every KEEPALIVE_CHECK seconds, forever. `step` renews the
    token if it is due and raises on failure (which is logged, then retried later)."""
    delay = KEEPALIVE_CHECK
    while True:
        await asyncio.sleep(delay)
        try:
            await step()
            delay = KEEPALIVE_CHECK
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("%s token keepalive failed, trying again in %s h",
                             name, KEEPALIVE_BACKOFF // 3600)
            delay = KEEPALIVE_BACKOFF


@dataclass
class Post:
    """A Telegram post in a platform-neutral form."""
    source: str                # channel title
    text: str                  # Telegram text in Markdown (links/formatting kept)
    html: str                  # same text as Telegram-flavoured HTML
    plain: str                 # same text with no formatting at all
    link: str                  # URL of the original Telegram post
    media: list[str] = field(default_factory=list)  # local file paths
    # URL buttons under the Telegram post, as rows of (label, url)
    buttons: list[list[tuple[str, str]]] = field(default_factory=list)

    def flat_buttons(self):
        return [button for row in self.buttons for button in row]


class Target:
    name = "target"

    @classmethod
    def from_env(cls):
        """Return an instance if the env is configured for this target, else None."""
        raise NotImplementedError

    async def setup(self, session):
        """Called once with the shared aiohttp session before the first send."""

    async def send(self, post: Post):
        raise NotImplementedError


def load_targets():
    from .discord import DiscordTarget
    from .max import MaxTarget
    from .threads import ThreadsTarget
    from .tumblr import TumblrTarget
    from .vk import VkTarget

    candidates = [DiscordTarget, MaxTarget, VkTarget, TumblrTarget, ThreadsTarget]
    targets = []
    for cls in candidates:
        try:
            target = cls.from_env()
        except Exception:
            # A misconfigured target must not take the others down with it.
            logger.exception("Target %s has invalid settings and is disabled", cls.name)
            continue
        if target is not None:
            targets.append(target)
    return targets
