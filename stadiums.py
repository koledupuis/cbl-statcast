"""
League-wide batting stats aggregated by park/venue -- powers the
Stadiums leaderboard tab and each park's own detail page.

build_stadium_stats walks the full season schedule ONCE (gamelog._all_games
-- every game in the league, not one team's schedule), not once per
team the way a per-team walk would -- a single game involves two
teams, so iterating team-by-team would process every game's at-bats
twice for no reason. Each game's venue comes from gameday.get_venue
(confirmed real field, snapshot.setup.venue), and every completed
plate appearance from that game gets counted toward that venue's
totals -- both teams' hitters combined, since this is a park-factor-
style view (how does the ball carry at this specific field), not a
team or player stat.

build_park_detail walks the season AGAIN, filtered to one specific
venue, to add the things the leaderboard row doesn't have room for:
a home/away split, pitching-against totals, per-player leaders at
that park, and the actual list of games played there. This is a
second full pass over the schedule (every game's own gameday payload
needs fetching just to know its venue in the first place, so there's
no way to skip non-matching games without still fetching them) --
an acceptable cost for a detail page someone clicks into occasionally,
not something polled repeatedly the way the live broadcast overlay is.

Games with no identifiable venue are grouped under "Unknown Venue"
rather than silently dropped, so the season-wide totals across every
row still add up to the real number of completed games -- excluding
them would make the page's row count quietly disagree with the
season's actual game count.
"""
from collections import OrderedDict

import cbl_api
import gameday
import gamelog
import stats


def _blank():
    return {
        "games": 0, "pa": 0, "ab": 0, "h": 0, "doubles": 0, "triples": 0, "hr": 0,
        "r": 0, "rbi": 0, "bb": 0, "so": 0,
    }


def _accumulate_batting(totals, ab):
    """Folds one completed at-bat into a batting totals dict -- shared
    by the season-wide per-venue walk and the single-park detail walk
    below, rather than duplicated in each."""
    outcome = ab.get("outcome") or ""
    is_ab = not gameday.is_non_ab_outcome(ab)
    totals["pa"] += 1
    if is_ab:
        totals["ab"] += 1
    if outcome in gameday.HIT_OUTCOMES:
        totals["h"] += 1
        if outcome == "double":
            totals["doubles"] += 1
        elif outcome == "triple":
            totals["triples"] += 1
        elif outcome == "home_run":
            totals["hr"] += 1
    elif outcome in gameday.WALK_OUTCOMES:
        totals["bb"] += 1
    if outcome in gameday.STRIKEOUT_OUTCOMES:
        totals["so"] += 1
    totals["r"] += len(ab.get("runsScored") or [])
    totals["rbi"] += ab.get("rbiCount") or 0


def _finalize_batting(totals):
    """Adds avg/obp/slg/ops (and hr_per_game/runs_per_game, when
    "games" is present) to an already-accumulated batting totals
    dict, in place."""
    h, ab, bb, pa = totals["h"], totals["ab"], totals["bb"], totals["pa"]
    doubles, triples, hr = totals["doubles"], totals["triples"], totals["hr"]
    singles = max(h - doubles - triples - hr, 0)
    tb = singles + 2 * doubles + 3 * triples + 4 * hr
    obp = stats.safe_div(h + bb, pa)
    slg = stats.safe_div(tb, ab)
    totals["avg"] = stats.safe_div(h, ab)
    totals["obp"] = obp
    totals["slg"] = slg
    totals["ops"] = (obp or 0) + (slg or 0)
    if "games" in totals and totals["games"]:
        totals["hr_per_game"] = stats.safe_div(hr, totals["games"])
        totals["runs_per_game"] = stats.safe_div(totals["r"], totals["games"])
    return totals


def build_stadium_stats(season_year=None):
    """Returns a list of per-venue totals dicts, sorted by games played
    descending. Each dict has the raw counting line (games/pa/ab/h/
    doubles/triples/hr/r/rbi/bb/so) plus avg/obp/slg/ops/hr_per_game --
    same rate-stat conventions used everywhere else in this app."""
    venues = OrderedDict()

    for g in gamelog._all_games(season_year):
        if gamelog._field(g, "status", default="") != "completed":
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

        venue = gameday.get_venue(gd) or "Unknown Venue"
        totals = venues.setdefault(venue, _blank())
        totals["games"] += 1

        for ab in gameday.get_at_bats(gd):
            if not ab.get("isComplete"):
                continue
            _accumulate_batting(totals, ab)

    result = [_finalize_batting(dict(totals, venue=venue)) for venue, totals in venues.items()]
    result.sort(key=lambda v: v["games"], reverse=True)
    return result


MIN_PA_FOR_PARK_LEADER = 10  # small-sample guard for "top hitters at this park" -- one park, one season


