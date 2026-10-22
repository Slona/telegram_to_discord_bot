#!/usr/bin/env python3
"""Send posts that were already in the Telegram channel to chosen targets only.

For catching up one platform that missed posts (say Threads was blocked for a
day) without sending them again to the others.

  python3 tools/repost.py --targets threads --ids 7823 7824 7825 7826
  python3 tools/repost.py --targets threads --from-id 7823 --yes
  python3 tools/repost.py --targets threads --since "2026-10-22 00:00"   # user accounts only

The posts found are listed first and you confirm (unless --yes). Albums are sent
as one post, oldest first. A Telegram *bot* session can't read a channel's history,
so --since only works with a user session; for a bot use --ids, or --from-id N
which takes every post from message N up to the newest (the id of a post is the
number at the end of its t.me link, and the bot's log names it when a target fails).

The Telegram session file can't be shared with a running bot, so stop the bot
first and start it again afterwards (it catches up with anything posted meanwhile):
  sudo systemctl stop telegram_to_discord_bot.service
  ... run this ...
  sudo systemctl start telegram_to_discord_bot.service
Run it from the bot's folder (the session file and .env live there).
"""

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime, timezone

import aiohttp
from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.errors import BotMethodInvalidError

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from relay import relay  # noqa: E402
from targets import load_targets  # noqa: E402

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("telethon").setLevel(logging.WARNING)

PAUSE_BETWEEN_POSTS = 5


def group_posts(messages):
    """Album parts belong together; everything else is a post of its own.
    Returns lists of messages, each sorted by id, posts oldest first."""
    albums, posts = {}, []
    for m in messages:
        if getattr(m, "action", None) is not None:      # join/pin/etc. service messages
            continue
        gid = getattr(m, "grouped_id", None)
        if gid is None:
            posts.append([m])
        else:
            albums.setdefault(gid, []).append(m)
    posts += albums.values()
    posts = [sorted(group, key=lambda m: m.id) for group in posts]
    return sorted(posts, key=lambda group: group[0].id)


def describe(group):
    first = group[0]
    when = first.date.astimezone().strftime("%d.%m %H:%M")
    text = (next((m.message for m in group if m.message), "") or "").replace("\n", " ")
    media = sum(1 for m in group if m.media is not None)
    return f"#{first.id} {when}  {media} media  {text[:70]!r}"


async def scan_from(client, chat, start, batch=20, patience=2):
    """Every existing message from id `start` upward, by id (bots may do that).
    Stops after `patience` batches in a row without a single message."""
    found, i, empty = [], start, 0
    while empty < patience:
        got = [m for m in await client.get_messages(chat, ids=list(range(i, i + batch))) if m]
        found += got
        empty = 0 if got else empty + 1
        i += batch
    return found


async def collect(client, chat, args):
    if args.from_id:
        return group_posts(await scan_from(client, chat, args.from_id))
    if args.ids:
        found = {m.id: m for m in await client.get_messages(chat, ids=args.ids) if m}
        albums = {m.grouped_id for m in found.values() if m.grouped_id}
        if albums:      # an album post is only complete with all its parts
            around = {i for m in list(found.values()) if m.grouped_id
                      for i in range(m.id - 10, m.id + 11)} - set(found)
            for m in await client.get_messages(chat, ids=sorted(around)):
                if m and m.grouped_id in albums:
                    found[m.id] = m
        return group_posts(found.values())
    since = datetime.fromisoformat(args.since).astimezone(timezone.utc)
    try:
        found = [m async for m in client.iter_messages(chat, offset_date=since, reverse=True)]
    except BotMethodInvalidError:
        sys.exit("This Telegram session is a bot and can't read the channel history: "
                 "use --ids or --from-id (the number at the end of the post's t.me link).")
    return group_posts(found)


async def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--targets", nargs="+", required=True, help="e.g. threads max vk")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--since", help='local time, e.g. "2026-10-22 00:00"')
    source.add_argument("--ids", nargs="+", type=int, help="Telegram message ids")
    source.add_argument("--from-id", type=int, help="this message id and every newer one")
    parser.add_argument("--channel", type=int, help="channel id (default: first INPUT_CHANNELS)")
    parser.add_argument("--yes", action="store_true", help="don't ask for confirmation")
    args = parser.parse_args()

    dlloc = os.environ["DLLOC"]
    channels = [int(c) for c in os.environ.get("INPUT_CHANNELS", "").split(",") if c.strip()]
    channel = args.channel or (channels[0] if channels else None)
    if channel is None:
        sys.exit("No channel: set INPUT_CHANNELS or pass --channel")

    available = {t.name: t for t in load_targets()}
    unknown = [n for n in args.targets if n not in available]
    if unknown:
        sys.exit(f"Not configured here: {', '.join(unknown)} (have: {', '.join(available)})")
    targets = [available[n] for n in args.targets]

    client = TelegramClient(os.environ["APINAME"], os.environ["APPID"], os.environ["APIHASH"])
    await client.connect()
    try:
        if not await client.is_user_authorized():
            sys.exit("The Telegram session is not signed in; start the bot by hand once.")
        chat = await client.get_entity(channel)
        groups = await collect(client, chat, args)
        if not groups:
            sys.exit("No posts found.")
        print(f"{len(groups)} post(s) for {', '.join(args.targets)}:")
        for group in groups:
            print("  ", describe(group))
        if not args.yes and input("Send them? [y/N] ").strip().lower() != "y":
            sys.exit("Cancelled.")

        async with aiohttp.ClientSession() as session:
            for target in targets:
                await target.setup(session)
            for i, group in enumerate(groups):
                if i:
                    await asyncio.sleep(PAUSE_BETWEEN_POSTS)
                print("->", describe(group))
                await relay(group, chat, targets, dlloc)
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
