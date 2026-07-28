"""
Season-long split tables for one pitcher, mirroring splits.py's batter
splits but filtered by pitcherId instead of batterId: baserunner state,
outs, inning, home/away, day/night, and month.

Built the same way splits.py/gamelog.py build batter splits: walk every
completed game for the pitcher's team (gamelog.team_games), pull each
game's raw at-bat log (cbl_api.get_gameday, already cached), and keep
only the at-bats where this player is the pitcher. No new network
surface.

Platoon splits (vs L/R batters) are NOT built here: the at-bat log has
no batter-handedness field the way splits.py can lean on CBL's
`/players/<id>/analytics` endpoint for hitters' vs-pitcher-handedness
splits. If CBL's analytics endpoint returns an equivalent split for
pitchers (e.g. `byBatterHandedness`) it's rendered directly from
`event_pitching.splits` in player.html, the same way the hitter page
already renders `event_batting.splits` generically -- this module
doesn't fabricate it.

Earned vs. unearned runs: same caveat as gameday.py/README -- the feed
only exposes total runs allowed (R) while a pitcher was on the mound,
not which were earned. So there's no true per-split ERA here; the "ERA"
column below is actually runs-allowed-per-9 (R rather than ER), labeled
as such everywhere it's displayed. This matches the existing box score
convention in gameday.py/game.html.

Known gaps (same as splits.py):
  - No stolen base / caught stealing data.
  - "Late & Close" isn't computed (would need inning-by-inning score
    differential, which isn't tracked anywhere in this app).
  - Opponent AVG/OBP/SLG/OPS here treat plate appearances against as
    AB + BB (HBP/SF aren't broken out), matching the convention already
    used by gamelog.py and splits.py for hitters.
"""
from collections import OrderedDict
from datetime import date, datetime

import cbl_api
import gameday
import gamelog
import stats

COUNTING_KEYS = ("bf", "outs", "h", "r", "er", "hr", "bb", "so")

OUTS_LABELS = OrderedDict([(0, "No Outs"), (1, "One Out"), (2, "Two Outs")])

_BASE_STATE_MAP = splits_base_state_map = {
    (): "empty",
    ("first",): "1st",
    ("second",): "2nd",
    ("third",): "3rd",
    ("first", "second"): "1st_2nd",
    ("first", "third"): "1st_3rd",
    ("second", "third"): "2nd_3rd",
    ("first", "second", "third"): "loaded",
}

BASE_STATE_LABELS = OrderedDict([
    ("empty", "Bases Empty"),
    ("loaded", "Bases Loaded"),
    ("1st", "Runner at 1st"),
    ("1st_2nd", "Runners at 1st & 2nd"),
    ("1st_3rd", "Runners at 1st & 3rd"),
    ("2nd", "Runner at 2nd"),
    ("2nd_3rd", "Runners at 2nd & 3rd"),
    ("3rd", "Runner at 3rd"),
])


def _blank():
    return {k: 0 for k in COUNTING_KEYS}


def _base_state_key(before):
    before = before or {}
    on = tuple(sorted(b for b in ("first", "second", "third") if before.get(b)))
    return _BASE_STATE_MAP.get(on, "empty")


def _is_night(game_time):
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


_COUNT_STRIKE_RESULTS = ("called_strike", "swinging_strike")
_COUNT_FOUL_RESULTS = ("foul", "foul_bunt")

_CANONICAL_COUNTS = [
    "0-0", "0-1", "0-2",
    "1-0", "1-1", "1-2",
    "2-0", "2-1", "2-2",
    "3-0", "3-1", "3-2",
]


