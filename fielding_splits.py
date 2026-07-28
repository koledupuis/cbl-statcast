"""
Per-position fielding splits for one player -- built by walking every
game the player's team played and aggregating each game's
`playerFieldingStats[pid].positionStats` breakdown by position.

Confirmed real structure (from a real uploaded gameday payload):
  liveGame.playerFieldingStats[pid].positionStats.{POSITION} = {
    "games", "gamesStarted", "putouts", "assists", "doublePlays",
    "triplePlays", "fieldingErrors", "throwingErrors", "innings", ...
  }

A player can have more than one position key within a single game's
positionStats if they changed position mid-game (e.g. moved from 2B to
SS) -- each position gets credited that game's stats for that specific
position, not the whole game lumped under one position.

No new network calls -- rides on the same cached gameday fetches every
other per-game module in this app already triggers (gamelog.py,
splits.py, pitching_splits.py all walk the same schedule the same way).

Errors: CBL's per-position breakdown separates fieldingErrors from
throwingErrors -- this module sums both into a single "errors" total
per position (matching how the season fielding stat row and every
other part of this app already reports errors), rather than showing
them as two separate numbers nobody else on the site distinguishes.
"""
from collections import OrderedDict

import cbl_api
import gameday
import gamelog
import stats


def _blank():
    return {
        "games": 0, "games_started": 0, "putouts": 0, "assists": 0,
        "fielding_errors": 0, "throwing_errors": 0,
        "double_plays": 0, "triple_plays": 0,
    }


def _accumulate(totals, pos_stats):
    totals["games"] += pos_stats.get("games") or 0
    totals["games_started"] += pos_stats.get("gamesStarted") or 0
    totals["putouts"] += pos_stats.get("putouts") or 0
    totals["assists"] += pos_stats.get("assists") or 0
    totals["fielding_errors"] += pos_stats.get("fieldingErrors") or 0
    totals["throwing_errors"] += pos_stats.get("throwingErrors") or 0
    totals["double_plays"] += pos_stats.get("doublePlays") or 0
    totals["triple_plays"] += pos_stats.get("triplePlays") or 0


def _finalize(totals):
    po = totals["putouts"]
    a = totals["assists"]
    e = totals["fielding_errors"] + totals["throwing_errors"]
    totals["errors"] = e
    totals["total_chances"] = po + a + e
    totals["fielding_pct"] = stats.safe_div(po + a, po + a + e)
    totals["range_factor"] = stats.safe_div(po + a, totals["games"])
    return totals


def _is_night(game_time):
    """Rough day/night cut on the printed gameTime (e.g. '2:00 PM');
    6pm+ counts as night -- same convention splits.py's own _is_night
    uses for batting/pitching Game Type Splits, kept as a small local
    copy here rather than importing another module's private helper."""
    if not game_time:
        return None
    t = game_time.strip().upper()
    try:
        hh = int(t.split(":")[0]) % 12
        if "PM" in t:
            hh += 12
        return hh >= 18
    except (ValueError, IndexError):
        return None


def _walk_fielding(player_id, team_name, season_year, bucket_fn):
    """Shared walk for the monthly/day-night split builders below --
    `bucket_fn(g)` returns the bucket LABEL for one scheduled game (a
    month name, or "Day"/"Night"), or None to skip that game (e.g. an
    unrecognized game time for the day/night split). Combines every
    position a player played that game into one row per bucket, since
    month/day-night splits are about overall fielding workload, not
    broken out by position the way build_fielding_position_splits is."""
    buckets = OrderedDict()

    for g in gamelog.team_games(team_name, season_year):
        label = bucket_fn(g)
        if label is None:
            continue
        public_id = gamelog._field(g, "publicGameId", "public_game_id", "public-game-id")
        if not public_id:
            continue
        try:
            gd = cbl_api.get_gameday(public_id)
        except Exception:
            continue
        if not gd or not gd.get("snapshot"):
            continue

        pfs = gameday.get_player_fielding_stats(gd, player_id)
        if not pfs:
            continue
        bucket = buckets.setdefault(label, _blank())
        for pos, pos_stats in (pfs.get("positionStats") or {}).items():
            _accumulate(bucket, pos_stats)

    result = []
    for label, totals in buckets.items():
        _finalize(totals)
        totals["label"] = label
        result.append(totals)
    return result


def build_fielding_monthly_splits(player_id, team_name, season_year=None):
    """Fielding totals (every position combined) bucketed by month --
    same MONTH_NAMES/date convention gamelog.py's own batting Monthly
    Splits uses. Returns a list of totals dicts (each with a "label"
    key), in the order months are first encountered -- team_games()
    already walks chronologically, so this comes out in season order
    for free."""
    return _walk_fielding(
        player_id, team_name, season_year,
        lambda g: gamelog._month_name(gamelog._field(g, "gameDate", "game_date", "game-date", default="")),
    )


def build_fielding_daynight_splits(player_id, team_name, season_year=None):
    """Fielding totals (every position combined) split by day/night
    game time. Games with an unrecognized/missing game time are
    skipped from this split entirely (not lumped into either bucket),
    same as splits.py's own day/night handling elsewhere."""
    def _bucket(g):
        game_time = gamelog._field(g, "gameTime", "game_time", "game-time", default="")
        is_night = _is_night(game_time)
        if is_night is None:
            return None
        return "Night Games" if is_night else "Day Games"

    return _walk_fielding(player_id, team_name, season_year, _bucket)


def build_fielding_position_splits(player_id, team_name, season_year=None):
    """Returns a list of per-position totals dicts (each with a
    "position" key), sorted by games played at that position, most-
    played first. Empty list if the player has no fielding data on
    record for this team/season."""
    positions = OrderedDict()

    for g in gamelog.team_games(team_name, season_year):
        public_id = gamelog._field(g, "publicGameId", "public_game_id", "public-game-id")
        if not public_id:
            continue
        try:
            gd = cbl_api.get_gameday(public_id)
        except Exception:
            continue
        if not gd or not gd.get("snapshot"):
            continue

        pfs = gameday.get_player_fielding_stats(gd, player_id)
        if not pfs:
            continue
        for pos, pos_stats in (pfs.get("positionStats") or {}).items():
            bucket = positions.setdefault(pos, _blank())
            _accumulate(bucket, pos_stats)

    result = []
    for pos, totals in positions.items():
        _finalize(totals)
        totals["position"] = pos
        result.append(totals)

    result.sort(key=lambda p: p["games"], reverse=True)
    return result
