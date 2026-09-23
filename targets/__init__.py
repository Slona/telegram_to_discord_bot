"""Relay targets. Each target is enabled only when its env vars are set."""

import logging
from dataclasses import dataclass, field

logger = logging.getLogger("tg_relay")


@dataclass
class Post:
    """A Telegram post in a platform-neutral form."""
    source: str                # channel title
    text: str                  # Telegram text in Markdown (links/formatting kept)
    html: str                  # same text as Telegram-flavoured HTML
    plain: str                 # same text with no formatting at all
    link: str                  # URL of the original Telegram post
    media: list[str] = field(default_factory=list)  # local file paths


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
    from .tumblr import TumblrTarget
    from .vk import VkTarget

    candidates = [DiscordTarget, MaxTarget, VkTarget, TumblrTarget]
    targets = []
    for cls in candidates:
        target = cls.from_env()
        if target is not None:
            targets.append(target)
    return targets
