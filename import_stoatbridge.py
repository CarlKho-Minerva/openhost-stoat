#!/usr/bin/env python3
"""Import a StoatBridge Discord scan into an empty self-hosted Stoat server.

Run this inside the unified OpenHost Stoat container. The importer obtains the
passwordless OpenHost owner's existing session from the local MongoDB instance;
it never accepts or prints a session token.
"""

from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import json
import mimetypes
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pymongo import MongoClient


API = "http://127.0.0.1:14702"
AUTUMN = "http://127.0.0.1:14704"
OWNER_EMAIL = "owner@openhost.internal"
DISCORD_CDN_HOSTS = {"cdn.discordapp.com", "media.discordapp.net"}
SUPPORTED_CHANNEL_TYPES = {"text": "Text", "announcement": "Text", "voice": "Voice"}
ULID_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def log(message: str) -> None:
    print(f"[stoatbridge-import] {message}", flush=True)


def load_scan(path: str) -> tuple[dict[str, Any], bytes]:
    encoded = os.environ.get("STOATBRIDGE_SCAN_B64")
    if encoded:
        raw = base64.b64decode(encoded, validate=True)
    elif path == "-":
        raw = sys.stdin.buffer.read()
    else:
        raw = Path(path).read_bytes()
    scan = json.loads(raw)
    if not isinstance(scan, dict):
        raise ValueError("scan root must be an object")
    return scan, raw


def load_baseline_scan(path: str | None) -> tuple[dict[str, Any], bytes]:
    encoded = os.environ.get("STOATBRIDGE_BASELINE_SCAN_B64")
    if encoded:
        raw = base64.b64decode(encoded, validate=True)
    elif path:
        raw = Path(path).read_bytes()
    else:
        raise ValueError("scan extension requires --baseline-scan or STOATBRIDGE_BASELINE_SCAN_B64")
    baseline = json.loads(raw)
    if not isinstance(baseline, dict):
        raise ValueError("baseline scan root must be an object")
    return baseline, raw


def owner_credentials() -> tuple[str, str]:
    database = MongoClient("mongodb://127.0.0.1:27017", connect=True)["revolt"]
    account = database.accounts.find_one({"email_normalised": OWNER_EMAIL})
    if not account:
        raise RuntimeError("OpenHost owner account does not exist")
    session = database.sessions.find_one(
        {"user_id": account["_id"]}, sort=[("last_seen", -1)]
    )
    if not session or not session.get("token"):
        raise RuntimeError("OpenHost owner has no usable Stoat session")
    return str(account["_id"]), str(session["token"])


