#!/usr/bin/python3
"""Substring message search, replacing Stoat's exact-word `$text` search.

Delta answers POST /channels/{id}/search with a MongoDB `$text` query. That
index only matches whole words and ORs multi-word queries, so `amaz` finds
nothing and `amazon us` returns every message containing "us". This bridge
sits in front of that one route and answers it with case-insensitive
substring matching where every term must be present.

Measured on this instance: a substring scan of all 26,599 messages takes
~30ms, so the strictness bought nothing.

Anything this cannot answer is proxied through to Delta unchanged, so a bug
here degrades search back to Stoat's behaviour rather than breaking it. Every
fallback is logged; a silent one would be worse than a loud failure.
"""

import json
import re
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from pymongo import MongoClient

API = "http://127.0.0.1:14702"
PORT = 14707

# Guard rails on caller-supplied input.
MAX_QUERY_CHARS = 256
MAX_TERMS = 8
DEFAULT_LIMIT = 50
MAX_LIMIT = 200

SEARCH_ROUTE = re.compile(r"^/channels/([0-9A-HJKMNP-TV-Z]{26})/search$")
# Quoted runs stay together as a phrase; everything else splits on whitespace.
TERM_PATTERN = re.compile(r'"([^"]+)"|(\S+)')

database = MongoClient("mongodb://127.0.0.1:27017", connect=True)["revolt"]


def log(message):
    print(f"[search-bridge] {message}", flush=True)


def parse_terms(query):
    """Split a query into terms, keeping "quoted phrases" intact."""
    terms = []
    for quoted, bare in TERM_PATTERN.findall(query[:MAX_QUERY_CHARS]):
        term = (quoted or bare).strip()
        if term:
            terms.append(term)
        if len(terms) == MAX_TERMS:
            break
    return terms


def may_read(channel_id, token):
    """Ask Delta whether this session can see the channel.

    Delegating keeps role and permission overrides authoritative instead of
    reimplementing them here. Note this checks channel visibility, not the
    separate ReadMessageHistory permission, so a role that can see a channel
    but not its history would still get results. That distinction does not
    exist on this instance; revisit before opening the server up.
    """
    request = urllib.request.Request(
        f"{API}/channels/{channel_id}",
        headers={"X-Session-Token": token},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status == 200
    except urllib.error.HTTPError as error:
        if error.code in (401, 403, 404):
            return False
        raise


def score(content, terms):
    """Rank by how early and how densely the terms land in the message."""
    lowered = content.lower()
    total = 0
    for term in terms:
        position = lowered.find(term.lower())
        if position == -1:
            continue
        total += 100 - min(position, 90)
    # Prefer the tighter match when two messages score the same otherwise.
    return total - len(content) / 1000


def run_search(channel_id, params):
    """Answer a search request straight from MongoDB."""
    query = (params.get("query") or "").strip()
    pinned = params.get("pinned")
    terms = parse_terms(query) if query else []

    # The pins sidebar reuses this route with no query at all.
    if not terms and not pinned:
        return {"messages": [], "users": [], "members": []}

    selector = {"channel": channel_id}
    if pinned is not None:
        selector["pinned"] = bool(pinned)
    if terms:
        selector["$and"] = [
            {"content": {"$regex": re.escape(term), "$options": "i"}}
            for term in terms
        ]

    limit = params.get("limit") or DEFAULT_LIMIT
    limit = max(1, min(int(limit), MAX_LIMIT))
    sort = params.get("sort") or "Latest"

    if sort == "Relevance" and terms:
        # Rank in Python: the candidate set is small and Mongo cannot express
        # this ordering. Pull a wider slice first so ranking has room to work.
        candidates = list(
            database.messages.find(selector).sort("_id", -1).limit(limit * 4)
        )
        candidates.sort(key=lambda m: score(m.get("content") or "", terms), reverse=True)
        messages = candidates[:limit]
    else:
        direction = 1 if sort == "Oldest" else -1
        messages = list(
            database.messages.find(selector).sort("_id", direction).limit(limit)
        )

    result = {"messages": messages}
    if params.get("include_users"):
        author_ids = {m["author"] for m in messages if m.get("author")}
        result["users"] = list(database.users.find({"_id": {"$in": list(author_ids)}}))

        channel = database.channels.find_one({"_id": channel_id}, {"server": 1})
        server_id = (channel or {}).get("server")
        result["members"] = (
            list(
                database.server_members.find(
                    {"_id.server": server_id, "_id.user": {"$in": list(author_ids)}}
                )
            )
            if server_id
            else []
        )

    return result


def proxy_to_delta(path, body, token):
    """Hand the request to Delta untouched."""
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Session-Token"] = token
    request = urllib.request.Request(API + path, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _respond(self, status, payload):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        token = self.headers.get("X-Session-Token")
        match = SEARCH_ROUTE.match(self.path)

        if not match:
            self._respond(404, {"type": "NotFound"})
            return
        if not token:
            self._respond(401, {"type": "Unauthenticated"})
            return

        channel_id = match.group(1)
        try:
            if not may_read(channel_id, token):
                self._respond(403, {"type": "MissingPermission"})
                return

            started = time.perf_counter()
            result = run_search(channel_id, json.loads(raw or b"{}"))
            elapsed = (time.perf_counter() - started) * 1000
            log(f"{channel_id} -> {len(result['messages'])} hits in {elapsed:.1f}ms")
            self._respond(200, result)
        except Exception as error:
            # Loud, then fall through to Delta so search still works.
            log(f"FAILED on {channel_id}: {type(error).__name__}: {error}")
            log("falling back to Delta's own search for this request")
            status, body = proxy_to_delta(self.path, raw, token)
            self._respond(status, body)

    def log_message(self, message, *args):
        pass  # Requests carry search queries; only log what run_search reports.


if __name__ == "__main__":
    log(f"listening on 127.0.0.1:{PORT}")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
