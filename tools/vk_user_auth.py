#!/usr/bin/env python3
"""One-time VK ID sign-in for a community admin.

The bot uploads photos and video to the community with this user token (the
community key then publishes the post). VK ID hands out a 1-hour access token
plus a refresh token that the bot swaps for a new pair by itself; every swap
invalidates the previous pair, so the token file (VK_USER_TOKEN_FILE, default
vk_user_token.json) is rewritten each time and must live on ONE machine only.
It gives access to that personal account: treat it as a secret.

1. In the VK app settings add a trusted redirect URL, e.g.
   https://localhost/callback (nothing has to answer there); if you pick a
   different one, put it in VK_REDIRECT_URI.
2. Set VK_APP_ID (plus VK_SERVICE_TOKEN if VK says the app is confidential)
   and run the script.
3. Open the link signed in as a community ADMIN, approve, and paste the address
   the browser ends up on (the page itself may fail to load, that's fine).

Rights can be passed as an argument: python3 tools/vk_user_auth.py "wall photos"
"""

import base64
import hashlib
import os
import secrets
import sys
from urllib.parse import parse_qs, urlencode, urlparse

import requests
from dotenv import load_dotenv

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from targets.vk import VK_ID_URL, UserTokenStore  # noqa: E402

load_dotenv()

SCOPE = "wall photos video groups"


def main():
    app_id = os.environ["VK_APP_ID"]
    redirect_uri = os.environ.get("VK_REDIRECT_URI", "https://localhost/callback")
    token_file = os.environ.get("VK_USER_TOKEN_FILE", "vk_user_token.json")
    vk_id_url = os.environ.get("VK_ID_URL", VK_ID_URL).rstrip("/")
    scope = sys.argv[1].replace(",", " ") if len(sys.argv) > 1 else SCOPE

    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(32)

    url = f"{vk_id_url}/authorize?" + urlencode({
        "response_type": "code",
        "client_id": app_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "scope": scope,
    })
    print("Open this link as a community admin and approve access:\n\n" + url + "\n")
    answer = input("Paste the address you were redirected to: ").strip()

    query = parse_qs(urlparse(answer).query)
    if "code" not in query:
        sys.exit(f"No code in the address. VK said: {query.get('error', ['?'])[0]} "
                 f"{query.get('error_description', [''])[0]}")
    if query.get("state", [None])[0] != state:
        sys.exit("State mismatch: this is not the redirect for this sign-in attempt.")
    device_id = query["device_id"][0]

    data = {
        "grant_type": "authorization_code",
        "code_verifier": verifier,
        "redirect_uri": redirect_uri,
        "code": query["code"][0],
        "client_id": app_id,
        "device_id": device_id,
        "state": state,
    }
    if os.environ.get("VK_SERVICE_TOKEN"):
        data["service_token"] = os.environ["VK_SERVICE_TOKEN"]
    resp = requests.post(f"{vk_id_url}/oauth2/auth", data=data, timeout=30)
    try:
        body = resp.json()
    except ValueError:
        sys.exit(f"Token exchange failed: HTTP {resp.status_code}")
    if "access_token" not in body:
        sys.exit(f"Token exchange failed: {body.get('error')} {body.get('error_description', '')}")

    UserTokenStore(token_file).save(UserTokenStore.from_response(body, device_id))
    print(f"Saved tokens to {token_file}; granted rights: {body.get('scope')}")
    if not body.get("refresh_token"):
        print("WARNING: no refresh token was issued, the bot will lose media uploads "
              "once the access token expires.")


if __name__ == "__main__":
    main()
