#!/usr/bin/python3
"""Bridge OpenHost's trusted owner identity into a native Stoat session."""

import json
import secrets
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from pymongo import MongoClient


API = "http://127.0.0.1:14702"
OWNER_EMAIL = "owner@openhost.internal"
OWNER_USERNAME = "OpenHostOwner"
database = MongoClient("mongodb://127.0.0.1:27017", connect=True)["revolt"]
seed_lock = threading.Lock()


def api_request(path, payload, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Session-Token"] = token
    request = urllib.request.Request(
        API + path,
        data=json.dumps(payload).encode(),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            body = response.read()
            return json.loads(body) if body else None
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace")
        raise RuntimeError(f"Stoat API {path} returned {error.code}: {detail}") from error


def owner_session():
    # Browsers may request the module more than once during a first load. Keep
    # account creation single-flight so two requests cannot race each other.
    with seed_lock:
        account = database.accounts.find_one({"email_normalised": OWNER_EMAIL})
        if account:
            if database.users.count_documents({"_id": account["_id"]}, limit=1) != 1:
                raise RuntimeError("owner account exists but onboarding is incomplete")
            session = database.sessions.find_one(
                {"user_id": account["_id"]}, sort=[("last_seen", -1)]
            )
            if not session:
                raise RuntimeError("owner account exists without a usable session")
            return session

        password = secrets.token_urlsafe(32)
        api_request("/auth/account/create", {"email": OWNER_EMAIL, "password": password})
        login = api_request(
            "/auth/session/login",
            {
                "email": OWNER_EMAIL,
                "password": password,
                "friendly_name": "OpenHost SSO",
            },
        )
        if not login or login.get("result") != "Success":
            raise RuntimeError("Stoat did not create the OpenHost owner session")

        api_request("/onboard/complete", {"username": OWNER_USERNAME}, login["token"])
        print("[owner-auth] seeded and onboarded the OpenHost owner", flush=True)
        return login


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/session":
            self.send_error(404)
            return
        if self.headers.get("X-OpenHost-Is-Owner") != "true":
            self.send_error(403)
            return
        try:
            session = owner_session()
            body = json.dumps(
                {
                    "_id": session["_id"],
                    "token": session["token"],
                    "userId": session["user_id"],
                    "valid": True,
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'none'")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as error:  # Keep failures visible to supervisor logs and callers.
            print(f"[owner-auth] session bootstrap failed: {error}", flush=True)
            self.send_error(503, "owner session unavailable")

    def log_message(self, message, *args):
        print(f"[owner-auth] {self.address_string()} {message % args}", flush=True)


if __name__ == "__main__":
    # January uses Stoat's fixed port 14705, so the OpenHost-only bridge owns
    # the next loopback port instead.
    ThreadingHTTPServer(("127.0.0.1", 14706), Handler).serve_forever()