def _final_count(pitches):
    """Ball-strike count (e.g. '3-2', '0-2') the batter was actually
    facing when the plate appearance's LAST pitch was thrown -- same
    logic as splits.py's own _final_count, kept as a local copy here
    rather than a cross-module import of another module's private
    helper (matching this app's established pattern elsewhere, e.g.
    stadiums.py's own local _is_night)."""
    if not pitches:
        return None
    balls = 0
    strikes = 0
    for i, p in enumerate(pitches):
        if i == len(pitches) - 1:
            return f"{balls}-{strikes}"
        result = (p.get("result") or "").lower()
        if result == "ball":
            balls += 1
        elif result in _COUNT_STRIKE_RESULTS:
            strikes = min(strikes + 1, 2)
        elif result in _COUNT_FOUL_RESULTS:
            if strikes < 2:
                strikes += 1
    return f"{balls}-{strikes}"


def _accumulate(totals, ab):
    outcome = ab.get("outcome") or ""
    totals["bf"] += 1
    totals["outs"] += ab.get("outsRecorded") or 0
    totals["r"] += len(ab.get("runsScored") or [])
    if outcome in gameday.HIT_OUTCOMES:
        totals["h"] += 1
        if outcome == "home_run":
            totals["hr"] += 1
    elif outcome == "walk":
        totals["bb"] += 1
    if outcome in gameday.STRIKEOUT_OUTCOMES:
        totals["so"] += 1


def _rates(t):
    outs = t.get("outs") or 0
    ip = outs / 3
    bf = t.get("bf") or 0
    h = t.get("h") or 0
    bb = t.get("bb") or 0
    er = t.get("er")
    if er is None:
        er = t.get("r") or 0  # fallback for any totals dict built without the er key set
    ab_against = max(bf - bb, 0)  # HBP/SF not tracked, see module docstring
    era = stats.safe_div(er * 9, ip) if ip else 0.0
    whip = stats.safe_div(h + bb, ip) if ip else None
    avg_against = stats.safe_div(h, ab_against)
    return {
        "ip": f"{outs // 3}.{outs % 3}",
        "ip_float": ip,
        "era": era,  # earned-run based when era_exact is True on the row -- see build_pitcher_game_log
        "whip": whip,
        "avg_against": avg_against,
    }


def _finalize(totals):
    totals.update(_rates(totals))
    return totals


def _pitcher_started_game(gd, team_name, is_home, player_id):
    """A pitcher "started" if they threw the first pitch of the game for
    their side -- i.e. they're the pitcherId on the first at-bat of the
    half-inning their team is on defense. No separate "starter" flag
    exists on the pitcher-facing at-bat records, so this is derived
    rather than read directly."""
    at_bats = gameday.get_at_bats(gd)
    defense_half = "top" if is_home else "bottom"
    for ab in at_bats:
        if (ab.get("inning") or 1) == 1 and (ab.get("halfInning") or "top") == defense_half:
            return ab.get("pitcherId") == player_id
    return False


def _sum_counting(dest, src):
    """Sums COUNTING_KEYS fields from an already-aggregated per-game `src`
    row into `dest` -- NOT the same as _accumulate() above, which reads
    raw per-at-bat fields (outcome/outsRecorded/runsScored) instead of
    pre-summed totals. Used for rolling per-game rows up into month/
    season totals."""
    for key in COUNTING_KEYS:
        dest[key] = dest.get(key, 0) + (src.get(key) or 0)


