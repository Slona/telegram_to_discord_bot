#!/usr/bin/env python3
"""Find out how VK wants a real photo uploaded.

Usage: python3 tools/vk_photo_test.py /path/to/photo.jpg

Uploads the same file to VK's wall upload server in several ways and prints
which of them VK accepts (a non-empty "photo" in its answer). Nothing is
published and nothing is saved on the wall: only the upload step runs.
Needs the same .env as the bot (VK_TOKEN, VK_GROUP_ID, VK_APP_ID, ...) and a
valid vk_user_token.json.
"""

import asyncio
import mimetypes
import os
import sys

import aiohttp
from dotenv import load_dotenv

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from targets.vk import UserTokenError, VkError, VkTarget  # noqa: E402

load_dotenv()


async def attempt(target, token, label, build_form):
    try:
        server = await target.call("photos.getWallUploadServer", _token=token,
                                   group_id=target.group_id)
    except VkError as e:
        print(f"[FAIL] {label}: no upload server ({e})")
        return
    handles = []
    try:
        form = build_form(handles)
        async with target.session.post(server["upload_url"], data=form,
                                       ssl=target.ssl_context) as resp:
            body = await resp.json(content_type=None)
    except Exception as e:  # noqa: BLE001
        print(f"[FAIL] {label}: {type(e).__name__}: {e}")
        return
    finally:
        for fh in handles:
            fh.close()
    ok = body.get("photo") not in (None, "", "[]")
    shown = {k: v for k, v in body.items() if k != "hash"}
    print(f"[{' OK ' if ok else 'FAIL'}] {label}: {shown if not ok else 'accepted'}")


async def main():
    path = sys.argv[1]
    data = open(path, "rb").read()
    ctype = mimetypes.guess_type(path)[0] or "image/jpeg"
    name = os.path.basename(path)
    print(f"file: {name}, {len(data)} bytes, {ctype}, starts with {data[:4].hex()}")

    target = VkTarget.from_env()
    async with aiohttp.ClientSession() as session:
        await target.setup(session)
        try:
            token = await target.user_token()
        except UserTokenError as e:
            sys.exit(f"user token: {e}")

        def a(handles):
            f = aiohttp.FormData()
            f.add_field("photo", data, filename=name, content_type=ctype)
            return f

        def b(handles):
            f = aiohttp.FormData()
            f.add_field("photo", data, filename=name)
            return f

        def c(handles):
            fh = open(path, "rb")
            handles.append(fh)
            f = aiohttp.FormData()
            f.add_field("photo", fh, filename=name)
            return f

        def d(handles):
            f = aiohttp.FormData()
            f.add_field("photo", data, filename="photo.jpg", content_type="image/jpeg")
            return f

        await attempt(target, token, "A bytes + Content-Type (what the bot does now)", a)
        await attempt(target, token, "B bytes, no Content-Type", b)
        await attempt(target, token, "C file object, no Content-Type (old bot code)", c)
        await attempt(target, token, "D bytes, fixed name photo.jpg + image/jpeg", d)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    asyncio.run(main())
