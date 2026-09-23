#!/usr/bin/env python3
"""Check what a VK community access key is actually allowed to do.

Reads VK_TOKEN (community key) and VK_GROUP_ID (number, no minus) from the
environment/.env. Optional VK_CA_FILE points at a PEM bundle to trust instead of
the system store (e.g. the path printed by `python -c "import certifi; print(certifi.where())"`). Nothing visible is published: the test post is created as a
postponed post a year ahead and deleted right away.
"""

import asyncio
import os
import ssl
import time

import aiohttp
from dotenv import load_dotenv

load_dotenv()

API = "https://api.vk.com/method/"
VERSION = os.environ.get("VK_API_VERSION", "5.199")


async def call(session, token, method, **params):
    params["v"] = VERSION
    async with session.post(API + method, data=params,
                            headers={"Authorization": f"Bearer {token}"}) as resp:
        return await resp.json(content_type=None)


def report(label, data):
    if "error" in data:
        err = data["error"]
        print(f"[FAIL] {label}: {err.get('error_code')} {err.get('error_msg')}")
        return None
    print(f"[ OK ] {label}")
    return data["response"]


async def main():
    token = os.environ["VK_TOKEN"]
    group_id = int(os.environ["VK_GROUP_ID"])

    ca_file = os.environ.get("VK_CA_FILE")
    ctx = ssl.create_default_context(cafile=ca_file) if ca_file else None
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ctx)) as session:
        info = report("groups.getById", await call(session, token, "groups.getById",
                                                   group_id=group_id))
        if info:
            groups = info.get("groups", info) if isinstance(info, dict) else info
            print("       community:", groups[0].get("name"))

        server = report("photos.getWallUploadServer (photo upload)",
                        await call(session, token, "photos.getWallUploadServer",
                                   group_id=group_id))

        post = report("wall.post (postponed, from community)",
                      await call(session, token, "wall.post", owner_id=-group_id,
                                 from_group=1, message="api check, ignore",
                                 publish_date=int(time.time()) + 365 * 24 * 3600))
        if post:
            report("wall.delete (cleanup)",
                   await call(session, token, "wall.delete", owner_id=-group_id,
                              post_id=post["post_id"]))

        video = report("video.save (video upload)",
                       await call(session, token, "video.save", group_id=group_id,
                                  name="api check", is_private=1))
        if video:
            print("       video upload is allowed; a stub video was created,"
                  f" delete it: video_id={video.get('video_id')}")
            await call(session, token, "video.delete", owner_id=video.get("owner_id"),
                       video_id=video.get("video_id"))

    print("\nSend me only the [ OK ]/[FAIL] lines above, never the token.")


if __name__ == "__main__":
    asyncio.run(main())