def build_pitcher_game_log(player_id, team_name, season_year=None):
    """
    {
      "months": OrderedDict(month_name -> {"rows": [...], "totals": {...}}),
      "season_totals": {...},
    }
    Each row/totals dict has bf/outs/h/r/er/hr/bb/so plus ip/era/whip/
    avg_against. ERA here uses CBL's own per-game earned-run total
    (playerPitchingStats[pid].earnedRuns, confirmed to exist in a real
    payload) whenever it's available for that game, NOT the runs-
    allowed proxy this module uses elsewhere -- this is real,
    earned-run-based ERA, not an approximation, for any row where
    era_exact is True. Falls back to treating all runs as earned only
    for the rare game missing that field, and era_exact is False on
    exactly those rows (and on any month/season total that includes
    one) so the UI can flag it correctly. Rows additionally carry date,
    opponent, is_home, venue, umpire, and public_game_id.

    Scope limit worth knowing: this fix only applies at the WHOLE-GAME
    level, because that's the granularity CBL's own earned-run field
    is at. The situational splits built elsewhere in this module
    (build_pitcher_splits -- by baserunner state, inning, outs, etc.)
    necessarily span partial games, and there's no way to attribute a
    whole game's earned-run total to a subset of its at-bats -- those
    splits still use the runs-allowed proxy and still carry the "ERA*"
    caveat for that reason.

    Totals are derived by summing the actual rows collected here, the
    same self-consistency guarantee gamelog.build_player_game_log uses
    (see that function's docstring for why) -- a monthly or season
    total can never disagree with what's actually shown for that
    month/season.
    """
    months = OrderedDict()

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

        home = gamelog._field(g, "homeTeam", "home_team", "home-team")
        away = gamelog._field(g, "awayTeam", "away_team", "away-team")
        is_home = gamelog.team_matches(home, team_name)
        opponent = away if is_home else home

        totals = _blank()
        appeared = False
        for ab in gameday.get_at_bats(gd):
            if ab.get("pitcherId") == player_id and ab.get("isComplete"):
                appeared = True
                _accumulate(totals, ab)
        if not appeared:
            continue  # player didn't pitch in this game

        # CBL's own per-game playerPitchingStats separates earned from
        # total runs allowed (confirmed in a real payload) -- use that
        # directly when available rather than the at-bat-derived "r"
        # count, which (a) can't distinguish earned from unearned at
        # all, the reason ERA here used to be flagged with an asterisk,
        # and (b) has a known gap of its own: it doesn't include runs
        # that scored via a wild pitch/passed ball/steal-of-home
        # (see gameday.get_extra_scoring_events, used elsewhere for the
        # line score but not wired into this at-bat walk). CBL's own
        # total is authoritative for both counts when present.
        era_exact = False
        pp_stats = gameday.get_player_pitching_stats(gd, player_id)
        if pp_stats and pp_stats.get("earnedRuns") is not None:
            totals["er"] = pp_stats["earnedRuns"]
            if pp_stats.get("runs") is not None:
                totals["r"] = pp_stats["runs"]
            era_exact = True
        else:
            totals["er"] = totals["r"]  # fallback: no per-game ER available, treat all runs as earned

        date_str = gamelog._field(g, "gameDate", "game_date", "game-date", default="")
        row = {"date": date_str, "opponent": opponent, "is_home": is_home,
               "public_game_id": public_id, "venue": gameday.get_venue(gd),
               "umpire": gameday.get_home_plate_umpire(gd), "era_exact": era_exact}
        row.update(totals)
        row.update(_rates(totals))

        bucket = months.setdefault(gamelog._month_name(date_str), {"rows": []})
        bucket["rows"].append(row)

    season_totals = _blank()
    season_era_exact = True
    for bucket in months.values():
        month_totals = _blank()
        month_era_exact = True
        for row in bucket["rows"]:
            _sum_counting(month_totals, row)
            _sum_counting(season_totals, row)
            if not row.get("era_exact"):
                month_era_exact = False
                season_era_exact = False
        month_totals.update(_rates(month_totals))
        month_totals["era_exact"] = month_era_exact
        bucket["totals"] = month_totals

    season_totals.update(_rates(season_totals))
    season_totals["era_exact"] = season_era_exact
    return {"months": months, "season_totals": season_totals}


