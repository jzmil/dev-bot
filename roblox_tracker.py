#!/usr/bin/env python3
"""Track public Roblox experiences and post changes to a Discord webhook."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests


CREATORS = (
    {"id": 7627847305, "type": "user", "label": "User 7627847305"},
    {"id": 719390069, "type": "group", "label": "Group 719390069"},
    {"id": 71552399, "type": "user", "label": "User 71552399"},
)
STATE_PATH = Path(__file__).with_name("state.json")
API_TIMEOUT = (10, 30)
MAX_PAGES_PER_CREATOR = 100
MAX_EMBEDS_PER_MESSAGE = 10
COLOR_NEW = 0x57F287
COLOR_UPDATE = 0x5865F2


class TrackerError(RuntimeError):
    pass


def request_json(url: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """GET JSON with bounded retries for transient Roblox/API errors."""
    headers = {"User-Agent": "jzmil-dev-bot/1.0 (public Roblox experience tracker)"}
    for attempt in range(4):
        try:
            response = requests.get(url, params=params, headers=headers, timeout=API_TIMEOUT)
            if response.status_code == 429 or response.status_code >= 500:
                if attempt == 3:
                    response.raise_for_status()
                retry_after = response.headers.get("Retry-After")
                delay = min(float(retry_after), 20) if retry_after else 2**attempt
                time.sleep(delay)
                continue
            response.raise_for_status()
            result = response.json()
            if not isinstance(result, dict):
                raise TrackerError(f"Unexpected response from {url}")
            return result
        except (requests.RequestException, ValueError) as exc:
            if attempt == 3:
                raise TrackerError(f"Request failed for {url}: {exc}") from exc
            time.sleep(2**attempt)
    raise TrackerError(f"Request failed for {url}")


def list_creator_games(creator: dict[str, Any], games_by_id: dict[str, dict[str, Any]]) -> None:
    """Read every public game page for one user or group."""
    if creator["type"] == "user":
        url = f"https://games.roblox.com/v2/users/{creator['id']}/games"
    else:
        url = f"https://games.roblox.com/v2/groups/{creator['id']}/games"

    cursor = None
    games: list[dict[str, Any]] = []
    for _ in range(MAX_PAGES_PER_CREATOR):
        params: dict[str, Any] = {"accessFilter": "Public", "sortOrder": "Asc", "limit": 50}
        if cursor:
            params["cursor"] = cursor
        page = request_json(url, params=params)
        rows = page.get("data")
        if not isinstance(rows, list):
            raise TrackerError(f"Unexpected games list response for {creator['label']}")
        for game in rows:
            if not isinstance(game, dict) or not game.get("id"):
                continue
            game_id = str(game["id"])
            record = games_by_id.setdefault(game_id, {"universe_id": game_id, "sources": []})
            record.update({
                "name": game.get("name") or f"Roblox experience {game_id}",
                "created": game.get("created"),
                "updated": game.get("updated"),
                "root_place_id": (game.get("rootPlace") or {}).get("id"),
                "visits": game.get("placeVisits"),
            })
            if creator["label"] not in record["sources"]:
                record["sources"].append(creator["label"])
        cursor = page.get("nextPageCursor")
        if not cursor:
            return
    raise TrackerError(f"Too many pages returned for {creator['label']} (limit {MAX_PAGES_PER_CREATOR})")


def fetch_games() -> dict[str, dict[str, Any]]:
    """Merge creators' public game lists, de-duplicated by universe ID."""
    games_by_id: dict[str, dict[str, Any]] = {}
    for creator in CREATORS:
        list_creator_games(creator, games_by_id)
    return games_by_id


def enrich_games(games: dict[str, dict[str, Any]]) -> None:
    """Fetch current player counts and canonical links in API-sized batches."""
    ids = list(games)
    for start in range(0, len(ids), 50):
        batch = ids[start : start + 50]
        details = request_json(
            "https://games.roblox.com/v1/games",
            params={"universeIds": ",".join(batch)},
        )
        rows = details.get("data")
        if not isinstance(rows, list):
            raise TrackerError("Unexpected Roblox game details response")
        for detail in rows:
            universe_id = str(detail.get("id", ""))
            if universe_id not in games:
                continue
            game = games[universe_id]
            for key in ("name", "created", "updated", "playing", "visits", "rootPlaceId"):
                if detail.get(key) is not None:
                    target_key = "root_place_id" if key == "rootPlaceId" else key
                    game[target_key] = detail[key]
            game["url"] = f"https://www.roblox.com/games/{game.get('root_place_id') or universe_id}"


def fetch_icons(universe_ids: list[str]) -> dict[str, str]:
    icons: dict[str, str] = {}
    for start in range(0, len(universe_ids), 100):
        batch = universe_ids[start : start + 100]
        data = request_json(
            "https://thumbnails.roblox.com/v1/games/icons",
            params={
                "universeIds": ",".join(batch),
                "size": "512x512",
                "format": "Png",
                "isCircular": "false",
            },
        )
        rows = data.get("data")
        if not isinstance(rows, list):
            raise TrackerError("Unexpected Roblox thumbnails response")
        for item in rows:
            if item.get("state") == "Completed" and item.get("imageUrl"):
                icons[str(item["targetId"])] = item["imageUrl"]
    return icons


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "initialized": False, "games": {}}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TrackerError(f"Could not read {path}: {exc}") from exc
    if state.get("version") != 1 or not isinstance(state.get("games"), dict):
        raise TrackerError(f"State file {path} has an unsupported format")
    return state


