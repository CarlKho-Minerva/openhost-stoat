#!/usr/bin/env python3
"""Import Carl-authored Discord package messages as timestamped Stoat archives.

The compact archive JSON is read from stdin. Run inside the unified OpenHost
Stoat container alongside ``import_stoatbridge.py``. Other Discord authors are
never invented: Discord account exports contain only the requesting user data.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from import_stoatbridge import StoatApi, owner_credentials


MAX_CONTENT_BYTES = 1900
MAX_CHUNK_BODY_BYTES = 1750


def log(message: str) -> None:
    print(f"[discord-message-import] {message}", flush=True)


def normalise_messages(messages: list[dict[str, Any]]) -> list[dict[str, str]]:
    normalised: list[dict[str, str]] = []
    for message in messages:
        message_id = str(message.get("ID") or "").strip()
        timestamp = str(message.get("Timestamp") or "").strip()
        contents = str(message.get("Contents") or "")
        attachments = str(message.get("Attachments") or "").strip()
        if not message_id or not timestamp:
            raise ValueError("Discord message is missing ID or Timestamp")
        normalised.append(
            {
                "id": message_id,
                "timestamp": timestamp,
                "contents": contents,
                "attachments": attachments,
            }
        )
    normalised.sort(key=lambda item: (item["timestamp"], int(item["id"])))
    return normalised


def message_entry(message: dict[str, str]) -> str:
    lines = [
        f"**{message['timestamp']} UTC** · Discord message `{message['id']}`",
    ]
    if message["contents"]:
        lines.append(message["contents"])
    if message["attachments"]:
        lines.append(f"Attachment(s): {message['attachments']}")
    if not message["contents"] and not message["attachments"]:
        lines.append("_(empty message record)_")
    return "\n".join(lines)


def utf8_length(value: str) -> int:
    return len(value.encode("utf-8"))


def split_utf8(value: str, maximum_bytes: int) -> list[str]:
    pieces: list[str] = []
    current: list[str] = []
    current_bytes = 0
    for character in value:
        character_bytes = utf8_length(character)
        if current and current_bytes + character_bytes > maximum_bytes:
            pieces.append("".join(current))
            current = []
            current_bytes = 0
        current.append(character)
        current_bytes += character_bytes
    if current:
        pieces.append("".join(current))
    return pieces or [""]


def split_oversized_entry(entry: str) -> list[str]:
    if utf8_length(entry) <= MAX_CHUNK_BODY_BYTES - 200:
        return [entry]
    header, _, body = entry.partition("\n")
    piece_size = MAX_CHUNK_BODY_BYTES - utf8_length(header) - 80
    pieces = split_utf8(body, piece_size)
    return [f"{header} · part {index}/{len(pieces)}\n{piece}" for index, piece in enumerate(pieces, 1)]


def build_chunks(
    guild_id: str,
    source_channel_id: str,
    channel_name: str,
    messages: list[dict[str, str]],
) -> tuple[str, list[str]]:
    canonical = json.dumps(messages, sort_keys=True, separators=(",", ":")).encode()
    batch = hashlib.sha256(canonical).hexdigest()[:16]
    entries: list[str] = []
    for message in messages:
        entries.extend(split_oversized_entry(message_entry(message)))

    intro = (
        f"**Discord archive · #{channel_name}**\n"
        "These are only Carl-authored messages present in the Discord account export; "
        "other users' messages were not supplied. Original timestamps are UTC."
    )
    chunks: list[str] = []
    current = intro
    for entry in entries:
        candidate = f"{current}\n\n---\n\n{entry}"
        if utf8_length(candidate) <= MAX_CHUNK_BODY_BYTES:
            current = candidate
        else:
            chunks.append(current)
            current = entry
    if current:
        chunks.append(current)

    tagged: list[str] = []
    for index, chunk in enumerate(chunks, 1):
        tag = (
            f"<!-- discord-archive:{guild_id}:{source_channel_id}:{batch}:"
            f"{index}/{len(chunks)} -->"
        )
        tagged_chunk = f"{chunk}\n\n{tag}"
        if utf8_length(tagged_chunk) > MAX_CONTENT_BYTES:
            raise ValueError("internal error: tagged transcript chunk exceeds Stoat byte limit")
        tagged.append(tagged_chunk)
    return batch, tagged


def get_messages(api: StoatApi, channel_id: str) -> list[dict[str, Any]]:
    response = api.request("GET", f"/channels/{channel_id}/messages?limit=100")
    if isinstance(response, list):
        return response
    if isinstance(response, dict) and isinstance(response.get("messages"), list):
        return response["messages"]
    raise RuntimeError(f"unexpected message-list response for channel {channel_id}")


def write_report(path: Path, report: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def import_archive(
    archive: dict[str, Any],
    server_id: str,
    guild_id: str,
    structure_report_path: str,
    execute: bool,
) -> dict[str, Any]:
    if str(archive.get("guild_id")) != guild_id:
        raise ValueError("message archive belongs to a different Discord guild")
    channels = archive.get("channels")
    if not isinstance(channels, dict):
        raise ValueError("message archive channels must be an object")

    structure_report = json.loads(Path(structure_report_path).read_text())
    if structure_report.get("target_server_id") != server_id:
        raise ValueError("structure report belongs to a different Stoat server")
    if structure_report.get("guild_id") != guild_id:
        raise ValueError("structure report belongs to a different Discord guild")
    if structure_report.get("mode") != "complete" or structure_report.get("errors"):
        raise ValueError("structure import is not complete and error-free")
    channel_map = {
        str(key): str(value)
        for key, value in (structure_report.get("channel_map") or {}).items()
    }

    prepared: list[dict[str, Any]] = []
    total_source_messages = 0
    total_chunks = 0
    for source_channel_id, source_channel in channels.items():
        if source_channel_id not in channel_map:
            raise ValueError(f"no Stoat channel mapping for Discord channel {source_channel_id}")
        source_messages = normalise_messages(source_channel.get("messages") or [])
        if not source_messages:
            continue
        channel_name = str(source_channel.get("name") or source_channel_id)
        batch, chunks = build_chunks(guild_id, source_channel_id, channel_name, source_messages)
        prepared.append(
            {
                "source_channel_id": source_channel_id,
                "target_channel_id": channel_map[source_channel_id],
                "channel_name": channel_name,
                "batch": batch,
                "chunks": chunks,
                "source_message_count": len(source_messages),
            }
        )
        total_source_messages += len(source_messages)
        total_chunks += len(chunks) + 1  # Completion marker.

    summary = {
        "guild_id": guild_id,
        "target_server_id": server_id,
        "source_channel_count": len(prepared),
        "source_message_count": total_source_messages,
        "stoat_message_count": total_chunks,
        "mode": "execute" if execute else "dry-run",
    }
    log(json.dumps(summary, sort_keys=True))
    if not execute:
        return summary

    owner_id, token = owner_credentials()
    api = StoatApi(token)
    target = api.request("GET", f"/servers/{server_id}")
    if target.get("owner") != owner_id:
        raise RuntimeError("target server is not owned by the OpenHost owner")
    live_channels = {str(channel_id) for channel_id in target.get("channels") or []}
    missing_targets = sorted(
        {item["target_channel_id"] for item in prepared} - live_channels
    )
    if missing_targets:
        raise RuntimeError(f"mapped target channels are missing: {missing_targets}")

    archive_root = Path(os.environ.get("OPENHOST_APP_ARCHIVE_DIR", "/data/app_archive/stoat"))
    import_root = archive_root / "imports"
    import_root.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report_path = import_root / f"discord-messages-{guild_id}-{timestamp}-report.json"
    report: dict[str, Any] = {
        **summary,
        "mode": "running",
        "completed_channels": {},
        "errors": [],
    }
    write_report(report_path, report)

    for index, item in enumerate(prepared, 1):
        source_channel_id = item["source_channel_id"]
        target_channel_id = item["target_channel_id"]
        batch = item["batch"]
        completion_tag = f"discord-archive-complete:{guild_id}:{source_channel_id}:{batch}"
        existing = get_messages(api, target_channel_id)
        if any(completion_tag in str(message.get("content") or "") for message in existing):
            log(f"channel {index}/{len(prepared)} already complete: {item['channel_name']}")
            report["completed_channels"][source_channel_id] = {
                "target_channel_id": target_channel_id,
                "batch": batch,
                "source_message_count": item["source_message_count"],
                "status": "preexisting",
            }
            write_report(report_path, report)
            continue

        partial_tag = f"discord-archive:{guild_id}:{source_channel_id}:{batch}:"
        partial_messages = [
            message
            for message in existing
            if partial_tag in str(message.get("content") or "")
        ]
        for partial in partial_messages:
            partial_id = str(partial.get("_id") or partial.get("id") or "")
            if partial_id:
                api.request("DELETE", f"/channels/{target_channel_id}/messages/{partial_id}")
        if partial_messages:
            log(f"removed {len(partial_messages)} incomplete archive chunks from {item['channel_name']}")

        created_message_ids: list[str] = []
        try:
            for chunk_number, content in enumerate(item["chunks"], 1):
                created = api.request(
                    "POST", f"/channels/{target_channel_id}/messages", {"content": content}
                )
                created_id = str(created.get("_id") or created.get("id") or "")
                if not created_id:
                    raise RuntimeError("message creation returned no id")
                created_message_ids.append(created_id)
                log(
                    f"channel {index}/{len(prepared)} chunk {chunk_number}/{len(item['chunks'])}: "
                    f"{item['channel_name']}"
                )
            completion = (
                f"**Discord archive complete** · {item['source_message_count']} Carl-authored "
                f"source messages imported in {len(item['chunks'])} transcript chunks.\n\n"
                f"<!-- {completion_tag} -->"
            )
            created = api.request(
                "POST", f"/channels/{target_channel_id}/messages", {"content": completion}
            )
            completion_id = str(created.get("_id") or created.get("id") or "")
            if not completion_id:
                raise RuntimeError("completion message creation returned no id")
            created_message_ids.append(completion_id)
            report["completed_channels"][source_channel_id] = {
                "target_channel_id": target_channel_id,
                "batch": batch,
                "source_message_count": item["source_message_count"],
                "stoat_message_count": len(created_message_ids),
                "status": "complete",
            }
            write_report(report_path, report)
        except Exception as error:
            rollback_errors: list[str] = []
            for created_id in reversed(created_message_ids):
                try:
                    api.request(
                        "DELETE", f"/channels/{target_channel_id}/messages/{created_id}"
                    )
                except Exception as rollback_error:
                    rollback_errors.append(str(rollback_error))
            detail = f"{item['channel_name']}: {error}"
            if rollback_errors:
                detail += f"; rollback errors: {rollback_errors}"
            report["errors"].append(detail)
            write_report(report_path, report)

    completed_source_messages = sum(
        int(item["source_message_count"])
        for item in report["completed_channels"].values()
    )
    report["completed_source_message_count"] = completed_source_messages
    report["completed_channel_count"] = len(report["completed_channels"])
    report["mode"] = "complete" if not report["errors"] else "partial"
    write_report(report_path, report)
    log(f"saved message import report to {report_path}")
    log(
        json.dumps(
            {key: value for key, value in report.items() if key != "completed_channels"},
            sort_keys=True,
        )
    )
    if report["errors"]:
        raise RuntimeError(
            f"message import completed with {len(report['errors'])} error(s); see {report_path}"
        )
    if completed_source_messages != total_source_messages:
        raise RuntimeError(
            f"expected {total_source_messages} source messages, completed {completed_source_messages}"
        )
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server-id", required=True)
    parser.add_argument("--guild-id", required=True)
    parser.add_argument("--structure-report", required=True)
    parser.add_argument("--execute", action="store_true")
    return parser.parse_args()


def main() -> int:
    arguments = parse_args()
    try:
        archive = json.load(sys.stdin)
        import_archive(
            archive,
            arguments.server_id,
            arguments.guild_id,
            arguments.structure_report,
            arguments.execute,
        )
    except Exception as error:
        print(f"[discord-message-import] ERROR: {error}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
