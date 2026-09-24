#!/usr/bin/env python3
"""Check what a VK community access key is actually allowed to do.

Reads VK_TOKEN (community key) and VK_GROUP_ID (number, no minus) from the
environment/.env. Optional VK_CA_FILE points at a PEM bundle to trust instead of
the system store (e.g. `python -c "import certifi; print(certifi.where())"`).

Nothing visible is published: the test post is a postponed post a year ahead.
A community key can't delete posts (wall.delete), so up to TWO postponed posts
("api check photo/doc, ignore") stay behind: remove them under "Отложенные записи".
"""

import asyncio
import base64
import os
import ssl
import time

import aiohttp
from dotenv import load_dotenv

load_dotenv()

API = "https://api.vk.com/method/"
VERSION = os.environ.get("VK_API_VERSION", "5.199")

# 1x1 PNG used as the test upload.
TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


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


async def upload_doc(session, token, group_id):
    """Upload TINY_PNG as a community document; return 'doc<owner>_<id>' or None."""
    server = report("docs.getWallUploadServer (doc upload)",
                    await call(session, token, "docs.getWallUploadServer",
                               group_id=group_id))
    if not server:
        return None
    form = aiohttp.FormData()
    form.add_field("file", TINY_PNG, filename="api_check.png", content_type="image/png")
    async with session.post(server["upload_url"], data=form) as resp:
        uploaded = await resp.json(content_type=None)
    if "file" not in uploaded:
        print(f"[FAIL] doc upload to VK server: {uploaded}")
        return None
    saved = report("docs.save", await call(session, token, "docs.save",
                                           file=uploaded["file"], title="api check"))
    if not saved:
        return None
    doc = saved["doc"]
    return f"doc{doc['owner_id']}_{doc['id']}"


async def upload_message_photo(session, token):
    """Upload TINY_PNG via the messages route; return 'photo<owner>_<id>[_key]' or None."""
    server = report("photos.getMessagesUploadServer (photo upload)",
                    await call(session, token, "photos.getMessagesUploadServer"))
    if not server:
        return None
    form = aiohttp.FormData()
    form.add_field("photo", TINY_PNG, filename="api_check.png", content_type="image/png")
    async with session.post(server["upload_url"], data=form) as resp:
        uploaded = await resp.json(content_type=None)
    if not uploaded.get("photo"):
        print(f"[FAIL] photo upload to VK server: {uploaded}")
        return None
    saved = report("photos.saveMessagesPhoto",
                   await call(session, token, "photos.saveMessagesPhoto",
                              photo=uploaded["photo"], server=uploaded["server"],
                              hash=uploaded["hash"]))
    if not saved:
        return None
    photo = saved[0]
    key = f"_{photo['access_key']}" if photo.get("access_key") else ""
    return f"photo{photo['owner_id']}_{photo['id']}{key}"


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

        # Other photo upload routes, availability only.
        for method, extra in (
            ("photos.getWallUploadServer", {"group_id": group_id}),
            ("photos.getUploadServer", {"group_id": group_id}),
            ("photos.getMessagesUploadServer", {}),
        ):
            report(f"{method} (photo upload route)",
                   await call(session, token, method, **extra))

        photo = await upload_message_photo(session, token)
        doc = await upload_doc(session, token, group_id)

        # Publish one postponed post per route, then READ IT BACK: wall.post
        # accepts attachments it later drops, so success alone proves nothing.
        for kind, attachment in (("photo", photo), ("doc", doc)):
            if not attachment:
                continue
            posted = report(f"wall.post (postponed, with the {kind} attached)",
                            await call(session, token, "wall.post", owner_id=-group_id,
                                       from_group=1, message=f"api check {kind}, ignore",
                                       attachments=attachment,
                                       publish_date=int(time.time()) + 365 * 24 * 3600))
            if not posted:
                continue
            back = report(f"wall.getById ({kind} post read back)",
                          await call(session, token, "wall.getById",
                                     posts=f"-{group_id}_{posted['post_id']}"))
            if back is None:
                continue
            items = back.get("items", back) if isinstance(back, dict) else back
            if not items:
                print(f"[WARN] {kind}: VK returned nothing for the postponed post, "
                      "can't verify; look at it in 'Отложенные записи' by hand")
                continue
            kinds = [a.get("type") for a in (items[0].get("attachments") or [])]
            print(f"[{' OK ' if kind in kinds else 'FAIL'}] the {kind} attachment is "
                  f"{'really on the post' if kind in kinds else 'MISSING from the post'}"
                  f" (attachments seen: {kinds})")

        video = report("video.save (video upload)",
                       await call(session, token, "video.save", group_id=group_id,
                                  name="api check", is_private=1))
        if video:
            print("       video upload is allowed; delete the stub video "
                  f"manually: video_id={video.get('video_id')}")

    print("\nSend me only the [ OK ]/[FAIL] lines above, never the token.")
    print("Then delete the postponed 'api check ...' posts in the community.")


if __name__ == "__main__":
    asyncio.run(main())
