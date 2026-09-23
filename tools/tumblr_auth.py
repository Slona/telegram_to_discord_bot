#!/usr/bin/env python3
"""One-time Tumblr OAuth2 sign-in: writes the token file the bot uses.

1. Register an application at https://www.tumblr.com/oauth/apps and set its
   "OAuth2 redirect URLs" to TUMBLR_REDIRECT_URI (any URL you like, nothing has
   to listen there, e.g. https://localhost/callback).
2. Put TUMBLR_CLIENT_ID / TUMBLR_CLIENT_SECRET in .env and run this script.
3. Open the printed link, approve access, then paste the address the browser
   ends up on (the page itself may fail to load, that is fine).

The token file (TUMBLR_TOKEN_FILE, default tumblr_token.json) is a secret.
Tokens rotate: use the file on ONE machine only. To deploy, copy it to the
server and stop using the local copy.
"""

import os
import secrets
import sys
from urllib.parse import parse_qs, urlencode, urlparse

import requests
from dotenv import load_dotenv

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from targets.tumblr import API_BASE, TokenStore  # noqa: E402

load_dotenv()


def main():
    client_id = os.environ["TUMBLR_CLIENT_ID"]
    client_secret = os.environ["TUMBLR_CLIENT_SECRET"]
    redirect_uri = os.environ.get("TUMBLR_REDIRECT_URI", "https://localhost/callback")
    token_file = os.environ.get("TUMBLR_TOKEN_FILE", "tumblr_token.json")

    state = secrets.token_urlsafe(16)
    url = "https://www.tumblr.com/oauth2/authorize?" + urlencode({
        "client_id": client_id,
        "response_type": "code",
        "scope": "write offline_access",
        "state": state,
        "redirect_uri": redirect_uri,
    })
    print("Open this link and approve access:\n\n" + url + "\n")
    answer = input("Paste the address you were redirected to: ").strip()

    query = parse_qs(urlparse(answer).query)
    if query.get("state", [None])[0] != state:
        sys.exit("State mismatch: this is not the redirect for this sign-in attempt.")
    if "code" not in query:
        sys.exit(f"No code in the address (error: {query.get('error')}).")

    resp = requests.post(f"{API_BASE}/oauth2/token", data={
        "grant_type": "authorization_code",
        "code": query["code"][0],
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
    }, timeout=30)
    if resp.status_code != 200:
        sys.exit(f"Token request failed: {resp.status_code} {resp.text}")

    TokenStore(token_file).save(TokenStore.from_token_response(resp.json()))
    print(f"Saved tokens to {token_file}")


if __name__ == "__main__":
    main()