def build_pitcher_start_totals(player_id, team_name, season_year=None):
    """
    {"starts": int, "quality_starts": int}

    "starts" is derived here (first pitch of the game thrown by this
    pitcher for their side) -- CBL's own per-game `starts` counter on
    playerPitchingStats has been observed NOT populated even for
    confirmed starting pitchers, so it can't be trusted directly.

    "quality_starts" prefers CBL's own per-game `qualityStarts` field on
    playerPitchingStats when present -- a true earned-run-based number,
    confirmed against real data, better than this module's own IP/runs-
    allowed proxy (which can't distinguish earned from unearned runs).
    Falls back to the 6+ IP / <=3-runs-allowed proxy only if that field
    is missing from a given game's payload.
    """
    starts = 0
    quality_starts = 0

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

        home = gamelog._field(g, "homeTeam", "home_team", "home-team")
        is_home = gamelog.team_matches(home, team_name)

        if not _pitcher_started_game(gd, team_name, is_home, player_id):
            continue
        starts += 1

        pstats = gameday.get_player_pitching_stats(gd, player_id)
        if pstats is not None and "qualityStarts" in pstats:
            quality_starts += pstats.get("qualityStarts") or 0
            continue

        totals = _blank()
        for ab in gameday.get_at_bats(gd):
            if ab.get("pitcherId") == player_id and ab.get("isComplete"):
                _accumulate(totals, ab)
        outs = totals["outs"]
        if outs >= 18 and totals["r"] <= 3:  # 6.0 IP, <=3 runs allowed (proxy fallback only)
            quality_starts += 1

    return {"starts": starts, "quality_starts": quality_starts}


def build_pitcher_splits(player_id, team_name, season_year=None):
    """
    Returns an OrderedDict of section_name -> list of (label, totals) rows:
      "Monthly Splits", "Baserunner Splits", "Outs Splits",
      "Inning Splits", "Game Type Splits"
    Each `totals` dict has bf/outs/h/r/hr/bb/so plus ip/era/whip/avg_against
    (era here is runs-allowed-per-9, not true earned-run ERA -- see
    module docstring).
    """
    months = OrderedDict()
    baserunners = OrderedDict((k, _blank()) for k in BASE_STATE_LABELS)
    risp = _blank()
    counts = {}
    outs = OrderedDict((k, _blank()) for k in OUTS_LABELS)
    innings = OrderedDict()
    game_type = OrderedDict([
        ("home", _blank()), ("away", _blank()),
        ("day", _blank()), ("night", _blank()),
    ])

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

        home = gamelog._field(g, "homeTeam", "home_team", "home-team")
        away = gamelog._field(g, "awayTeam", "away_team", "away-team")
        is_home = gamelog.team_matches(home, team_name)

        date_str = gamelog._field(g, "gameDate", "game_date", "game-date", default="")
        game_time = gamelog._field(g, "gameTime", "game_time", "game-time", default="")
        is_night = _is_night(game_time)

        at_bats = gameday.get_at_bats(gd)
        outs_before = {}

        for ab in at_bats:
            inning = ab.get("inning") or 1
            half = ab.get("halfInning") or "top"
            inning_key = (inning, half)
            cur_outs = outs_before.get(inning_key, 0)

            if ab.get("pitcherId") == player_id and ab.get("isComplete"):
                month = months.setdefault(gamelog._month_name(date_str), _blank())
                _accumulate(month, ab)

                _accumulate(baserunners[_base_state_key(ab.get("baseRunnersBeforePlay"))], ab)
                if ab.get("runnersInScoringPosition"):
                    _accumulate(risp, ab)
                final_count = _final_count(ab.get("pitches"))
                if final_count:
                    _accumulate(counts.setdefault(final_count, _blank()), ab)

                _accumulate(outs[min(cur_outs, 2)], ab)

                inning_label = f"{inning}{'st' if inning == 1 else 'nd' if inning == 2 else 'rd' if inning == 3 else 'th'} Inning" if inning <= 9 else "Extra Innings"
                inning_bucket = innings.setdefault(inning_label, _blank())
                _accumulate(inning_bucket, ab)

                _accumulate(game_type["home" if is_home else "away"], ab)
                if is_night is not None:
                    _accumulate(game_type["night" if is_night else "day"], ab)

            outs_before[inning_key] = cur_outs + (ab.get("outsRecorded") or 0)

    for bucket in (months, innings):
        for totals in bucket.values():
            _finalize(totals)
    for totals in baserunners.values():
        _finalize(totals)
    _finalize(risp)
    for totals in counts.values():
        _finalize(totals)
    for totals in outs.values():
        _finalize(totals)
    for totals in game_type.values():
        _finalize(totals)

    baserunner_rows = [(label, baserunners[key]) for key, label in BASE_STATE_LABELS.items()]
    baserunner_rows.append(("Scoring Position", risp))

    return OrderedDict([
        ("Monthly Splits", list(months.items())),
        ("Baserunner Splits", baserunner_rows),
        ("Outs Splits", [(label, outs[key]) for key, label in OUTS_LABELS.items()]),
        ("Inning Splits", list(innings.items())),
        ("Game Type Splits", [
            ("Home Games", game_type["home"]),
            ("Away Games", game_type["away"]),
            ("Day Games", game_type["day"]),
            ("Night Games", game_type["night"]),
        ]),
        ("Count Splits", [
            (count, counts[count]) for count in _CANONICAL_COUNTS if count in counts
        ]),
    ])


