"""
Team-level win/loss record and situational winning-percentage splits,
built by walking every completed game for a team (gamelog.team_games,
same schedule-walking pattern as gamelog.py/splits.py) and reading each
game's line score (gameday.build_line_score, which itself reads
gameday.get_at_bats -- no new network surface, everything here rides on
already-cached gameday fetches).

Everything here is derived from real per-inning run totals -- nothing
is invented. Two things worth knowing:

  - Pythagorean win % uses exponent 1.83 (a common refinement of Bill
    James's original exponent-2 formula), not a value fit to this
    league specifically -- there isn't enough of a sample size in a
    summer league season to derive a league-specific exponent reliably.
  - "Scored first" / "leading after N innings" are read directly off
    the inning-by-inning line score built for the Gameday page, so they
    reflect exactly what's shown there.

Ties are skipped (shouldn't happen in baseball, but guards against a
data-entry oddity crashing this rather than just excluding that game).
"""
import time
from collections import OrderedDict

import cbl_api
import gameday
import gamelog
import transactions

PYTHAGOREAN_EXPONENT = 1.83


def count_completed_games_by_team(season_year=None):
    """{normalized_team_name: completed_game_count} for every team, in
    ONE pass over the season schedule -- no per-game network calls
    needed (unlike build_team_season_record, which needs each game's
    actual line score to compute wins/losses/streaks, so has to fetch
    every game's full gameday JSON).

    Keyed by gamelog._normalize()'d team name, NOT the raw string --
    CBL's schedule data is hand-entered and known to spell the same
    team differently in different places (that's exactly why
    _normalize exists: the "Chatham-Kent" vs "Chatham Kent" issue).
    A raw-string dict here would silently fail to match whenever the
    season stats feed spells a team name even slightly differently
    than the schedule feed does, quietly falling back to whatever the
    caller does when a lookup misses -- use games_for_team() below to
    look values up correctly rather than indexing this dict directly.

    Use this specifically when all you need is "how many games has
    this team played" -- the correct denominator for any team-level
    per-game rate stat (Runs/Game, Runs Allowed/Game, etc.). NOT any
    individual player's own games-played count: a single player almost
    never appears in every team game (pitchers especially, but even
    everyday position players get rest days), so using one player's
    count as a stand-in for the team's count badly inflates every
    per-game rate. See analytics.py's team_batting_stats /
    team_pitching_stats, which used to do exactly that.
    """
    counts = {}
    for g in gamelog._all_games(season_year):
        status = gamelog._field(g, "status", default="")
        if status != "completed":
            continue
        home = gamelog._field(g, "homeTeam", "home_team", "home-team")
        away = gamelog._field(g, "awayTeam", "away_team", "away-team")
        if home:
            key = gamelog._normalize(home)
            counts[key] = counts.get(key, 0) + 1
        if away:
            key = gamelog._normalize(away)
            counts[key] = counts.get(key, 0) + 1
    return counts


def games_for_team(team_games_counts, team_name):
    """Correct way to read a value out of count_completed_games_by_team()'s
    result -- normalizes team_name the same way the dict's keys were
    built, so a spelling difference between the schedule feed and
    wherever team_name came from (season stats, a roster page, etc.)
    doesn't cause a silent lookup miss. Returns None if there's truly
    no entry for this team once normalized."""
    if not team_games_counts:
        return None
    return team_games_counts.get(gamelog._normalize(team_name))

_standings_cache = {}  # tuple(sorted(team_names)) -> (timestamp, records)
STANDINGS_CACHE_TTL = 300  # 5 minutes -- see build_standings()


def safe_div(n, d):
    return (n / d) if d else None


def _team_side(g, team_name):
    home = gamelog._field(g, "homeTeam", "home_team", "home-team")
    return "home" if gamelog._normalize(home) == gamelog._normalize(team_name) else "away"