class StoatApi:
    def __init__(self, token: str) -> None:
        self._token = token
        self._last_mutation = 0.0

    def _pace_mutation(self) -> None:
        # Delta's self-hosted limiter allows only a small burst. Sustained
        # imports are reliable at roughly one mutation every three seconds.
        remaining = 3.1 - (time.monotonic() - self._last_mutation)
        if remaining > 0:
            time.sleep(remaining)
        self._last_mutation = time.monotonic()

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        body: bytes | None = None,
        content_type: str = "application/json",
        retries: int = 4,
    ) -> Any:
        if payload is not None and body is not None:
            raise ValueError("payload and body are mutually exclusive")
        data = body
        if payload is not None:
            data = json.dumps(payload, separators=(",", ":")).encode()
        headers = {"X-Session-Token": self._token}
        if data is not None:
            headers["Content-Type"] = content_type

        for attempt in range(retries + 1):
            if method != "GET":
                self._pace_mutation()
            request = urllib.request.Request(
                API + path, data=data, headers=headers, method=method
            )
            try:
                with urllib.request.urlopen(request, timeout=45) as response:
                    response_body = response.read()
                    if not response_body:
                        return None
                    return json.loads(response_body)
            except urllib.error.HTTPError as error:
                detail = error.read().decode(errors="replace")
                if error.code in {429, 502, 503} and attempt < retries:
                    wait = min(2 ** attempt, 12)
                    log(f"{error.code} from {method} {path}; retrying in {wait}s")
                    time.sleep(wait)
                    continue
                raise RuntimeError(
                    f"Stoat API {method} {path} returned {error.code}: {detail}"
                ) from error
            except urllib.error.URLError as error:
                if attempt < retries:
                    wait = min(2 ** attempt, 12)
                    log(f"network error on {method} {path}; retrying in {wait}s")
                    time.sleep(wait)
                    continue
                raise RuntimeError(f"Stoat API {method} {path} failed: {error}") from error
        raise AssertionError("unreachable")

    def upload(self, url: str, tag: str) -> str:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "https" or parsed.hostname not in DISCORD_CDN_HOSTS:
            raise ValueError(f"refusing non-Discord CDN URL: {url}")
        download_request = urllib.request.Request(
            url,
            headers={
                "Accept": "image/avif,image/webp,image/png,image/gif,image/*,*/*;q=0.8",
                "User-Agent": "Mozilla/5.0 (compatible; OpenHost-StoatBridge/1.0)",
            },
        )
        with urllib.request.urlopen(download_request, timeout=45) as response:
            content_length = int(response.headers.get("Content-Length", "0") or "0")
            if content_length > 10 * 1024 * 1024:
                raise ValueError(f"asset exceeds 10 MiB: {url}")
            asset = response.read(10 * 1024 * 1024 + 1)
            mime_type = response.headers.get_content_type()
        if len(asset) > 10 * 1024 * 1024:
            raise ValueError(f"asset exceeds 10 MiB: {url}")

        boundary = f"stoatbridge-{random.getrandbits(96):024x}"
        filename = Path(parsed.path).name or "upload"
        multipart = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            f"Content-Type: {mime_type or mimetypes.guess_type(filename)[0] or 'application/octet-stream'}\r\n"
            "\r\n"
        ).encode() + asset + f"\r\n--{boundary}--\r\n".encode()
        request = urllib.request.Request(
            f"{AUTUMN}/{tag}",
            data=multipart,
            headers={
                "X-Session-Token": self._token,
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                result = json.loads(response.read())
        except urllib.error.HTTPError as error:
            detail = error.read().decode(errors="replace")
            raise RuntimeError(
                f"Autumn upload /{tag} returned {error.code}: {detail}"
            ) from error
        asset_id = result.get("id")
        if not asset_id:
            raise RuntimeError(f"Autumn upload /{tag} returned no id")
        return str(asset_id)


def make_ulid() -> str:
    value = (int(time.time() * 1000) << 80) | random.getrandbits(80)
    characters: list[str] = []
    for _ in range(26):
        characters.append(ULID_ALPHABET[value & 31])
        value >>= 5
    return "".join(reversed(characters))


def map_permissions(discord_permissions: str | int) -> int:
    discord = int(discord_permissions or 0)
    known_bits = [0, 1, 2, 3, 6, 7, 8, 10, 11, 20, 21, 22, 23, 24, 25, 26, 27, 29, 30, 31]
    if discord & (1 << 3):
        return sum(1 << bit for bit in known_bits)
    mapping = [
        (0, 25), (1, 6), (2, 7), (4, 0), (5, 1), (6, 29),
        (10, 20), (11, 22), (13, 23), (14, 26), (15, 27),
        (16, 21), (20, 30), (21, 31), (26, 10), (27, 11),
        (28, 2), (28, 3), (29, 24), (40, 8),
    ]
    return sum(1 << stoat_bit for discord_bit, stoat_bit in mapping if discord & (1 << discord_bit))


def normalise_emoji_name(name: str, used: set[str]) -> str:
    base = re.sub(r"[^a-z0-9_]", "", re.sub(r"[-\s]+", "_", name.lower()))[:32]
    base = base or "emoji"
    candidate = base
    suffix = 2
    while candidate in used:
        tail = f"_{suffix}"
        candidate = base[: 32 - len(tail)] + tail
        suffix += 1
    used.add(candidate)
    return candidate


def channel_fields(channel: dict[str, Any]) -> tuple[str, str | None]:
    original_name = str(channel["name"]).strip()
    channel_name = original_name if len(original_name) <= 32 else original_name[:31] + "…"
    description = str(channel.get("topic") or "").strip()
    if channel_name != original_name:
        provenance = f"Imported from Discord channel “{original_name}”."
        description = f"{provenance}\n\n{description}" if description else provenance
    return channel_name, description[:1024] or None


def validate_scan(scan: dict[str, Any], expected_guild_id: str | None) -> list[dict[str, Any]]:
    guild = scan.get("guild") or {}
    if expected_guild_id and str(guild.get("id")) != expected_guild_id:
        raise ValueError(
            f"scan guild is {guild.get('id')!r}, expected {expected_guild_id!r}"
        )
    if not guild.get("name") or not guild.get("id"):
        raise ValueError("scan is missing guild id or name")
    if not isinstance(scan.get("categories"), list) or not isinstance(scan.get("roles"), list):
        raise ValueError("scan is missing categories or roles")

    channels: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for category in scan["categories"]:
        if not isinstance(category.get("channels"), list):
            raise ValueError(f"category {category.get('name')!r} has invalid channels")
        for channel in category["channels"]:
            if channel.get("typeName") not in SUPPORTED_CHANNEL_TYPES:
                continue
            channel_id = str(channel.get("id") or "")
            if not channel_id or channel_id in seen_ids:
                raise ValueError(f"channel has missing or duplicate id: {channel_id!r}")
            if not channel.get("name"):
                raise ValueError(f"channel {channel_id} has no name")
            seen_ids.add(channel_id)
            channels.append(channel)
    if not channels:
        raise ValueError("scan contains no supported channels")
    return channels


def import_scan(
    scan: dict[str, Any],
    raw_scan: bytes,
    server_id: str,
    expected_guild_id: str | None,
    execute: bool,
) -> dict[str, Any]:
    supported_channels = validate_scan(scan, expected_guild_id)
    owner_id, token = owner_credentials()
    api = StoatApi(token)
    target = api.request("GET", f"/servers/{server_id}")
    if target.get("owner") != owner_id:
        raise RuntimeError("target server is not owned by the OpenHost owner")
    if target.get("name") != scan["guild"]["name"]:
        raise RuntimeError(
            f"target server name {target.get('name')!r} does not match scan guild {scan['guild']['name']!r}"
        )

    existing_channels = list(target.get("channels") or [])
    existing_categories = list(target.get("categories") or [])
    existing_roles = dict(target.get("roles") or {})
    if len(existing_channels) > 1 or existing_categories or existing_roles:
        raise RuntimeError(
            "target is not an empty starter server; refusing to merge or replace live content"
        )

    roles_to_create = [
        role
        for role in scan["roles"]
        if not role.get("managed") and not role.get("isDefault")
    ]
    summary = {
        "guild": scan["guild"]["name"],
        "guild_id": str(scan["guild"]["id"]),
        "target_server_id": server_id,
        "scan_sha256": hashlib.sha256(raw_scan).hexdigest(),
        "channels": len(supported_channels),
        "categories": len(scan["categories"]),
        "roles": len(roles_to_create),
        "emojis": len(scan.get("emojis") or []),
        "mode": "execute" if execute else "dry-run",
    }
    log(json.dumps(summary, sort_keys=True))
    if not execute:
        return summary

    archive_root = Path(os.environ.get("OPENHOST_APP_ARCHIVE_DIR", "/data/app_archive/stoat"))
    import_root = archive_root / "imports"
    import_root.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = import_root / f"stoatbridge-{scan['guild']['id']}-{timestamp}-before.json"
    backup_path.write_text(json.dumps(target, indent=2, sort_keys=True) + "\n")
    log(f"saved pre-import metadata to {backup_path}")

    errors: list[str] = []
    role_map: dict[str, str] = {}
    channel_map: dict[str, str] = {}

    for starter_channel_id in existing_channels:
        api.request("DELETE", f"/channels/{starter_channel_id}")
        log(f"removed empty starter channel {starter_channel_id}")

    icon = scan["guild"].get("icon")
    if icon:
        icon_url = f"https://cdn.discordapp.com/icons/{scan['guild']['id']}/{icon}.png?size=512"
        try:
            icon_id = api.upload(icon_url, "icons")
            api.request("PATCH", f"/servers/{server_id}", {"icon": icon_id})
            log("transferred guild icon")
        except Exception as error:  # Non-structural asset; report and continue.
            errors.append(f"icon: {error}")

    for index, role in enumerate(roles_to_create, start=1):
        role_name = str(role["name"]).strip()[:100]
        try:
            created = api.request(
                "POST",
                f"/servers/{server_id}/roles",
                {"name": role_name, "rank": int(role.get("position") or 0)},
            )
            created_id = str(created.get("id") or created.get("_id") or "")
            if not created_id:
                raise RuntimeError("role creation returned no id")
            role_map[str(role["id"])] = created_id
            patch: dict[str, Any] = {"hoist": bool(role.get("hoist"))}
            if int(role.get("color") or 0):
                patch["colour"] = f"#{int(role['color']):06x}"
            api.request("PATCH", f"/servers/{server_id}/roles/{created_id}", patch)
            api.request(
                "PUT",
                f"/servers/{server_id}/permissions/{created_id}",
                {"permissions": {"allow": map_permissions(role.get("permissions", 0)), "deny": 0}},
            )
            log(f"role {index}/{len(roles_to_create)}: {role_name}")
        except Exception as error:
            errors.append(f"role {role_name}: {error}")
        time.sleep(0.15)

    category_records: list[dict[str, Any]] = []
    channel_number = 0
    total_channels = len(supported_channels)
    for category in scan["categories"]:
        category_channel_ids: list[str] = []
        for channel in category["channels"]:
            channel_type = SUPPORTED_CHANNEL_TYPES.get(channel.get("typeName"))
            if not channel_type:
                continue
            channel_number += 1
            channel_name, description = channel_fields(channel)
            payload: dict[str, Any] = {
                "name": channel_name,
                "type": channel_type,
                "nsfw": bool(channel.get("nsfw")),
            }
            if description:
                payload["description"] = description
            try:
                created = api.request("POST", f"/servers/{server_id}/channels", payload)
                created_id = str(created.get("_id") or created.get("id") or "")
                if not created_id:
                    raise RuntimeError("channel creation returned no id")
                channel_map[str(channel["id"])] = created_id
                category_channel_ids.append(created_id)
                log(f"channel {channel_number}/{total_channels}: {channel_name}")
            except Exception as error:
                errors.append(f"channel {channel_name}: {error}")
            time.sleep(0.15)
        category_records.append(
            {
                "id": make_ulid(),
                "title": str(category["name"]).strip()[:100],
                "channels": category_channel_ids,
            }
        )

    api.request("PATCH", f"/servers/{server_id}", {"categories": category_records})
    log(f"applied {len(category_records)} categories")

    guild_id = str(scan["guild"]["id"])
    permission_count = 0
    for category in scan["categories"]:
        for channel in category["channels"]:
            stoat_channel_id = channel_map.get(str(channel.get("id")))
            if not stoat_channel_id:
                continue
            for overwrite in channel.get("permission_overwrites") or []:
                role_id = str(overwrite.get("id") or "")
                stoat_role_id = "default" if role_id == guild_id else role_map.get(role_id)
                if not stoat_role_id:
                    continue
                allow = map_permissions(overwrite.get("allow", 0))
                deny = map_permissions(overwrite.get("deny", 0))
                if deny & (1 << 20):
                    deny |= 1 << 22
                if not allow and not deny:
                    continue
                try:
                    api.request(
                        "PUT",
                        f"/channels/{stoat_channel_id}/permissions/{stoat_role_id}",
                        {"permissions": {"allow": allow, "deny": deny}},
                    )
                    permission_count += 1
                except Exception as error:
                    errors.append(f"permissions {channel['name']}/{stoat_role_id}: {error}")
                time.sleep(0.1)
    log(f"applied {permission_count} channel permission overrides")

    used_emoji_names: set[str] = set()
    emoji_map: dict[str, str] = {}
    emoji_count = 0
    for emoji in scan.get("emojis") or []:
        emoji_name = normalise_emoji_name(str(emoji.get("name") or "emoji"), used_emoji_names)
        try:
            emoji_id = api.upload(str(emoji["url"]), "emojis")
            api.request(
                "PUT",
                f"/custom/emoji/{emoji_id}",
                {"name": emoji_name, "parent": {"type": "Server", "id": server_id}},
            )
            emoji_map[str(emoji.get("id") or emoji_name)] = emoji_id
            emoji_count += 1
            log(f"emoji {emoji_count}/{len(scan.get('emojis') or [])}: {emoji_name}")
        except Exception as error:
            errors.append(f"emoji {emoji_name}: {error}")
        time.sleep(0.2)

    final = api.request("GET", f"/servers/{server_id}")
    report = {
        **summary,
        "mode": "complete" if not errors else "partial",
        "created_channel_count": len(channel_map),
        "created_role_count": len(role_map),
        "created_emoji_count": emoji_count,
        "applied_permission_count": permission_count,
        "final_channel_count": len(final.get("channels") or []),
        "final_category_count": len(final.get("categories") or []),
        "role_map": role_map,
        "channel_map": channel_map,
        "emoji_map": emoji_map,
        "errors": errors,
    }
    report_path = import_root / f"stoatbridge-{scan['guild']['id']}-{timestamp}-report.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    log(f"saved import report to {report_path}")
    log(json.dumps({key: value for key, value in report.items() if key not in {"role_map", "channel_map"}}, sort_keys=True))
    if errors:
        raise RuntimeError(f"import completed with {len(errors)} error(s); see {report_path}")
    if report["final_channel_count"] != total_channels:
        raise RuntimeError(
            f"expected {total_channels} final channels, found {report['final_channel_count']}"
        )
    return report


def repair_scan(
    scan: dict[str, Any],
    raw_scan: bytes,
    server_id: str,
    expected_guild_id: str | None,
    previous_report_path: str,
    allow_scan_extension: bool = False,
    baseline_scan_path: str | None = None,
) -> dict[str, Any]:
    supported_channels = validate_scan(scan, expected_guild_id)
    owner_id, token = owner_credentials()
    api = StoatApi(token)
    target = api.request("GET", f"/servers/{server_id}")
    if target.get("owner") != owner_id:
        raise RuntimeError("target server is not owned by the OpenHost owner")
    if target.get("name") != scan["guild"]["name"]:
        raise RuntimeError("target server name does not match the scan guild")

    previous_report = json.loads(Path(previous_report_path).read_text())
    scan_hash = hashlib.sha256(raw_scan).hexdigest()
    if previous_report.get("target_server_id") != server_id:
        raise RuntimeError("repair report belongs to a different Stoat server")
    if previous_report.get("guild_id") != str(scan["guild"]["id"]):
        raise RuntimeError("repair report belongs to a different Discord guild")
    role_map = {str(key): str(value) for key, value in (previous_report.get("role_map") or {}).items()}
    channel_map = {str(key): str(value) for key, value in (previous_report.get("channel_map") or {}).items()}
    emoji_map = {str(key): str(value) for key, value in (previous_report.get("emoji_map") or {}).items()}
    existing_categories = list(target.get("categories") or [])
    extension_source_channels: set[str] = set()
    scan_changed = previous_report.get("scan_sha256") != scan_hash
    if scan_changed and not allow_scan_extension:
        raise RuntimeError("repair report was generated from a different scan")
    if scan_changed:
        if previous_report.get("mode") != "complete" or previous_report.get("errors"):
            raise RuntimeError("only a complete, error-free import can be extended")
        baseline_scan, baseline_raw = load_baseline_scan(baseline_scan_path)
        if hashlib.sha256(baseline_raw).hexdigest() != previous_report.get("scan_sha256"):
            raise RuntimeError("baseline scan hash does not match the completed import report")
        added_channel_count = len(supported_channels) - int(
            baseline_scan.get("summary", {}).get("totalChannels", 0)
        )
        expected_summary = copy.deepcopy(baseline_scan.get("summary"))
        expected_summary["totalChannels"] = int(expected_summary["totalChannels"]) + added_channel_count
        expected_summary["totalCategories"] = int(expected_summary["totalCategories"]) + 1
        if scan.get("summary") != expected_summary:
            raise RuntimeError("extended scan summary changed fields beyond the added category/channels")
        proposed_baseline = copy.deepcopy(scan)
        proposed_baseline["categories"] = proposed_baseline.get("categories", [])[:-1]
        proposed_baseline["summary"] = copy.deepcopy(baseline_scan.get("summary"))
        if proposed_baseline != baseline_scan:
            raise RuntimeError("extended scan changed fields from the immutable baseline scan")
        source_channel_ids = {str(channel["id"]) for channel in supported_channels}
        removed_source_channels = sorted(set(channel_map) - source_channel_ids)
        if removed_source_channels:
            raise RuntimeError(
                f"extended scan removed previously imported channels: {removed_source_channels}"
            )
        extension_source_channels = source_channel_ids - set(channel_map)
        if not extension_source_channels:
            raise RuntimeError("extended scan must add at least one channel")
        if len(scan["categories"]) != len(existing_categories) + 1:
            raise RuntimeError("extended scan must add exactly one category")
        for index, existing_category in enumerate(existing_categories):
            proposed_category = scan["categories"][index]
            proposed_title = str(proposed_category["name"]).strip()[:100]
            if proposed_title != str(existing_category.get("title") or ""):
                raise RuntimeError(
                    f"extended scan changed existing category {index}: {proposed_title!r}"
                )
            proposed_source_ids = [
                str(channel["id"])
                for channel in proposed_category["channels"]
                if channel.get("typeName") in SUPPORTED_CHANNEL_TYPES
            ]
            if any(source_id not in channel_map for source_id in proposed_source_ids):
                raise RuntimeError("extended scan inserted a new channel into an existing category")
            proposed_target_ids = [channel_map[source_id] for source_id in proposed_source_ids]
            if proposed_target_ids != [str(value) for value in existing_category.get("channels") or []]:
                raise RuntimeError("extended scan reordered or moved existing channels")
        legacy_category = scan["categories"][-1]
        if str(legacy_category.get("name") or "").strip() != "Discord Legacy Archive":
            raise RuntimeError("extended scan category must be named 'Discord Legacy Archive'")
        legacy_source_ids = {
            str(channel["id"])
            for channel in legacy_category.get("channels") or []
            if channel.get("typeName") in SUPPORTED_CHANNEL_TYPES
        }
        if legacy_source_ids != extension_source_channels:
            raise RuntimeError("all and only new channels must be in Discord Legacy Archive")
        scan_role_ids = {
            str(role["id"])
            for role in scan["roles"]
            if not role.get("managed") and not role.get("isDefault")
        }
        if scan_role_ids != set(role_map):
            raise RuntimeError("extended scan changed transferable roles")
        scan_emoji_ids = {
            str(emoji.get("id") or emoji.get("name") or "emoji")
            for emoji in scan.get("emojis") or []
        }
        if scan_emoji_ids != set(emoji_map):
            raise RuntimeError("extended scan changed transferable emojis")
    live_channels = {str(channel_id) for channel_id in target.get("channels") or []}
    stale_channels = sorted(set(channel_map.values()) - live_channels)
    if stale_channels:
        raise RuntimeError(f"repair report references deleted channels: {stale_channels}")

    live_roles = {str(role_id) for role_id in (target.get("roles") or {}).keys()}
    stale_roles = sorted(set(role_map.values()) - live_roles)
    if stale_roles:
        raise RuntimeError(f"repair report references deleted roles: {stale_roles}")

    errors: list[str] = []
    icon = scan["guild"].get("icon")
    if icon and not target.get("icon") and not scan_changed:
        icon_url = f"https://cdn.discordapp.com/icons/{scan['guild']['id']}/{icon}.png?size=512"
        try:
            icon_id = api.upload(icon_url, "icons")
            api.request("PATCH", f"/servers/{server_id}", {"icon": icon_id})
            log("repair transferred guild icon")
        except Exception as error:
            errors.append(f"icon: {error}")

    total_channels = len(supported_channels)
    for channel in supported_channels:
        discord_channel_id = str(channel["id"])
        if discord_channel_id in channel_map:
            continue
        channel_name, description = channel_fields(channel)
        payload: dict[str, Any] = {
            "name": channel_name,
            "type": SUPPORTED_CHANNEL_TYPES[channel["typeName"]],
            "nsfw": bool(channel.get("nsfw")),
        }
        if description:
            payload["description"] = description
        try:
            created = api.request("POST", f"/servers/{server_id}/channels", payload)
            created_id = str(created.get("_id") or created.get("id") or "")
            if not created_id:
                raise RuntimeError("channel creation returned no id")
            channel_map[discord_channel_id] = created_id
            log(f"repair channel {len(channel_map)}/{total_channels}: {channel_name}")
        except Exception as error:
            errors.append(f"channel {channel['name']}: {error}")

    if len(existing_categories) != len(scan["categories"]) and not (
        allow_scan_extension and len(existing_categories) < len(scan["categories"])
    ):
        raise RuntimeError(
            f"target has {len(existing_categories)} categories; expected {len(scan['categories'])}"
        )
    category_records: list[dict[str, Any]] = []
    for index, category in enumerate(scan["categories"]):
        existing = existing_categories[index] if index < len(existing_categories) else {}
        if scan_changed and existing:
            category_records.append(copy.deepcopy(existing))
            continue
        source_channel_ids = [
            str(channel["id"])
            for channel in category["channels"]
            if channel.get("typeName") in SUPPORTED_CHANNEL_TYPES
        ]
        category_records.append(
            {
                "id": str(existing.get("id") or make_ulid()),
                "title": str(category["name"]).strip()[:100],
                "channels": [
                    channel_map[source_id]
                    for source_id in source_channel_ids
                    if source_id in channel_map
                ],
            }
        )
    api.request("PATCH", f"/servers/{server_id}", {"categories": category_records})
    log(f"repair reconciled {len(category_records)} categories")

    guild_id = str(scan["guild"]["id"])
    permission_count = 0
    for category in scan["categories"]:
        for channel in category["channels"]:
            source_channel_id = str(channel.get("id"))
            if scan_changed and source_channel_id not in extension_source_channels:
                continue
            stoat_channel_id = channel_map.get(source_channel_id)
            if not stoat_channel_id:
                continue
            for overwrite in channel.get("permission_overwrites") or []:
                source_role_id = str(overwrite.get("id") or "")
                stoat_role_id = "default" if source_role_id == guild_id else role_map.get(source_role_id)
                if not stoat_role_id:
                    continue
                allow = map_permissions(overwrite.get("allow", 0))
                deny = map_permissions(overwrite.get("deny", 0))
                if deny & (1 << 20):
                    deny |= 1 << 22
                if not allow and not deny:
                    continue
                try:
                    api.request(
                        "PUT",
                        f"/channels/{stoat_channel_id}/permissions/{stoat_role_id}",
                        {"permissions": {"allow": allow, "deny": deny}},
                    )
                    permission_count += 1
                except Exception as error:
                    errors.append(f"permissions {channel['name']}/{stoat_role_id}: {error}")
    log(f"repair reconciled {permission_count} channel permission overrides")

    used_emoji_names: set[str] = set()
    for emoji in scan.get("emojis") or []:
        emoji_key = str(emoji.get("id") or emoji.get("name") or "emoji")
        emoji_name = normalise_emoji_name(str(emoji.get("name") or "emoji"), used_emoji_names)
        if emoji_key in emoji_map:
            continue
        try:
            emoji_id = api.upload(str(emoji["url"]), "emojis")
            api.request(
                "PUT",
                f"/custom/emoji/{emoji_id}",
                {"name": emoji_name, "parent": {"type": "Server", "id": server_id}},
            )
            emoji_map[emoji_key] = emoji_id
            log(f"repair emoji {len(emoji_map)}/{len(scan.get('emojis') or [])}: {emoji_name}")
        except Exception as error:
            errors.append(f"emoji {emoji_name}: {error}")

    final = api.request("GET", f"/servers/{server_id}")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive_root = Path(os.environ.get("OPENHOST_APP_ARCHIVE_DIR", "/data/app_archive/stoat"))
    import_root = archive_root / "imports"
    import_root.mkdir(parents=True, exist_ok=True)
    report = {
        "guild": scan["guild"]["name"],
        "guild_id": guild_id,
        "target_server_id": server_id,
        "scan_sha256": scan_hash,
        "mode": "complete" if not errors else "partial",
        "scan_extension": scan_changed,
        "channels": total_channels,
        "categories": len(scan["categories"]),
        "roles": len([role for role in scan["roles"] if not role.get("managed") and not role.get("isDefault")]),
        "emojis": len(scan.get("emojis") or []),
        "created_channel_count": len(channel_map),
        "created_role_count": len(role_map),
        "created_emoji_count": len(emoji_map),
        "applied_permission_count": permission_count,
        "final_channel_count": len(final.get("channels") or []),
        "final_category_count": len(final.get("categories") or []),
        "role_map": role_map,
        "channel_map": channel_map,
        "emoji_map": emoji_map,
        "previous_errors": previous_report.get("errors") or [],
        "errors": errors,
    }
    report_path = import_root / f"stoatbridge-{guild_id}-{timestamp}-repair-report.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    log(f"saved repair report to {report_path}")
    log(json.dumps({key: value for key, value in report.items() if key not in {"role_map", "channel_map", "emoji_map", "previous_errors"}}, sort_keys=True))
    if errors:
        raise RuntimeError(f"repair completed with {len(errors)} error(s); see {report_path}")
    if report["final_channel_count"] != total_channels:
        raise RuntimeError(
            f"expected {total_channels} final channels, found {report['final_channel_count']}"
        )
    if report["created_emoji_count"] != report["emojis"]:
        raise RuntimeError(
            f"expected {report['emojis']} emojis, imported {report['created_emoji_count']}"
        )
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scan", default="-", help="scan JSON file, or - for stdin")
    parser.add_argument("--server-id", required=True, help="existing empty Stoat server id")
    parser.add_argument("--expected-guild-id", help="refuse a scan for any other Discord guild")
    parser.add_argument(
        "--repair-report",
        help="repair a partial import using its archived report instead of requiring an empty target",
    )
    parser.add_argument(
        "--allow-scan-extension",
        action="store_true",
        help="allow repair to add channels/categories from a strict superset scan",
    )
    parser.add_argument(
        "--baseline-scan",
        help="immutable scan used by the completed report being extended",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="perform the import; without this flag only validate and show counts",
    )
    return parser.parse_args()


def main() -> int:
    arguments = parse_args()
    try:
        scan, raw_scan = load_scan(arguments.scan)
        if arguments.repair_report:
            if not arguments.execute:
                raise ValueError("--repair-report requires --execute")
            repair_scan(
                scan,
                raw_scan,
                arguments.server_id,
                arguments.expected_guild_id,
                arguments.repair_report,
                arguments.allow_scan_extension,
                arguments.baseline_scan,
            )
        else:
            import_scan(
                scan,
                raw_scan,
                arguments.server_id,
                arguments.expected_guild_id,
                arguments.execute,
            )
    except Exception as error:
        print(f"[stoatbridge-import] ERROR: {error}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