def build_pitcher_park_splits(pitcher_game_log, team_name):
    """Same idea as gamelog.build_park_splits, for a pitcher's own game
    log (see build_pitcher_game_log). No new network calls -- just
    re-buckets rows already fetched for the pitcher's Game Log tab.
    Inherits that game log's real earned-run ERA (era_exact tracked
    the same way: True only if every game bucketed into a park had
    CBL's own per-game earned-run data)."""
    parks = OrderedDict()

    for bucket in (pitcher_game_log or {}).get("months", {}).values():
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
        totals = _blank()
        era_exact = True
        for row in data["rows"]:
            _sum_counting(totals, row)
            if not row.get("era_exact"):
                era_exact = False
        totals.update(_rates(totals))
        totals["park"] = park
        totals["games"] = len(data["rows"])
        totals["era_exact"] = era_exact
        result.append(totals)

    result.sort(key=lambda p: p["games"], reverse=True)
    return result


def build_pitcher_umpire_splits(pitcher_game_log, min_games=1):
    """Same idea as build_pitcher_park_splits, grouped by home plate
    umpire instead of park. No new network calls. Games with no
    identifiable umpire are excluded rather than lumped into an
    "Unknown" bucket. Inherits the game log's real earned-run ERA the
    same way build_pitcher_park_splits does."""
    umpires = OrderedDict()

    for bucket in (pitcher_game_log or {}).get("months", {}).values():
        for row in bucket["rows"]:
            ump = row.get("umpire")
            if not ump:
                continue
            umpires.setdefault(ump, {"rows": []})["rows"].append(row)

    result = []
    for ump, data in umpires.items():
        if len(data["rows"]) < min_games:
            continue
        totals = _blank()
        era_exact = True
        for row in data["rows"]:
            _sum_counting(totals, row)
            if not row.get("era_exact"):
                era_exact = False
        totals.update(_rates(totals))
        totals["umpire"] = ump
        totals["games"] = len(data["rows"])
        totals["era_exact"] = era_exact
        result.append(totals)

    result.sort(key=lambda p: p["games"], reverse=True)
    return result


