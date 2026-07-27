"""
Per-player game log and schedule-derived splits (monthly, home/away),
built by walking the season schedule (/feed/game-ids) and aggregating
each completed game's boxscore (/feed/public-gameday) for one player.

This is pure aggregation over what cbl_api.py already fetches, so it
adds no new network surface -- every underlying call goes through
cbl_api._cached_get, which already caches each game individually.
Walking a full team schedule still means on the order of 30-40 gameday
fetches for one player, so this is wired up as its own lazily-loaded
route/page (see app.py: /player/<id>/gamelog) rather than being built
on every player-page load.

A note on field names: cbl_api.get_game_ids() requests format=json, so
the live response should use the same camelCase convention as every
other endpoint (playerId, teamName, publicGameId, ...). The XML sample
this was built against uses dash-case tags (<public-game-id>) since
that's what the feed renders without format=json. _field() below tries
several spellings for each value so a casing mismatch degrades
gracefully (the game/row is just skipped) instead of crashing the
page -- but if the on-site game log ever comes up empty, this is the
first place to check against a real JSON response.
"""
from collections import OrderedDict

import cbl_api
import gameday
import re
import stats

MONTH_NAMES = [
    "", "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]

BOX_COUNTING_KEYS = ("ab", "r", "h", "doubles", "triples", "hr", "rbi", "bb", "so")


def _field(d, *names, default=None):
    """Try several possible key spellings for one logical field."""
    for n in names:
        if n in d:
            return d[n]
    return default


def _all_games(season_year=None):
    data = cbl_api.get_game_ids(season_year) or {}
    if isinstance(data, list):
        return data
    return _field(data, "ids", "games", default=[]) or []


def _normalize(s):
    """Lowercase, trim, collapse repeated whitespace, and treat hyphens/
    underscores/periods as spaces -- schedule data is hand-entered, and
    a compound town name like "Chatham-Kent" is exactly the kind of
    thing that ends up spelled two different ways ("Chatham-Kent" vs
    "Chatham Kent") across different games on the same schedule. Plain
    lowercase+trim (the original version of this function) doesn't
    catch that; this does."""
    s = (s or "").strip().lower()
    s = re.sub(r"[-_.]", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def _as_team_name_set(team_name):
    """team_name may be a single team name (the normal case) or an
    iterable of team names (a player who switched teams mid-season has
    played for more than one) -- either way, returns a set of
    normalized names to match against."""
    if isinstance(team_name, str):
        return {_normalize(team_name)}
    return {_normalize(t) for t in team_name}


def team_matches(name, team_name):
    """True if `name` (a schedule entry's home/away team field) matches
    `team_name`, which may be a single team name or an iterable of
    them (see _as_team_name_set)."""
    return _normalize(name) in _as_team_name_set(team_name)


def team_games(team_name, season_year=None, statuses=("completed",)):
    """Games for one team (or, if `team_name` is a list/set/tuple of
    names, every game belonging to ANY of them -- for a player who
    switched teams mid-season and needs both schedules walked as one
    combined game log), oldest first, optionally filtered by status.

    Team-name and status comparisons are normalized (trimmed, case-
    insensitive, hyphens/underscores/periods treated as spaces) rather
    than exact-matched -- a schedule feed is hand-entered data, and a
    single game with a stray extra space, a different status casing
    ("Completed" vs "completed"), or a punctuation difference in a
    compound name ("Chatham-Kent" vs "Chatham Kent") than every other
    game would otherwise silently vanish from that team's game log with
    no error, while every other game on the schedule keeps working
    fine. This doesn't change what "completed" means -- the `statuses`
    values you pass in are still matched exactly except for
    case/whitespace, so a genuinely different status (e.g. "postponed")
    still won't match.

    De-duplicates by public game ID: if the season schedule feed ever
    lists the same game twice (e.g. a stale entry left behind by a
    reschedule), processing it twice would silently double-count that
    game's stats into any game log / totals built from this list. Keeps
    the first occurrence encountered."""
    team_name_set = _as_team_name_set(team_name)
    norm_statuses = {_normalize(s) for s in statuses} if statuses else None

    games = []
    seen_ids = set()
    for g in _all_games(season_year):
        home = _field(g, "homeTeam", "home_team", "home-team")
        away = _field(g, "awayTeam", "away_team", "away-team")
        if _normalize(home) not in team_name_set and _normalize(away) not in team_name_set:
            continue
        if norm_statuses and _normalize(_field(g, "status", default="")) not in norm_statuses:
            continue
        public_id = _field(g, "publicGameId", "public_game_id", "public-game-id")
        if public_id:
            if public_id in seen_ids:
                continue
            seen_ids.add(public_id)
        games.append(g)

    def sort_key(g):
        return (
            _field(g, "gameDate", "game_date", "game-date", default=""),
            _field(g, "gameTime", "game_time", "game-time", default=""),
        )

    return sorted(games, key=sort_key)


def build_player_game_log(player_id, team_name, season_year=None):
    """
    {
      "months": OrderedDict(month_name -> {"rows": [...], "totals": {...}}),
      "season_totals": {...},
    }
    Each row has the raw counting line (ab/r/h/doubles/triples/hr/rbi/bb/so)
    plus per-game AVG/OBP/SLG/OPS, the date, opponent, and home/away flag.

    Totals are computed by summing the rows actually collected below (see
    the final pass at the end of this function), not accumulated
    incrementally alongside them -- so a monthly or season total can
    never disagree with the sum of what's actually displayed for that
    month/season, no matter what upstream cause (a duplicate schedule
    entry, a stale cache, etc.) might otherwise introduce a mismatch.
    """
    months = OrderedDict()

    for g in team_games(team_name, season_year):
        public_id = _field(g, "publicGameId", "public_game_id", "public-game-id")
        if not public_id:
            continue
        try:
            gd = cbl_api.get_gameday(public_id)
        except Exception:
            continue
        if not gd or not gd.get("snapshot"):
            continue

        lookup = gameday.build_player_lookup(gd)
        box = gameday.build_batting_box(gd, lookup)

        home = _field(g, "homeTeam", "home_team", "home-team")
        away = _field(g, "awayTeam", "away_team", "away-team")
        is_home = team_matches(home, team_name)
        side = "home" if is_home else "away"
        opponent = away if is_home else home

        line = _find_row(box.get(side, []), player_id)
        if line is None:
            continue  # player didn't appear (DNP / not on this roster yet)

        date_str = _field(g, "gameDate", "game_date", "game-date", default="")
        row = {
            "date": date_str,
            "opponent": opponent,
            "is_home": is_home,
            "public_game_id": public_id,
            "venue": gameday.get_venue(gd),
            "umpire": gameday.get_home_plate_umpire(gd),
            **line,
            **_game_rates(line),
        }

        bucket = months.setdefault(_month_name(date_str), {"rows": [], "totals": _blank_totals()})
        bucket["rows"].append(row)

    # Final pass: totals are the sum of each month's own rows (and season
    # totals are the sum of every row across every month) -- see docstring.
    season_totals = _blank_totals()
    for bucket in months.values():
        month_totals = _blank_totals()
        for row in bucket["rows"]:
            _accumulate(month_totals, row)
            _accumulate(season_totals, row)
        bucket["totals"] = month_totals

    for bucket in months.values():
        bucket["totals"].update(_game_rates(bucket["totals"]))

    season_totals.update(_game_rates(season_totals))
    return {"months": months, "season_totals": season_totals}


def build_home_away_splits(game_log):
    """Roll a built game log's rows up into home/away counting + rate lines."""
    home_totals = _blank_totals()
    away_totals = _blank_totals()
    for bucket in game_log["months"].values():
        for row in bucket["rows"]:
            _accumulate(home_totals if row["is_home"] else away_totals, row)
    home_totals.update(_game_rates(home_totals))
    away_totals.update(_game_rates(away_totals))
    return {"home": home_totals, "away": away_totals}


def _find_row(rows, player_id):
    for r in rows:
        if r.get("playerId") == player_id:
            return r
    return None


def _blank_totals():
    return {k: 0 for k in BOX_COUNTING_KEYS}


def _accumulate(totals, row):
    for key in BOX_COUNTING_KEYS:
        totals[key] = totals.get(key, 0) + (row.get(key) or 0)


def _game_rates(row):
    ab = row.get("ab") or 0
    h = row.get("h") or 0
    bb = row.get("bb") or 0
    doubles = row.get("doubles") or 0
    triples = row.get("triples") or 0
    hr = row.get("hr") or 0
    singles = max(h - doubles - triples - hr, 0)
    tb = singles + 2 * doubles + 3 * triples + 4 * hr
    pa = ab + bb  # HBP/SF aren't tracked by gameday.build_batting_box today
    obp = stats.safe_div(h + bb, pa)
    slg = stats.safe_div(tb, ab)
    return {
        "pa": pa,
        "avg": stats.safe_div(h, ab),
        "obp": obp,
        "slg": slg,
        "ops": obp + slg,
    }


def _month_name(date_str):
    try:
        month_num = int(date_str.split("-")[1])
        return MONTH_NAMES[month_num]
    except (IndexError, ValueError):
        return date_str or "Unknown"


def team_schedule_status_breakdown(team_name, season_year=None):
    """{"completed": 12, "final": 1, ...} -- every RAW status string
    (unmodified, not normalized) seen on a schedule entry whose home or
    away team matches team_name, regardless of what that status is.

    team_games() itself filters entries down to whatever `statuses` you
    pass it (normally just "completed") *before* returning anything, so
    a game whose status is spelled differently from what you're
    filtering for -- not a case/punctuation variant (already handled),
    but a genuinely different word, like "final" instead of "completed"
    -- silently vanishes with zero visible trace anywhere. This function
    exists specifically so that kind of gap is visible: if this breakdown
    shows anything other than "completed" (or your equivalent), that's
    almost certainly a missing-game bug hiding in plain sight."""
    from collections import Counter
    team_name_set = _as_team_name_set(team_name)
    counts = Counter()
    for g in _all_games(season_year):
        home = _field(g, "homeTeam", "home_team", "home-team")
        away = _field(g, "awayTeam", "away_team", "away-team")
        if _normalize(home) not in team_name_set and _normalize(away) not in team_name_set:
            continue
        status = _field(g, "status", default="") or "(no status field)"
        counts[status] += 1
    return dict(counts)


def build_park_splits(game_log, team_name):
    """Groups an already-built player game log (see build_player_game_log)
    by PARK, using the real venue name confirmed to exist in CBL's raw
    payload (see gameday.get_venue()) when a game's row has one. Falls
    back to labeling by home team ("Home (Team Name)" / the opponent's
    name for road games) for any specific game where venue came back
    None -- keeps the split usable even if venue is missing on some
    older games rather than dropping those games from the split
    entirely.

    No new network calls or schedule walking here -- this just
    re-buckets rows the game log already computed for the Game Log tab.

    Returns a list of {"park", "games", counting stats..., rate stats...}
    sorted by games played (most-visited park first).
    """
    parks = OrderedDict()

    for bucket in (game_log or {}).get("months", {}).values():
        for row in bucket["rows"]:
            venue = row.get("venue")
            if venue:
                park = venue
            elif row.get("is_home"):
                park = f"Home ({team_name})"
            else:
                park = row.get("opponent") or "Unknown"
            parks.setdefault(park, {"rows": []})["rows"].append(row)

    result = []
    for park, data in parks.items():
        totals = _blank_totals()
        for row in data["rows"]:
            _accumulate(totals, row)
        totals.update(_game_rates(totals))
        totals["park"] = park
        totals["games"] = len(data["rows"])
        result.append(totals)

    result.sort(key=lambda p: p["games"], reverse=True)
    return result


def build_umpire_splits(game_log, min_games=1):
    """Same idea as build_park_splits, grouped by home plate umpire
    instead of park. No new network calls -- re-buckets rows already
    fetched for the Game Log tab. Games with no identifiable umpire
    (see gameday.get_home_plate_umpire) are simply excluded rather than
    lumped into an "Unknown" bucket, since that wouldn't be a
    meaningful split.

    min_games filters out umpires seen in only a game or two by
    default this stays at 1 (show everything) -- callers can raise it
    for a "meaningful sample only" view if single-game noise isn't
    useful."""
    umpires = OrderedDict()

    for bucket in (game_log or {}).get("months", {}).values():
        for row in bucket["rows"]:
            ump = row.get("umpire")
            if not ump:
                continue
            umpires.setdefault(ump, {"rows": []})["rows"].append(row)

    result = []
    for ump, data in umpires.items():
        if len(data["rows"]) < min_games:
            continue
        totals = _blank_totals()
        for row in data["rows"]:
            _accumulate(totals, row)
        totals.update(_game_rates(totals))
        totals["umpire"] = ump
        totals["games"] = len(data["rows"])
        result.append(totals)

    result.sort(key=lambda p: p["games"], reverse=True)
    return result
