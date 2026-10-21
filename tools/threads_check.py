#!/usr/bin/env python3
"""Check the Threads setup end to end WITHOUT publishing anything.

Steps: the token works; a test picture can be uploaded to the public folder and
fetched back over HTTPS; Threads accepts that URL (a media container is created
and must reach FINISHED); the picture is removed again. Containers that are
never published vanish by themselves after a day.

Run it with the bot's .env, on the machine that holds the token and the upload key.
"""

import asyncio
import base64
import os
import struct
import sys
import tempfile
import time
import zlib

import requests
from dotenv import load_dotenv

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from targets.threads import ThreadsError, ThreadsTarget  # noqa: E402

load_dotenv()


def make_png(width=800, height=450):
    def chunk(tag, data):
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))
    rows = b"".join(
        b"\x00" + b"".join(bytes((x * 255 // width, y * 255 // height, 160)) for x in range(width))
        for y in range(height))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


def line(ok, text):
    print(f"[{' OK ' if ok else 'FAIL'}] {text}")
    return ok


async def main():
    target = ThreadsTarget.from_env()
    if target is None:
        sys.exit("THREADS_APP_ID is not set")
    await target.setup(None)
    target._keepalive.cancel()
    try:
        token = await target.access_token()
        me = await target.api("GET", "me", token, fields="id,username")
        line(True, f"token works: @{me.get('username')} (id {me['id']})")
    except Exception as e:  # noqa: BLE001
        line(False, f"token: {e}")
        return

    if target.uploader is None:
        line(False, "media upload is not configured (THREADS_MEDIA_SFTP/KEY/URL)")
        return
    path = os.path.join(tempfile.mkdtemp(), "threads_check.png")
    with open(path, "wb") as fh:
        fh.write(make_png())
    names = []
    try:
        (name, url), = await target.uploader.put([path])
        names.append(name)
        line(True, "uploaded the test picture over SFTP")
        resp = requests.get(url, timeout=30)
        if not line(resp.status_code == 200 and resp.headers.get("content-type", "").startswith("image/"),
                    f"public URL answers: HTTP {resp.status_code}, {resp.headers.get('content-type')}"):
            return
        container = await target._create(token, media_type="IMAGE", image_url=url,
                                         text="threads check, never published")
        line(True, "Threads accepted the picture URL")
        for _ in range(30):
            info = await target.api("GET", container, token, fields="status,error_message")
            if info.get("status") in ("FINISHED", "ERROR", "EXPIRED"):
                break
            await asyncio.sleep(3)
        line(info.get("status") == "FINISHED",
             f"Threads fetched and processed it: {info.get('status')} {info.get('error_message', '')}")
    except (ThreadsError, RuntimeError) as e:
        line(False, str(e)[:300])
    finally:
        if names:
            await target.uploader.remove(names)
            line(True, "removed the test picture from the upload folder")
    print("\nNothing was published.")


if __name__ == "__main__":
    asyncio.run(main())