def build_pitcher_scoreless_streak(pitcher_game_log):
    """Current streak of consecutive most-recent appearances with zero
    runs allowed. Uses total runs ("r"), not earned-only ("er") --
    "scoreless" means literally no runs scored in that outing, period,
    regardless of earned/unearned.

    Relies on pitcher_game_log's rows already being in chronological
    order (guaranteed by gamelog.team_games()'s explicit date/time sort,
    which every month bucket here is built by walking in that same
    order) -- flattens every month's rows into one list and counts
    backward from the most recent appearance. No new network calls:
    this just re-reads rows already fetched for the Game Log tab.

    Returns 0 if the most recent appearance allowed any runs, or if
    there's no game log data at all -- not None, since "no streak" and
    "no data" both display the same way (nothing to show), and callers
    checking pitcher_game_log's own presence already handle the "no
    data" case."""
    rows = []
    for bucket in (pitcher_game_log or {}).get("months", {}).values():
        rows.extend(bucket["rows"])
    if not rows:
        return 0
    streak = 0
    for row in reversed(rows):
        if (row.get("r") or 0) == 0:
            streak += 1
        else:
            break
    return streak


BULLPEN_AVAILABILITY_DAYS = 3


def build_bullpen_availability(team_name, days=BULLPEN_AVAILABILITY_DAYS):
    """Every pitcher who's appeared IN RELIEF for this team in the last
    `days` days, with days-since and pitch count from that appearance
    -- lets a broadcaster speak to who's actually available tonight.
    Walks only the team's most recent few games (not a full-season walk
    per pitcher, which is what this would cost if done the same way
    build_pitcher_game_log walks one player's whole season) -- reads
    each game's own box score (gameday.build_pitching_box) instead.

    Excludes whoever STARTED each game -- a rotation starter isn't a
    "bullpen" arm, and listing them here would incorrectly suggest a
    starter is "available" out of the pen when they're actually on a
    normal starts-every-few-days rotation, not a day-to-day relief
    workload. Identified the same way _pitcher_started_game does
    elsewhere in this module: whoever appears FIRST in that game's own
    pitching box for this team's side (build_pitching_box's own
    appearance-order list is already in the order pitchers actually
    entered the game).

    If a pitcher appears in more than one of the last few days' games
    (unusual but possible in a short window), only their MOST RECENT
    appearance is kept -- a broadcaster cares about "when did they
    last pitch and how many pitches," not a sum across appearances.

    Returns a list of {"player_id", "name", "days_ago", "pitches"}
    dicts, most recent appearance first. Empty list if the team has no
    games in the window, or nothing could be read for any reason."""
    games = list(gamelog.team_games(team_name))
    if not games:
        return []
    today = date.today()

    appearances = {}
    for g in reversed(games):  # team_games() sorts ascending; walk newest-first
        game_date_str = gamelog._field(g, "gameDate", "game_date", "game-date", default="")
        try:
            game_date = datetime.strptime(game_date_str, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            continue
        days_ago = (today - game_date).days
        if days_ago < 0:
            continue  # a future/unplayed game somehow in the schedule; skip
        if days_ago > days:
            break  # walking newest-first, so everything from here on is even older

        public_id = gamelog._field(g, "publicGameId", "public_game_id", "public-game-id")
        if not public_id:
            continue
        try:
            gd = cbl_api.get_gameday(public_id)
        except Exception:
            continue
        if not gd or not gd.get("snapshot"):
            continue

        home = gamelog._field(g, "homeTeam", "home_team", "home-team")
        is_home = gamelog.team_matches(home, team_name)
        side = "home" if is_home else "away"

        try:
            lookup = gameday.build_player_lookup(gd)
            box = gameday.build_pitching_box(gd, lookup)
        except Exception:
            continue

        side_rows = box.get(side) or []
        if not side_rows:
            continue
        starter_id = side_rows[0].get("playerId")  # first to appear = started this game

        for row in side_rows:
            pid = row.get("playerId")
            if not pid or pid == starter_id:
                continue  # exclude the starter -- rotation arm, not a bullpen question
            if pid in appearances:
                continue  # already recorded a MORE recent appearance for this pitcher
            appearances[pid] = {
                "player_id": pid, "name": row.get("name") or pid,
                "days_ago": days_ago, "pitches": row.get("pitches") or 0,
            }

    return sorted(appearances.values(), key=lambda e: e["days_ago"])