def get_active_roster_pitchers(team_name, season_year=None):
    """Pitchers on `team_name`'s active roster right now -- CBL doesn't
    expose a dedicated "current roster" endpoint, so this uses the
    closest real proxy: the roster CBL itself attached to the team's
    MOST RECENT game (gamelog.team_games already sorts ascending by
    date, so the last entry is the most recent). That roster reflects
    who was actually eligible/rostered as of the last time this team
    played -- naturally excludes anyone who's since been released or
    left, and includes anyone recently added, unlike season stat rows
    (which persist all season regardless of current roster status).

    Also cross-checked against CBL's own transaction log (see
    transactions.py) as a safety net: a pitcher is excluded here if
    their most recent logged transaction shows them released,
    inactive, injured, traded away, or traded out of the league
    entirely -- even if they were still on the roster as of their last
    game (e.g. released the day after). This match is by NAME (the
    transaction feed has no player ID), case-insensitive and exact
    only -- a name that doesn't match anything in the transaction feed
    just isn't filtered, never incorrectly removed; a failed or empty
    transaction fetch degrades to "no extra filtering" the same way,
    never to an empty roster.

    Returns a list of {"id", "name"} dicts, sorted by name, filtered to
    roster entries whose listed position is "P". Empty list if the team
    has no games on record yet, or the most recent game's roster data
    isn't available for any reason -- never guesses at roster
    membership from season stats as a fallback, since a player with a
    pitching stat line from April isn't necessarily still on the team
    in July."""
    games = list(gamelog.team_games(team_name, season_year))
    if not games:
        return []
    most_recent = games[-1]
    public_id = gamelog._field(most_recent, "publicGameId", "public_game_id", "public-game-id")
    if not public_id:
        return []
    try:
        gd = cbl_api.get_gameday(public_id)
    except Exception:
        return []
    if not gd or not gd.get("snapshot"):
        return []

    side_key = "homeTeam" if _team_side(most_recent, team_name) == "home" else "awayTeam"
    roster = (gd["snapshot"].get(side_key) or {}).get("roster") or []
    pitchers = [
        {"id": p.get("id"), "name": p.get("name")}
        for p in roster if (p.get("position") or "").strip().upper() == "P" and p.get("id") and p.get("name")
    ]

    off_roster_statuses = {
        transactions.STATUS_RELEASED, transactions.STATUS_INACTIVE, transactions.STATUS_INJURED,
        transactions.STATUS_TRADED_AWAY, transactions.STATUS_LEFT_LEAGUE,
    }
    try:
        tx_status = transactions.build_roster_status(season_year)
    except Exception:
        tx_status = {}
    if tx_status:
        pitchers = [
            p for p in pitchers
            if (tx_status.get(p["name"].strip().lower()) or {}).get("status") not in off_roster_statuses
        ]

    return sorted(pitchers, key=lambda p: p["name"].lower())


