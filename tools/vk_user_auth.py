#!/usr/bin/env python3
"""One-time sign-in that gets a VK *user* token for a community admin.

A community key can only post text; bots that post pictures to a community
wall use an admin's user token. This asks VK for one through your own VK app
(VK_APP_ID, the "ID приложения" in the app settings) via the Implicit Flow.

1. Run the script, open the printed link in a browser signed in to an account
   that is an ADMIN of the community, approve access.
2. Paste the address the browser ends up on (oauth.vk.com/blank.html#...).

If VK answers "invalid scope", run with --probe: it prints one link per right,
so you can see which rights your app may request. Then pass the allowed ones:
    python3 tools/vk_user_auth.py wall,photos,offline

The token grants access to that personal account (wall, photos, video,
groups), so the token file (VK_USER_TOKEN_FILE, default vk_user_token.json) is
a secret: keep it on the server only and never share it.
"""

import json
import os
import sys
import time
from urllib.parse import parse_qs, urlencode, urlparse

from dotenv import load_dotenv

load_dotenv()

SCOPE = "wall,photos,video,groups,offline"
REDIRECT_URI = "https://oauth.vk.com/blank.html"


def auth_url(app_id, scope):
    return "https://oauth.vk.com/authorize?" + urlencode({
        "client_id": app_id,
        "display": "page",
        "redirect_uri": REDIRECT_URI,
        "scope": scope,
        "response_type": "token",
        "v": "5.199",
    })


def main():
    app_id = os.environ["VK_APP_ID"]
    token_file = os.environ.get("VK_USER_TOKEN_FILE", "vk_user_token.json")
    arg = sys.argv[1] if len(sys.argv) > 1 else SCOPE

    if arg == "--probe":
        print("Open each link. 'invalid scope' right away = the app may not request")
        print("that right; a sign-in/approval page = allowed (just close it, don't approve).\n")
        for right in SCOPE.split(","):
            print(f"{right}:\n{auth_url(app_id, right)}\n")
        return

    url = auth_url(app_id, arg)
    print("Open this link as a community admin and approve access:\n\n" + url + "\n")
    answer = input("Paste the address you were redirected to: ").strip()

    parsed = urlparse(answer)
    fields = parse_qs(parsed.fragment or parsed.query)
    if "access_token" not in fields:
        error = fields.get("error", ["?"])[0]
        description = fields.get("error_description", [""])[0]
        sys.exit(f"No token in the address. VK said: {error} {description}")

    expires_in = int(fields.get("expires_in", ["0"])[0])
    data = {
        "access_token": fields["access_token"][0],
        "user_id": fields.get("user_id", [None])[0],
        "expires_at": time.time() + expires_in if expires_in else 0,
    }
    tmp = f"{token_file}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, token_file)
    try:
        os.chmod(token_file, 0o600)
    except OSError:
        pass
    lifetime = "never expires" if not expires_in else f"expires in {expires_in // 3600} h"
    print(f"Saved the user token to {token_file} ({lifetime})")


if __name__ == "__main__":
    main()
