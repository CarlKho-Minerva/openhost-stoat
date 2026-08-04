#!/usr/bin/env python3
"""Queue rich-preview generation for imported Discord transcript messages.

Run inside the unified OpenHost Stoat container after January is healthy. The
tool is a dry run unless ``--execute`` is supplied. It only touches messages
owned by the OpenHost owner and tagged by ``import_discord_messages.py``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pymongo import MongoClient

from import_stoatbridge import StoatApi, owner_credentials


ARCHIVE_MARKER = "<!-- discord-archive:"
URL_PATTERN = re.compile(r"https?://[^\s<>]+")


def log(message: str) -> None:
    print(f"[embed-backfill] {message}", flush=True)


def write_report(path: Path, report: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def january_healthcheck() -> None:
    with urllib.request.urlopen("http://127.0.0.1:14705/", timeout=10) as response:
        payload = json.loads(response.read())
    if not str(payload.get("january") or "").startswith("Hello"):
        raise RuntimeError("January returned an unexpected health response")


def collect_candidates(
    database: Any,
    owner_id: str,
    server_channels: dict[str, list[str]],
) -> tuple[list[dict[str, str]], dict[str, dict[str, int]]]:
    channel_to_server = {
        channel_id: server_id
        for server_id, channel_ids in server_channels.items()
        for channel_id in channel_ids
    }
    counts = {
        server_id: {"tagged": 0, "eligible": 0, "already_embedded": 0, "missing": 0}
        for server_id in server_channels
    }
    cursor = database.messages.find(
        {
            "author": owner_id,
            "channel": {"$in": list(channel_to_server)},
            "content": {"$regex": ARCHIVE_MARKER},
        },
        {"_id": 1, "channel": 1, "content": 1, "embeds": 1},
    )
    candidates: list[dict[str, str]] = []
    for message in cursor:
        channel_id = str(message.get("channel") or "")
        server_id = channel_to_server.get(channel_id)
        if not server_id:
            continue
        counts[server_id]["tagged"] += 1
        content = str(message.get("content") or "")
        if not URL_PATTERN.search(content):
            continue
        counts[server_id]["eligible"] += 1
        if message.get("embeds"):
            counts[server_id]["already_embedded"] += 1
            continue
        message_id = str(message.get("_id") or "")
        if not message_id:
            raise RuntimeError("tagged message is missing its id")
        counts[server_id]["missing"] += 1
        candidates.append(
            {
                "server_id": server_id,
                "channel_id": channel_id,
                "message_id": message_id,
                "content": content,
            }
        )
    return candidates, counts


def run(server_ids: list[str], execute: bool, verify_timeout: int) -> dict[str, Any]:
    if len(server_ids) != len(set(server_ids)):
        raise ValueError("duplicate --server-id values are not allowed")

    owner_id, token = owner_credentials()
    api = StoatApi(token)
    server_channels: dict[str, list[str]] = {}
    for server_id in server_ids:
        server = api.request("GET", f"/servers/{server_id}")
        if str(server.get("owner") or "") != owner_id:
            raise RuntimeError(f"server {server_id} is not owned by the OpenHost owner")
        server_channels[server_id] = [str(value) for value in server.get("channels") or []]

    database = MongoClient("mongodb://127.0.0.1:27017", connect=True)["revolt"]
    candidates, counts = collect_candidates(database, owner_id, server_channels)
    summary: dict[str, Any] = {
        "mode": "execute" if execute else "dry-run",
        "server_ids": server_ids,
        "servers": counts,
        "messages_to_queue": len(candidates),
    }
    log(json.dumps(summary, sort_keys=True))
    if not execute:
        return summary

    january_healthcheck()
    configuration = api.request("GET", "/")
    january = ((configuration.get("features") or {}).get("january") or {})
    if not january.get("enabled"):
        raise RuntimeError("Delta reports that January is disabled")

    archive_root = Path(os.environ.get("OPENHOST_APP_ARCHIVE_DIR", "/data/app_archive/stoat"))
    report_root = archive_root / "imports"
    report_root.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report_path = report_root / f"embed-backfill-{timestamp}-report.json"
    report: dict[str, Any] = {
        **summary,
        "mode": "running",
        "queued": 0,
        "failures": [],
    }
    write_report(report_path, report)

    for index, candidate in enumerate(candidates, 1):
        try:
            api.request(
                "PATCH",
                f"/channels/{candidate['channel_id']}/messages/{candidate['message_id']}",
                {"content": candidate["content"]},
            )
            report["queued"] += 1
            if index == 1 or index % 25 == 0 or index == len(candidates):
                log(f"queued {index}/{len(candidates)} messages")
        except Exception as error:
            report["failures"].append(
                {"message_id": candidate["message_id"], "error": str(error)}
            )
        write_report(report_path, report)

    target_ids = [candidate["message_id"] for candidate in candidates]
    deadline = time.monotonic() + verify_timeout
    embedded_after = 0
    while target_ids:
        embedded_after = database.messages.count_documents(
            {"_id": {"$in": target_ids}, "embeds.0": {"$exists": True}}
        )
        if embedded_after >= report["queued"] or time.monotonic() >= deadline:
            break
        time.sleep(2)

    report["messages_with_embeds_after"] = embedded_after
    report["messages_without_embeds_after"] = max(report["queued"] - embedded_after, 0)
    report["mode"] = "complete" if not report["failures"] else "partial"
    write_report(report_path, report)
    log(f"saved embed backfill report to {report_path}")
    log(
        json.dumps(
            {
                "mode": report["mode"],
                "queued": report["queued"],
                "messages_with_embeds_after": embedded_after,
                "messages_without_embeds_after": report["messages_without_embeds_after"],
                "failure_count": len(report["failures"]),
            },
            sort_keys=True,
        )
    )
    if report["failures"]:
        raise RuntimeError(
            f"embed backfill had {len(report['failures'])} API failure(s); see {report_path}"
        )
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server-id", action="append", required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--verify-timeout", type=int, default=120)
    arguments = parser.parse_args()
    if arguments.verify_timeout < 0:
        parser.error("--verify-timeout must be non-negative")
    return arguments


def main() -> int:
    arguments = parse_args()
    try:
        run(arguments.server_id, arguments.execute, arguments.verify_timeout)
    except Exception as error:
        print(f"[embed-backfill] ERROR: {error}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
