"""
Handles players who switched teams mid-season.

CBL's season stat endpoints (`/stats/batting`, `/stats/pitching`,
`/stats/fielding`) return one row per player *per team-stint* -- so a
player traded partway through the season shows up as two separate rows
with the same playerId but different teamName (and each row's own
partial-season counting stats). This app's `_find_player()`-style
lookups previously just grabbed whichever row happened to come first in
the list, silently discarding the rest -- so a traded player's own page
showed only one team's partial stats, and worse, their game log only
walked that one team's schedule, since the team name for building it
came from that single row.

This module finds *every* row for a player and merges them into one
combined season line: counting stats are summed, rate stats are
recomputed from the combined totals (using the same simplified
conventions -- no HBP/SF -- already used elsewhere in this app for
approximated OBP/SLG, since the season stat line doesn't break those
out separately). The list of every team name a merged row's stats came
from is also returned, since anything that walks a schedule (game logs,
splits, rolling stats) needs to walk *all* of those teams' schedules,
not just one -- gamelog.team_games() and everything built on it already
accepts a list of team names for exactly this reason.
"""


def find_player_rows(player_id, rows):
    """Every row matching player_id, in the order they appear (usually
    chronological by team-stint, but not guaranteed)."""
    return [r for r in rows if r.get("playerId") == player_id]


def team_names_for_rows(rows):
    """Unique team names across a player's rows, preserving first-seen
    order. A single-team player gets a single-item list; a traded
    player gets one entry per team-stint found."""
    seen = []
    for r in rows:
        name = r.get("teamName")
        if name and name not in seen:
            seen.append(name)
    return seen


def _safe_div(n, d):
    return (n / d) if d else 0.0


def merge_batting_rows(rows):
    """Merges 1+ season batting rows for the same player into one
    combined row. Returns None for an empty list, the row itself
    unchanged for a single-team player (no merging needed)."""
    if not rows:
        return None
    if len(rows) == 1:
        return rows[0]

    merged = dict(rows[0])  # identity fields (playerId, fullName, position) from the first
    counting_keys = ("games", "plateAppearances", "atBats", "runs", "hits",
                      "doubles", "triples", "homeRuns", "rbi", "walks", "strikeouts")
    for key in counting_keys:
        merged[key] = sum(r.get(key) or 0 for r in rows)

    ab = merged["atBats"]
    h = merged["hits"]
    bb = merged["walks"]
    pa = merged["plateAppearances"]
    singles = max(h - merged["doubles"] - merged["triples"] - merged["homeRuns"], 0)
    tb = singles + 2 * merged["doubles"] + 3 * merged["triples"] + 4 * merged["homeRuns"]

    merged["battingAvg"] = _safe_div(h, ab)
    merged["obp"] = _safe_div(h + bb, pa)  # no HBP/SF in the season line, same approximation used elsewhere
    merged["slg"] = _safe_div(tb, ab)
    merged["ops"] = merged["obp"] + merged["slg"]
    merged["teamName"] = " / ".join(team_names_for_rows(rows))
    return merged


def merge_pitching_rows(rows):
    """Merges 1+ season pitching rows for the same player into one
    combined row."""
    if not rows:
        return None
    if len(rows) == 1:
        return rows[0]

    merged = dict(rows[0])
    counting_keys = ("games", "gamesStarted", "wins", "losses", "saves",
                      "hitsAllowed", "earnedRuns", "walksAllowed", "strikeoutsPitching",
                      "homeRunsAllowed")
    for key in counting_keys:
        merged[key] = sum(r.get(key) or 0 for r in rows)

    adv_keys = ("inningsPitchedOuts", "battersFaced", "pitchCount",
                "strikesSwinging", "groundOuts", "airOuts", "homeRunsAllowed")
    merged_adv = {}
    for key in adv_keys:
        merged_adv[key] = sum((r.get("advancedPitching") or {}).get(key) or 0 for r in rows)
    merged["advancedPitching"] = merged_adv

    outs = merged_adv.get("inningsPitchedOuts") or 0
    ip = outs / 3
    merged["inningsPitched"] = f"{outs // 3}.{outs % 3}"
    merged["era"] = _safe_div(merged["earnedRuns"] * 9, ip)
    merged["whip"] = _safe_div(merged["hitsAllowed"] + merged["walksAllowed"], ip)
    merged["teamName"] = " / ".join(team_names_for_rows(rows))
    return merged


def merge_fielding_rows(rows):
    """Merges 1+ season fielding rows for the same player into one
    combined row."""
    if not rows:
        return None
    if len(rows) == 1:
        return rows[0]

    merged = dict(rows[0])
    counting_keys = ("games", "putouts", "assists", "errors", "totalChances", "doublePlays")
    for key in counting_keys:
        merged[key] = sum(r.get(key) or 0 for r in rows)

    po = merged["putouts"]
    a = merged["assists"]
    tc = merged["totalChances"]
    merged["fieldingPct"] = _safe_div(po + a, tc)
    merged["teamName"] = " / ".join(team_names_for_rows(rows))
    return merged
