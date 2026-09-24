#!/usr/bin/env python3
"""Check what a VK community access key is actually allowed to do.

Reads VK_TOKEN (community key) and VK_GROUP_ID (number, no minus) from the
environment/.env. Optional VK_CA_FILE points at a PEM bundle to trust instead of
the system store (e.g. `python -c "import certifi; print(certifi.where())"`).

Nothing visible is published: the test post is a postponed post a year ahead.
A community key can't delete posts (wall.delete), so ONE postponed post
("api check link, ignore") stays behind: remove them under "Отложенные записи".
"""

import asyncio
import base64
import json
import os
import ssl
import struct
import time
import zlib

import aiohttp
from dotenv import load_dotenv

load_dotenv()

API = "https://api.vk.com/method/"
VERSION = os.environ.get("VK_API_VERSION", "5.199")

# 1x1 PNG, enough for upload-route probing.
TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def make_png(width=800, height=450):
    """A colourful gradient PNG, big enough to tell how VK renders an image."""
    def chunk(tag, data):
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))
    rows = b"".join(
        b"\x00" + b"".join(bytes((x * 255 // width, y * 255 // height, 160))
                          for x in range(width))
        for y in range(height)
    )
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


TEST_IMAGE = make_png()


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
    form.add_field("file", TEST_IMAGE, filename="api_check.png", content_type="image/png")
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
    form.add_field("photo", TEST_IMAGE, filename="api_check.png", content_type="image/png")
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


USER_TOKEN_FILE = os.environ.get("VK_USER_TOKEN_FILE", "vk_user_token.json")


async def user_token_checks(session, group_id, community_token):
    """Same job with an admin's USER token (tools/vk_user_auth.py), the way
    bots that post pictures to a community wall do it."""
    try:
        with open(USER_TOKEN_FILE, encoding="utf-8") as fh:
            token = json.load(fh)["access_token"]
    except (OSError, ValueError, KeyError):
        print(f"\n(no {USER_TOKEN_FILE}: user-token checks skipped, see tools/vk_user_auth.py)")
        return
    print("\n--- with the admin's USER token ---")
    report("users.get (token works)", await call(session, token, "users.get"))

    attachment = None
    server = report("photos.getWallUploadServer",
                    await call(session, token, "photos.getWallUploadServer",
                               group_id=group_id))
    if server:
        form = aiohttp.FormData()
        form.add_field("photo", TEST_IMAGE, filename="api_check.png", content_type="image/png")
        async with session.post(server["upload_url"], data=form) as resp:
            uploaded = await resp.json(content_type=None)
        if uploaded.get("photo") in (None, "", "[]"):
            print(f"[FAIL] photo upload to VK server: {uploaded}")
        else:
            saved = report("photos.saveWallPhoto",
                           await call(session, token, "photos.saveWallPhoto",
                                      group_id=group_id, photo=uploaded["photo"],
                                      server=uploaded["server"], hash=uploaded["hash"]))
            if saved:
                attachment = f"photo{saved[0]['owner_id']}_{saved[0]['id']}"

    if attachment:
        print(f"       uploaded photo belongs to owner {attachment.split('_')[0][5:]}")
        # Hybrid: the user token uploads, the COMMUNITY key publishes (a user token
        # of a non-standalone app may not call wall.post).
        report("HYBRID wall.post by the community key with the user-uploaded photo",
               await call(session, community_token, "wall.post", owner_id=-group_id,
                          from_group=1, message="api check hybrid photo, ignore",
                          attachments=attachment,
                          publish_date=int(time.time()) + 365 * 24 * 3600))
        post = report("wall.post (postponed, as the community, with the photo)",
                      await call(session, token, "wall.post", owner_id=-group_id,
                                 from_group=1, message="api check user photo, ignore",
                                 attachments=attachment,
                                 publish_date=int(time.time()) + 365 * 24 * 3600))
        if post:
            back = report("wall.getById (read back)",
                          await call(session, token, "wall.getById",
                                     posts=f"-{group_id}_{post['post_id']}"))
            items = (back.get("items", back) if isinstance(back, dict) else back) or []
            kinds = [a.get("type") for a in (items[0].get("attachments") or [])] if items else None
            if kinds is None:
                print("[WARN] can't read the postponed post back: check "
                      "'api check user photo' in 'Отложенные записи' by hand")
            else:
                print(f"[{' OK ' if 'photo' in kinds else 'FAIL'}] photo on the post: {kinds}")
                report("wall.delete (cleanup)",
                       await call(session, token, "wall.delete", owner_id=-group_id,
                                  post_id=post["post_id"]))

    video = report("video.save (video upload)",
                   await call(session, token, "video.save", group_id=group_id,
                              name="api check", is_private=1))
    if video:
        report("video.delete (cleanup of the empty stub)",
               await call(session, token, "video.delete", owner_id=video.get("owner_id"),
                          video_id=video.get("video_id")))


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

        # VK refuses a link attachment without a picture ("No photo given") and a
        # community key can't ask VK for one (wall.parseAttachedLink is closed), so
        # try passing our own uploaded photo as the snippet picture (link_photo_id).
        link = os.environ.get("VK_TEST_LINK", "https://t.me/plushstream/7668")
        photo = await upload_message_photo(session, token)
        variants = []
        if photo:
            bare = photo.replace("photo", "", 1)          # <owner>_<id>[_<key>]
            variants.append(("owner_id without access key", "_".join(bare.split("_")[:2])))
            variants.append(("owner_id with access key", bare))
        for label, photo_id in variants:
            report(f"wall.post (postponed, link snippet, link_photo_id = {label})",
                   await call(session, token, "wall.post", owner_id=-group_id,
                              from_group=1, message=f"api check link ({label}), ignore",
                              attachments=link, link_title="api check snippet",
                              link_photo_id=photo_id,
                              publish_date=int(time.time()) + 365 * 24 * 3600))

        video = report("video.save (video upload)",
                       await call(session, token, "video.save", group_id=group_id,
                                  name="api check", is_private=1))
        if video:
            print("       video upload is allowed; delete the stub video "
                  f"manually: video_id={video.get('video_id')}")

        await user_token_checks(session, group_id, token)

    print("\nSend me only the [ OK ]/[FAIL] lines above, never the token.")
    print("Then look at the postponed 'api check hybrid photo' post: is the picture there?")


if __name__ == "__main__":
    asyncio.run(main())
