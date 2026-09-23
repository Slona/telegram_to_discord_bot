#!/usr/bin/env python3
"""Print chat_id values that MAX reports to the bot, to find a channel's MAX_CHAT_ID.

Add the bot to the channel as an admin, run this script, then publish any post in
the channel (or re-add the bot). Stop with Ctrl+C.

Reads MAX_TOKEN (and optionally MAX_API_URL / MAX_CA_FILE) from the environment/.env.
"""

import asyncio
import os
import ssl

import aiohttp
from dotenv import load_dotenv

load_dotenv()


def find_chat_ids(node):
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "chat_id":
                yield value
            else:
                yield from find_chat_ids(value)
    elif isinstance(node, list):
        for item in node:
            yield from find_chat_ids(item)


async def main():
    token = os.environ["MAX_TOKEN"]
    api = os.environ.get("MAX_API_URL", "https://platform-api2.max.ru").rstrip("/")
    ctx = ssl.create_default_context()
    if os.environ.get("MAX_CA_FILE"):
        ctx.load_verify_locations(cafile=os.environ["MAX_CA_FILE"])

    marker = None
    print("Waiting for updates... publish something in the channel.")
    async with aiohttp.ClientSession() as session:
        while True:
            params = {"timeout": 30}
            if marker is not None:
                params["marker"] = marker
            async with session.get(f"{api}/updates", params=params, ssl=ctx,
                                   headers={"Authorization": token}) as resp:
                data = await resp.json(content_type=None)
            marker = data.get("marker", marker)
            for update in data.get("updates", []):
                ids = sorted(set(find_chat_ids(update)))
                print(f"{update.get('update_type')}: chat_id={ids}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