def build_team_season_record(team_name, season_year=None):
    """
    {
      "games": int, "wins": int, "losses": int, "win_pct": float|None,
      "loss_pct": float|None,
      "runs_scored": int, "runs_allowed": int, "run_differential": int,
      "pythagorean_win_pct": float|None,
      "expected_wins": float|None,  -- pythagorean_win_pct * games
      "expected_losses": float|None,  -- games - expected_wins
      "wins_above_expected": float|None,  -- actual wins minus expected_wins;
        positive means the team has won more than their runs scored/
        allowed would predict ("gotten lucky" in the sabermetric sense --
        typically from close-game performance), negative means fewer.
      "one_run_games"/"scored_first_games"/"allowed_first_games"/
      "lead_after_5_games"/"lead_after_7_games": int,
      matching "..._win_pct" and "..._loss_pct" (float|None, and
      loss_pct = 1 - win_pct for that split -- there are no ties, so
      this is exact, not an independent count).
      "game_results": [{"date","opponent","is_home","team_runs","opp_runs",
        "result" ("W"/"L"), "public_game_id", "umpire" (home plate
        umpire's name, or None if not identifiable for that game --
        see gameday.get_home_plate_umpire)}, ...] -- every game actually
        counted toward the record above, in schedule order. This is the
        audit trail: if the record looks wrong, this list is exactly
        what this app found and scored, so a missing or extra date shows
        up immediately by inspection instead of by guessing at another
        schedule-matching edge case blind.
      "diagnostics": {"schedule_entries_found", "skipped_no_public_id",
        "skipped_fetch_failed", "skipped_tie"} -- counts of *why* a
        schedule entry for this team didn't end up contributing to the
        record, for the same reason.
    }
    """
    games = wins = losses = 0
    runs_scored = runs_allowed = 0
    one_run_games = one_run_wins = 0
    scored_first_games = scored_first_wins = 0
    allowed_first_games = allowed_first_wins = 0
    lead5_games = lead5_wins = 0
    lead7_games = lead7_wins = 0
    game_results = []
    diagnostics = {"schedule_entries_found": 0, "skipped_no_public_id": 0,
                    "skipped_fetch_failed": 0, "skipped_tie": 0}

    schedule = gamelog.team_games(team_name, season_year)
    diagnostics["schedule_entries_found"] = len(schedule)

    for g in schedule:
        public_id = gamelog._field(g, "publicGameId", "public_game_id", "public-game-id")
        if not public_id:
            diagnostics["skipped_no_public_id"] += 1
            continue
        try:
            gd = cbl_api.get_gameday(public_id)
        except Exception:
            diagnostics["skipped_fetch_failed"] += 1
            continue
        if not gd or not gd.get("snapshot"):
            diagnostics["skipped_fetch_failed"] += 1
            continue

        side = _team_side(g, team_name)
        opp_side = "away" if side == "home" else "home"
        line = gameday.build_line_score(gd)
        team_block = line[side]
        opp_block = line[opp_side]

        team_runs = team_block["runs"]
        opp_runs = opp_block["runs"]
        if team_runs == opp_runs:
            diagnostics["skipped_tie"] += 1
            continue  # tie -- shouldn't happen, guard rather than crash

        games += 1
        runs_scored += team_runs
        runs_allowed += opp_runs
        won = team_runs > opp_runs
        if won:
            wins += 1
        else:
            losses += 1

        home = gamelog._field(g, "homeTeam", "home_team", "home-team")
        away = gamelog._field(g, "awayTeam", "away_team", "away-team")
        game_results.append({
            "date": gamelog._field(g, "gameDate", "game_date", "game-date", default=""),
            "opponent": away if side == "home" else home,
            "is_home": side == "home",
            "team_runs": team_runs, "opp_runs": opp_runs,
            "result": "W" if won else "L",
            "public_game_id": public_id,
            "umpire": gameday.get_home_plate_umpire(gd),
        })

        if abs(team_runs - opp_runs) == 1:
            one_run_games += 1
            if won:
                one_run_wins += 1

        team_line = team_block["line"]
        opp_line = opp_block["line"]

        team_first = next((i + 1 for i, r in enumerate(team_line) if r > 0), None)
        opp_first = next((i + 1 for i, r in enumerate(opp_line) if r > 0), None)
        if team_first is not None and (opp_first is None or team_first < opp_first):
            scored_first_games += 1
            if won:
                scored_first_wins += 1
        elif opp_first is not None and (team_first is None or opp_first < team_first):
            allowed_first_games += 1
            if won:
                allowed_first_wins += 1

        if len(team_line) >= 5 and len(opp_line) >= 5:
            if sum(team_line[:5]) > sum(opp_line[:5]):
                lead5_games += 1
                if won:
                    lead5_wins += 1
        if len(team_line) >= 7 and len(opp_line) >= 7:
            if sum(team_line[:7]) > sum(opp_line[:7]):
                lead7_games += 1
                if won:
                    lead7_wins += 1

    pyth = None
    if runs_scored or runs_allowed:
        rs_exp = runs_scored ** PYTHAGOREAN_EXPONENT
        ra_exp = runs_allowed ** PYTHAGOREAN_EXPONENT
        pyth = safe_div(rs_exp, rs_exp + ra_exp)

    expected_wins = (pyth * games) if pyth is not None else None
    expected_losses = (games - expected_wins) if expected_wins is not None else None
    wins_above_expected = (wins - expected_wins) if expected_wins is not None else None

    def loss_pct_of(win_pct):
        return (1 - win_pct) if win_pct is not None else None

    one_run_win_pct = safe_div(one_run_wins, one_run_games)
    scored_first_win_pct = safe_div(scored_first_wins, scored_first_games)
    allowed_first_win_pct = safe_div(allowed_first_wins, allowed_first_games)
    lead5_win_pct = safe_div(lead5_wins, lead5_games)
    lead7_win_pct = safe_div(lead7_wins, lead7_games)

    return {
        "games": games, "wins": wins, "losses": losses, "win_pct": safe_div(wins, games),
        "loss_pct": safe_div(losses, games),
        "runs_scored": runs_scored, "runs_allowed": runs_allowed,
        "run_differential": runs_scored - runs_allowed,
        "pythagorean_win_pct": pyth,
        "expected_wins": expected_wins,
        "expected_losses": expected_losses,
        "wins_above_expected": wins_above_expected,
        "one_run_games": one_run_games, "one_run_win_pct": one_run_win_pct,
        "one_run_loss_pct": loss_pct_of(one_run_win_pct),
        "scored_first_games": scored_first_games, "scored_first_win_pct": scored_first_win_pct,
        "scored_first_loss_pct": loss_pct_of(scored_first_win_pct),
        "allowed_first_games": allowed_first_games, "allowed_first_win_pct": allowed_first_win_pct,
        "allowed_first_loss_pct": loss_pct_of(allowed_first_win_pct),
        "lead_after_5_games": lead5_games, "lead_after_5_win_pct": lead5_win_pct,
        "lead_after_5_loss_pct": loss_pct_of(lead5_win_pct),
        "lead_after_7_games": lead7_games, "lead_after_7_win_pct": lead7_win_pct,
        "lead_after_7_loss_pct": loss_pct_of(lead7_win_pct),
        "game_results": game_results,
        "current_streak": _current_streak(game_results),
        "diagnostics": diagnostics,
    }