def compare_games(
    previous: dict[str, Any], current: dict[str, dict[str, Any]], initialized: bool
) -> list[tuple[str, dict[str, Any]]]:
    events: list[tuple[str, dict[str, Any]]] = []
    if not initialized:
        return events
    for universe_id, game in current.items():
        old = previous.get(universe_id)
        if old is None:
            events.append(("new", game))
        elif timestamp_value(game.get("updated")) > timestamp_value(old.get("updated")):
            events.append(("updated", game))
    return events


def timestamp_value(value: Any) -> float:
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def discord_embed(kind: str, game: dict[str, Any], icon: str | None = None) -> dict[str, Any]:
    new_game = kind == "new"
    description = (game.get("description") or "").strip()
    if len(description) > 500:
        description = description[:497].rstrip() + "..."
    fields = [
        {"name": "Creator", "value": ", ".join(game.get("sources") or ["Tracked creator"]), "inline": True},
        {"name": "Players", "value": str(game.get("playing", 0)), "inline": True},
        {"name": "Visits", "value": str(game.get("visits", 0)), "inline": True},
    ]
    embed: dict[str, Any] = {
        "title": ("🆕 New game: " if new_game else "🔄 Game updated: ") + str(game.get("name", "Roblox experience"))[:200],
        "url": game.get("url"),
        "description": description or "Public Roblox experience",
        "color": COLOR_NEW if new_game else COLOR_UPDATE,
        "fields": fields,
        "footer": {"text": f"Universe ID: {game['universe_id']}"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if icon:
        embed["thumbnail"] = {"url": icon}
    return embed


def post_discord(webhook_url: str, embeds: list[dict[str, Any]]) -> None:
    for start in range(0, len(embeds), MAX_EMBEDS_PER_MESSAGE):
        batch = embeds[start : start + MAX_EMBEDS_PER_MESSAGE]
        try:
            response = requests.post(
                webhook_url,
                json={"username": "Roblox Game Tracker", "embeds": batch},
                params={"wait": "true"},
                timeout=API_TIMEOUT,
            )
            if response.status_code == 429 or response.status_code >= 500:
                retry_after = response.headers.get("Retry-After")
                if retry_after and start == 0:
                    time.sleep(min(float(retry_after), 20))
                    response = requests.post(
                        webhook_url,
                        json={"username": "Roblox Game Tracker", "embeds": batch},
                        params={"wait": "true"},
                        timeout=API_TIMEOUT,
                    )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise TrackerError(f"Discord webhook delivery failed: {exc}") from exc


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        json.dump(state, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        temporary_path = Path(handle.name)
    temporary_path.replace(path)


def run(dry_run: bool = False, state_path: Path = STATE_PATH) -> int:
    state = load_state(state_path)
    current = fetch_games()
    enrich_games(current)
    events = compare_games(state["games"], current, state["initialized"])

    if state["initialized"]:
        icon_ids = [game["universe_id"] for _, game in events]
        try:
            icons = fetch_icons(icon_ids) if icon_ids else {}
        except TrackerError as exc:
            print(f"Warning: thumbnails unavailable; continuing without them ({exc})", file=sys.stderr)
            icons = {}
        embeds = [discord_embed(kind, game, icons.get(game["universe_id"])) for kind, game in events]
    else:
        embeds = []

    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook_url and not dry_run:
        raise TrackerError("DISCORD_WEBHOOK_URL is not set")

    if not state["initialized"]:
        print(f"First scan: recording a baseline of {len(current)} public games.")
        if not dry_run:
            post_discord(webhook_url, [{
                "title": "✅ Roblox tracker is online",
                "description": f"Baseline recorded for {len(current)} public games across the three tracked creators. New games and later updates will be posted here.",
                "color": COLOR_NEW,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }])
    elif embeds:
        print(f"Found {len(events)} change(s): {sum(k == 'new' for k, _ in events)} new, {sum(k == 'updated' for k, _ in events)} updated.")
        if not dry_run:
            post_discord(webhook_url, embeds)
    else:
        print(f"No changes found across {len(current)} public games.")

    if dry_run:
        print("Dry run: state was not changed and Discord was not contacted.")
        return 0

    # Persist only fields that affect future comparisons. Player and visit counts
    # change constantly and would otherwise create a repository commit every scan.
    saved_games = {
        universe_id: {
            key: game.get(key)
            for key in ("universe_id", "name", "created", "updated", "root_place_id", "sources")
        }
        for universe_id, game in current.items()
    }
    heartbeat_month = datetime.now(timezone.utc).strftime("%Y-%m")
    new_state = {
        "version": 1,
        "initialized": True,
        "heartbeat_month": heartbeat_month,
        "games": saved_games,
    }
    save_state(state_path, new_state)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="scan Roblox without sending Discord messages or saving state")
    parser.add_argument("--state", type=Path, default=STATE_PATH, help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        return run(dry_run=args.dry_run, state_path=args.state)
    except TrackerError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
