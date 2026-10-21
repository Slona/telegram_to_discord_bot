#!/usr/bin/env python3
"""One-time Threads sign-in: writes the long-lived token file the bot uses.

Before the first run:
  1. Create an app at https://developers.facebook.com, add the Threads API use
     case with the permissions threads_basic and threads_content_publish.
  2. In the app's settings add a valid OAuth redirect URI, e.g. https://localhost/
     (nothing has to answer there), and note the Threads app id and secret
     (Threads API settings -> "Threads app ID" / "Threads app secret").
  3. App roles -> add the Threads profile you want to post to as a "Threads
     Tester" and accept the invitation on that profile (Threads: Settings ->
     Account -> Website permissions).
Then put THREADS_APP_ID, THREADS_APP_SECRET (and THREADS_REDIRECT_URI if it isn't
https://localhost/) in .env and run this script. Open the link signed in to that
Threads profile, approve, and paste the address the browser ends up on (the page
itself may fail to load, that's fine).

The token (valid 60 days, the bot renews it weekly) lets anyone post as that
profile: treat the token file as a secret and keep it on ONE machine.
"""

import os
import secrets
import sys
from urllib.parse import parse_qs, urlencode, urlparse

import requests
from dotenv import load_dotenv

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from targets.threads import API_VERSION, GRAPH_URL, TokenStore  # noqa: E402

load_dotenv()

SCOPE = "threads_basic,threads_content_publish"


def fail(what, resp):
    sys.exit(f"{what} failed: HTTP {resp.status_code} {resp.text[:500]}")


def main():
    app_id = os.environ["THREADS_APP_ID"]
    secret = os.environ["THREADS_APP_SECRET"]
    redirect_uri = os.environ.get("THREADS_REDIRECT_URI", "https://localhost/")
    token_file = os.environ.get("THREADS_TOKEN_FILE", "threads_token.json")
    graph = os.environ.get("THREADS_GRAPH_URL", GRAPH_URL).rstrip("/")
    authorize = os.environ.get("THREADS_AUTH_URL", "https://threads.com/oauth/authorize")

    state = secrets.token_urlsafe(16)
    url = authorize + "?" + urlencode({
        "client_id": app_id, "redirect_uri": redirect_uri, "scope": SCOPE,
        "response_type": "code", "state": state,
    })
    print("Open this link signed in to the Threads profile to post to, and approve:\n\n"
          + url + "\n")
    answer = input("Paste the address you were redirected to: ").strip()

    query = parse_qs(urlparse(answer).query)
    if query.get("state", [None])[0] != state:
        sys.exit("State mismatch: this is not the redirect for this sign-in attempt.")
    if "code" not in query:
        sys.exit(f"No code in the address (error: {query.get('error_description', query.get('error'))}).")
    code = query["code"][0].split("#")[0]          # Meta appends '#_' to the code

    resp = requests.post(f"{graph}/oauth/access_token", data={
        "client_id": app_id, "client_secret": secret, "grant_type": "authorization_code",
        "redirect_uri": redirect_uri, "code": code}, timeout=30)
    if resp.status_code != 200:
        fail("Exchanging the code", resp)
    short = resp.json()

    resp = requests.get(f"{graph}/access_token", params={
        "grant_type": "th_exchange_token", "client_secret": secret,
        "access_token": short["access_token"]}, timeout=30)
    if resp.status_code != 200:
        fail("Getting the long-lived token", resp)
    long_lived = resp.json()

    resp = requests.get(f"{graph}/{API_VERSION}/me", params={
        "fields": "id,username", "access_token": long_lived["access_token"]}, timeout=30)
    if resp.status_code != 200:
        fail("Reading the profile", resp)
    me = resp.json()

    TokenStore(token_file).save(TokenStore.make(
        long_lived["access_token"], me["id"], long_lived.get("expires_in", 5183944)))
    days = int(long_lived.get("expires_in", 5183944)) // 86400
    print(f"Saved the token for @{me.get('username')} (id {me['id']}) to {token_file}, "
          f"valid {days} days")


if __name__ == "__main__":
    main()
