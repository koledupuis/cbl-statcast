"""
Thin client for the CBL (Canadian Baseball League) public stats API.

All requests go through simple time-based in-memory caching so that
loading a leaderboard page doesn't hammer cbl.ca on every refresh.

Completed gameday feeds additionally persist to disk (see
_gameday_disk_cache below) -- the in-memory cache alone gets wiped on
every process restart, which includes Flask's debug-mode auto-reloader
firing on every file save, so without a disk layer a single afternoon
of local development can mean re-fetching a whole team's schedule
(gamelog.py / splits.py walk 30-40+ games per player) over and over.
A completed game's box score never changes, so once it's on disk it's
never re-requested from cbl.ca again.
"""
import json
import os
import time
from pathlib import Path

import requests

BASE_URL = "https://www.cbl.ca/api/stats-api"
DEFAULT_SEASON = os.environ.get("CBL_SEASON", "2026 Summer")
DEFAULT_SEASON_YEAR = os.environ.get("CBL_SEASON_YEAR", "2026")
CACHE_TTL_SECONDS = int(os.environ.get("CBL_CACHE_TTL", "120"))
REQUEST_TIMEOUT = 15

_cache = {}  # key -> (fetched_at, data)


def _cached_get(url, params, cache_key, ttl=None):
    ttl = CACHE_TTL_SECONDS if ttl is None else ttl
    now = time.time()
    hit = _cache.get(cache_key)
    if hit and (now - hit[0]) < ttl:
        return hit[1]

    resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    _cache[cache_key] = (now, data)
    return data


def get_batting(season=None, ttl=None):
    season = season or DEFAULT_SEASON
    url = f"{BASE_URL}/stats/batting"
    data = _cached_get(url, {"season": season, "format": "json"}, f"batting:{season}", ttl=ttl)
    return data.get("stats", [])


def get_pitching(season=None, ttl=None):
    season = season or DEFAULT_SEASON
    url = f"{BASE_URL}/stats/pitching"
    data = _cached_get(url, {"season": season, "format": "json"}, f"pitching:{season}", ttl=ttl)
    return data.get("stats", [])


def get_fielding(season=None):
    season = season or DEFAULT_SEASON
    url = f"{BASE_URL}/stats/fielding"
    data = _cached_get(url, {"season": season, "format": "json"}, f"fielding:{season}")
    return data.get("stats", [])


def get_game_ids(season_year=None):
    season_year = season_year or DEFAULT_SEASON_YEAR
    url = f"{BASE_URL}/feed/game-ids"
    data = _cached_get(url, {"seasonYear": season_year, "format": "json"}, f"gameids:{season_year}")
    return data


GAMEDAY_LIVE_TTL = int(os.environ.get("CBL_GAMEDAY_LIVE_TTL", "20"))
GAMEDAY_FINAL_TTL = int(os.environ.get("CBL_GAMEDAY_FINAL_TTL", "3600"))

# Where completed-game feeds get persisted to disk. Defaults to a folder
# next to the app so it survives restarts/deploys without any setup;
# point it at a fixed path (e.g. a mapped drive) if you want the cache
# to survive a full reinstall too.
GAMEDAY_CACHE_DIR = Path(os.environ.get("CBL_GAMEDAY_CACHE_DIR", ".cbl_cache/gameday"))


def _safe_game_filename(public_game_id):
    # public_game_id is already a URL slug (e.g. "kitchener-at-brantford-2026-07-19"),
    # but strip anything that isn't filename-safe just in case.
    safe = "".join(c for c in public_game_id if c.isalnum() or c in "-_")
    return f"{safe}.json"


def _load_gameday_from_disk(public_game_id):
    path = GAMEDAY_CACHE_DIR / _safe_game_filename(public_game_id)
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        # Corrupt/partial file on disk shouldn't break the page -- just
        # re-fetch from the network as if there were no disk cache at all.
        return None


def _save_gameday_to_disk(public_game_id, data):
    path = GAMEDAY_CACHE_DIR / _safe_game_filename(public_game_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(".json.tmp")
        with tmp_path.open("w", encoding="utf-8") as f:
            json.dump(data, f)
        tmp_path.replace(path)  # atomic on the same filesystem
    except OSError:
        pass  # disk cache is a nice-to-have; never let it break a page load


def get_gameday(public_game_id):
    """
    Pull the full pitch-by-pitch feed for one game (rosters, lineups,
    at-bats, pitches, batted-ball detail, events).

    Completed games are effectively static, so once we've seen a game
    marked "completed" we cache it in memory for a long time *and*
    persist it to disk, so it's never fetched from cbl.ca again for the
    life of this cache directory. Anything else (in progress,
    scheduled) is re-fetched frequently and never written to disk.
    """
    cache_key = f"gameday:{public_game_id}"

    hit = _cache.get(cache_key)
    if hit and (time.time() - hit[0]) < (GAMEDAY_FINAL_TTL if hit[1].get("status") == "completed" else GAMEDAY_LIVE_TTL):
        return hit[1]

    disk_data = _load_gameday_from_disk(public_game_id)
    if disk_data and disk_data.get("status") == "completed":
        _cache[cache_key] = (time.time(), disk_data)
        return disk_data

    url = f"{BASE_URL}/feed/public-gameday"
    ttl = GAMEDAY_FINAL_TTL if hit and hit[1].get("status") == "completed" else GAMEDAY_LIVE_TTL
    data = _cached_get(url, {"publicGameId": public_game_id}, cache_key, ttl=ttl)

    if data.get("status") == "completed":
        _save_gameday_to_disk(public_game_id, data)

    return data


def get_player_analytics(player_id, season=None, ttl=None):
    season = season or DEFAULT_SEASON
    url = f"{BASE_URL}/players/{player_id}/analytics"
    # Analytics is player-specific and changes as new games finalize; still
    # cache briefly so a page load doesn't fire duplicate requests.
    data = _cached_get(url, {"season": season, "format": "json"}, f"analytics:{player_id}:{season}", ttl=ttl)
    return data


def clear_cache(include_disk=False):
    """
    Clear the in-memory cache. Pass include_disk=True to also wipe the
    persisted completed-game feeds (e.g. if a completed game somehow
    needed a correction and you want it re-fetched from cbl.ca).
    """
    _cache.clear()
    if include_disk and GAMEDAY_CACHE_DIR.exists():
        for f in GAMEDAY_CACHE_DIR.glob("*.json"):
            f.unlink(missing_ok=True)