def _current_streak(game_results):
    """"W5" / "L3" / None (no games) -- walks game_results (already in
    chronological schedule order) backward from the most recent game,
    counting consecutive same-result games."""
    if not game_results:
        return None
    last_result = game_results[-1]["result"]
    count = 0
    for g in reversed(game_results):
        if g["result"] != last_result:
            break
        count += 1
    return f"{last_result}{count}"


MIN_GAMES_FOR_UMPIRE_ROW = 1  # every umpire who's worked at least one game shows -- no meaningful "too small" cutoff at the team level


def build_umpire_record(game_results):
    """This team's win/loss record broken out by home plate umpire --
    takes the game_results list build_team_season_record already
    produces (which now carries the umpire per game) directly, rather
    than re-walking the same schedule a second time. Callers that
    don't already have a record on hand can just pass
    build_team_season_record(team_name)["game_results"].

    Games with no identifiable umpire on record are excluded entirely
    from this breakdown, not lumped into an "Unknown" row -- same
    convention gamelog.py's own per-player umpire splits already use
    elsewhere on this site.

    Returns a list of {"umpire", "games", "wins", "losses", "win_pct"}
    dicts, sorted by games worked descending (most-seen umpire first).
    Empty list if there are no games with an identifiable umpire on
    record at all."""
    if not game_results:
        return []

    by_umpire = OrderedDict()
    for g in game_results:
        umpire = g.get("umpire")
        if not umpire:
            continue
        row = by_umpire.setdefault(umpire, {"umpire": umpire, "games": 0, "wins": 0, "losses": 0})
        row["games"] += 1
        if g["result"] == "W":
            row["wins"] += 1
        else:
            row["losses"] += 1

    rows = list(by_umpire.values())
    for row in rows:
        row["win_pct"] = safe_div(row["wins"], row["games"])
    rows.sort(key=lambda r: r["games"], reverse=True)
    return rows


def build_standings(team_names, season_year=None):
    """Every team's season record (see build_team_season_record), sorted
    by win percentage descending, with games-behind-the-leader computed
    for each. Cached in memory for STANDINGS_CACHE_TTL seconds -- this
    walks every team's full schedule, which is expensive the first time
    (though every underlying gameday fetch is already disk-cached, so
    it's cheap on repeat) but there's no reason to redo the whole thing
    on every single page load within a few minutes of the last one."""
    cache_key = tuple(sorted(team_names))
    now = time.time()
    cached = _standings_cache.get(cache_key)
    if cached and (now - cached[0]) < STANDINGS_CACHE_TTL:
        return cached[1]

    records = []
    for team_name in team_names:
        try:
            record = build_team_season_record(team_name, season_year)
        except Exception:
            record = None
        if record and record.get("games"):
            record = dict(record)
            record["team"] = team_name
            records.append(record)

    records.sort(key=lambda r: (r.get("win_pct") if r.get("win_pct") is not None else -1), reverse=True)

    if records:
        leader = records[0]
        for r in records:
            r["games_behind"] = ((leader["wins"] - r["wins"]) + (r["losses"] - leader["losses"])) / 2.0

    _standings_cache[cache_key] = (now, records)
    return records


def clear_standings_cache():
    """Force the next build_standings() call to recompute from scratch --
    useful right after a schedule/name-matching fix, so a stale cached
    standings table doesn't look like the fix didn't work."""
    _standings_cache.clear()