def build_park_detail(venue_name, season_year=None):
    """In-depth stats for ONE specific park, beyond what the leaderboard
    row has room for:

    {
      "venue": str, "overall": totals, "home": totals, "away": totals,
      "pitching": {"outs", "ip", "h", "r", "bb", "so", "hr", "bf",
                    "era", "whip", "avg_against"},
      "top_avg": [{"playerId", "name", "team", "pa", "avg", "hr"}, ...],
      "top_hr": [...same shape, sorted by HR...],
      "games": [{"date", "home_team", "away_team", "home_score",
                  "away_score", "public_game_id"}, ...],
    }

    "home" / "away" totals are from the perspective of whichever team
    is actually designated home/away for each specific game played
    there -- i.e. does batting with the last-licks/home-field
    advantage at THIS park look any different than batting on the
    road there, not a specific team's split. "pitching" is every
    defensive half-inning at this park combined (both teams' pitching
    staffs), the park's own runs-allowed environment.

    Returns None if this venue has no completed games on record at
    all (distinct from an empty-but-valid park -- lets the caller
    show a clean 404 rather than an all-zeroes page for a venue name
    that doesn't actually exist in the schedule)."""
    overall = _blank()
    home = _blank()
    away = _blank()
    pitching = {"outs": 0, "h": 0, "r": 0, "bb": 0, "so": 0, "hr": 0, "bf": 0}
    players = {}  # player_id -> {"name", "team", totals}
    games = []
    found_any_game = False

    for g in gamelog._all_games(season_year):
        if gamelog._field(g, "status", default="") != "completed":
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

        venue = gameday.get_venue(gd) or "Unknown Venue"
        if venue != venue_name:
            continue
        found_any_game = True
        overall["games"] += 1

        home_team = gamelog._field(g, "homeTeam", "home_team", "home-team")
        away_team = gamelog._field(g, "awayTeam", "away_team", "away-team")
        home_score = 0
        away_score = 0

        for ab in gameday.get_at_bats(gd):
            if not ab.get("isComplete"):
                continue
            outcome = ab.get("outcome") or ""
            runs = len(ab.get("runsScored") or [])
            half = ab.get("halfInning") or "top"
            is_home_batting = half == "bottom"

            _accumulate_batting(overall, ab)
            _accumulate_batting(home if is_home_batting else away, ab)
            if is_home_batting:
                home_score += runs
            else:
                away_score += runs

            batter_id = ab.get("batterId")
            if batter_id:
                entry = players.setdefault(batter_id, {
                    "playerId": batter_id, "name": None,
                    "team": (home_team if is_home_batting else away_team),
                    "totals": _blank(),
                })
                _accumulate_batting(entry["totals"], ab)

            pitching["bf"] += 1
            pitching["outs"] += ab.get("outsRecorded") or 0
            if outcome in gameday.HIT_OUTCOMES:
                pitching["h"] += 1
                if outcome == "home_run":
                    pitching["hr"] += 1
            elif outcome in gameday.WALK_OUTCOMES:
                pitching["bb"] += 1
            if outcome in gameday.STRIKEOUT_OUTCOMES:
                pitching["so"] += 1
            pitching["r"] += runs

        games.append({
            "date": gamelog._field(g, "gameDate", "game_date", "game-date", default=""),
            "home_team": home_team, "away_team": away_team,
            "home_score": home_score, "away_score": away_score,
            "public_game_id": public_id,
        })

    if not found_any_game:
        return None

    # Resolve player names from the season batting rows (cheap, single
    # already-cached fetch) rather than re-reading each game's roster.
    try:
        all_batting = cbl_api.get_batting(season_year)
    except Exception:
        all_batting = []
    name_lookup = {r.get("playerId"): r.get("fullName") for r in all_batting if r.get("playerId")}
    for entry in players.values():
        entry["name"] = name_lookup.get(entry["playerId"]) or entry["playerId"]
        _finalize_batting(entry["totals"])

    qualified = [e for e in players.values() if e["totals"]["pa"] >= MIN_PA_FOR_PARK_LEADER]

    def _player_row(e):
        return {
            "playerId": e["playerId"], "name": e["name"], "team": e["team"],
            "pa": e["totals"]["pa"], "avg": e["totals"]["avg"], "hr": e["totals"]["hr"],
        }

    top_avg = sorted(qualified, key=lambda e: e["totals"]["avg"] or 0, reverse=True)[:5]
    top_hr = sorted(players.values(), key=lambda e: e["totals"]["hr"] or 0, reverse=True)[:5]
    top_hr = [e for e in top_hr if e["totals"]["hr"] > 0]

    outs = pitching["outs"]
    pitching["ip"] = f"{outs // 3}.{outs % 3}"
    pitching["era"] = stats.safe_div(pitching["r"] * 9, outs / 3) if outs else None
    pitching["whip"] = stats.safe_div(pitching["h"] + pitching["bb"], outs / 3) if outs else None
    ab_against = max(pitching["bf"] - pitching["bb"], 0)
    pitching["avg_against"] = stats.safe_div(pitching["h"], ab_against)

    games.sort(key=lambda gm: gm["date"])

    return {
        "venue": venue_name,
        "overall": _finalize_batting(overall),
        "home": _finalize_batting(home),
        "away": _finalize_batting(away),
        "pitching": pitching,
        "top_avg": [_player_row(e) for e in top_avg],
        "top_hr": [_player_row(e) for e in top_hr],
        "games": games,
    }

