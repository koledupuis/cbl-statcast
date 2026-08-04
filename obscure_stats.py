"""
Obscure stats -- top-5 leaderboards for things nobody thinks to look
up: an active streak of innings pitched without allowing a home run,
an active scoreless-appearance streak, an active hit streak, and a
few cheap derived rate stats (ISO, BB/K, K/BB) that never get their
own leaderboard elsewhere on this site even though the season rows
already have everything needed to compute them.

Cost profile, stated plainly since this matters for a page like this:
- ISO / BB-K / K-BB are pure season-row arithmetic -- no game-log walk,
  as cheap as any other leaderboard page.
- The three streak stats each need one game-log walk PER QUALIFYING
  PLAYER (bounded by a minimum-playing-time filter applied to the
  season rows FIRST, so this never walks every single player in the
  league regardless of how little they've played -- only those who
  clear the same kind of minimum this app already uses for its other
  "leader" lists). Still meaningfully more expensive than a pure
  season-row page; treated the same way Stadiums treats its own
  full-season walk -- fine for an occasionally-visited page, not
  something to call on every request.
"""
import analytics
import cbl_api
import gameday
import gamelog
import pitching_splits
import rolling
import stats
import transactions

MIN_IP_FOR_STREAK = 10
MIN_PA_FOR_STREAK = 20
MIN_PA_FOR_RATE = 20
MIN_IP_FOR_RATE = 10
TOP_N = 5


def _filter_active(rows):
    """Thin wrapper around transactions.filter_active_players -- kept
    as a local name so every call site in this module doesn't need to
    change, now that the actual filtering logic lives in one shared
    place instead of being duplicated per-module."""
    return transactions.filter_active_players(rows)


def _ip_from_row(row):
    outs = (row.get("advancedPitching") or {}).get("inningsPitchedOuts") or 0
    return outs / 3


def _hr_less_ip_streak_outs(game_log):
    """Total outs recorded across a pitcher's most recent consecutive
    appearances without allowing a home run, walking backward from
    their last outing. 0 if their most recent appearance itself
    included a home run, or there's no game log data at all."""
    game_rows = [r for bucket in (game_log or {}).get("months", {}).values() for r in bucket["rows"]]
    if not game_rows:
        return 0
    total_outs = 0
    for r in reversed(game_rows):
        if (r.get("hr") or 0) > 0:
            break
        total_outs += round((r.get("ip_float") or 0) * 3)
    return total_outs


def build_most_ip_without_hr(limit=TOP_N):
    """Top 5: current active streak of innings pitched without
    allowing a home run, among pitchers with at least MIN_IP_FOR_STREAK
    innings pitched this season. One game-log walk per qualifying
    pitcher (see module docstring on cost)."""
    all_pitching = cbl_api.get_pitching()
    candidates = _filter_active([r for r in all_pitching if _ip_from_row(r) >= MIN_IP_FOR_STREAK])

    results = []
    for row in candidates:
        player_id, team_name = row.get("playerId"), row.get("teamName")
        if not player_id or not team_name:
            continue
        try:
            game_log = pitching_splits.build_pitcher_game_log(player_id, team_name)
        except Exception:
            continue
        outs = _hr_less_ip_streak_outs(game_log)
        if outs <= 0:
            continue
        results.append({
            "playerId": player_id, "name": row.get("fullName"), "team": team_name,
            "value": f"{outs // 3}.{outs % 3}", "sort_key": outs,
        })

    results.sort(key=lambda e: e["sort_key"], reverse=True)
    return results[:limit]


def build_longest_scoreless_streak(limit=TOP_N):
    """Top 5: current active streak of consecutive scoreless
    appearances, among pitchers with at least MIN_IP_FOR_STREAK innings
    pitched this season. Reuses pitching_splits.build_pitcher_scoreless_streak
    -- same function the player page's own game log tab already calls
    per-player, just run across every qualifying pitcher here."""
    all_pitching = cbl_api.get_pitching()
    candidates = _filter_active([r for r in all_pitching if _ip_from_row(r) >= MIN_IP_FOR_STREAK])

    results = []
    for row in candidates:
        player_id, team_name = row.get("playerId"), row.get("teamName")
        if not player_id or not team_name:
            continue
        try:
            game_log = pitching_splits.build_pitcher_game_log(player_id, team_name)
            streak = pitching_splits.build_pitcher_scoreless_streak(game_log)
        except Exception:
            continue
        if not streak or streak < 2:
            continue
        results.append({
            "playerId": player_id, "name": row.get("fullName"), "team": team_name,
            "value": f"{streak}G", "sort_key": streak,
        })

    results.sort(key=lambda e: e["sort_key"], reverse=True)
    return results[:limit]


def build_longest_hit_streak(limit=TOP_N):
    """Top 5: current active hit streak, among batters with at least
    MIN_PA_FOR_STREAK plate appearances this season. Reuses
    rolling.build_rolling_stats' own "hit_streak" field -- a plain
    consecutive-games-with-a-hit count, not the weighted momentum
    score built from it elsewhere (that score is a separate,
    subsequent combination step this never calls)."""
    all_batting = cbl_api.get_batting()
    candidates = _filter_active([r for r in all_batting if (r.get("plateAppearances") or 0) >= MIN_PA_FOR_STREAK])

    results = []
    for row in candidates:
        player_id, team_name = row.get("playerId"), row.get("teamName")
        if not player_id or not team_name:
            continue
        try:
            game_log = gamelog.build_player_game_log(player_id, team_name)
            streak = rolling.build_rolling_stats(game_log).get("hit_streak") or 0
        except Exception:
            continue
        if streak < 2:
            continue
        results.append({
            "playerId": player_id, "name": row.get("fullName"), "team": team_name,
            "value": f"{streak}G", "sort_key": streak,
        })

    results.sort(key=lambda e: e["sort_key"], reverse=True)
    return results[:limit]


def build_longest_onbase_streak(limit=TOP_N):
    """Top 5: current active streak of consecutive games reaching base
    (hit, walk, or HBP -- anything counted in rolling.py's own
    "on_base_streak" field), among batters with at least
    MIN_PA_FOR_STREAK plate appearances this season. Same game log
    already fetched for the hit-streak category above; if both
    leaderboards are being built together a caller could share that
    fetch, but each category here is independently wrapped (see
    build_all_obscure_stats), so this refetches rather than assume
    the hit-streak category ran first."""
    all_batting = cbl_api.get_batting()
    candidates = _filter_active([r for r in all_batting if (r.get("plateAppearances") or 0) >= MIN_PA_FOR_STREAK])

    results = []
    for row in candidates:
        player_id, team_name = row.get("playerId"), row.get("teamName")
        if not player_id or not team_name:
            continue
        try:
            game_log = gamelog.build_player_game_log(player_id, team_name)
            streak = rolling.build_rolling_stats(game_log).get("on_base_streak") or 0
        except Exception:
            continue
        if streak < 2:
            continue
        results.append({
            "playerId": player_id, "name": row.get("fullName"), "team": team_name,
            "value": f"{streak}G", "sort_key": streak,
        })

    results.sort(key=lambda e: e["sort_key"], reverse=True)
    return results[:limit]


def build_highest_iso(limit=TOP_N):
    """Top 5: Isolated Power (SLG - AVG) -- how much of a hitter's
    slugging comes from extra bases specifically, stripped of the
    part AVG already covers. Pure season-row arithmetic, no game-log
    walk. Same min-PA guard as every other rate-stat leaderboard on
    this site."""
    all_batting = _filter_active(cbl_api.get_batting())
    results = []
    for row in all_batting:
        if (row.get("plateAppearances") or 0) < MIN_PA_FOR_RATE:
            continue
        avg, slg = row.get("battingAvg"), row.get("slg")
        if avg is None or slg is None:
            continue
        results.append({
            "playerId": row.get("playerId"), "name": row.get("fullName"), "team": row.get("teamName"),
            "value": stats.fmt3(slg - avg), "sort_key": slg - avg,
        })
    results.sort(key=lambda e: e["sort_key"], reverse=True)
    return results[:limit]


def build_best_bb_k_ratio(limit=TOP_N):
    """Top 5: walk-to-strikeout ratio (BB/K) for batters -- plate
    discipline distilled to one number. Pure season-row arithmetic.
    Requires at least 1 strikeout on record (undefined/infinite
    otherwise) as well as the usual min-PA guard."""
    all_batting = _filter_active(cbl_api.get_batting())
    results = []
    for row in all_batting:
        if (row.get("plateAppearances") or 0) < MIN_PA_FOR_RATE:
            continue
        bb, so = row.get("walks") or 0, row.get("strikeouts") or 0
        if so <= 0:
            continue
        ratio = bb / so
        results.append({
            "playerId": row.get("playerId"), "name": row.get("fullName"), "team": row.get("teamName"),
            "value": stats.fmt2(ratio), "sort_key": ratio,
        })
    results.sort(key=lambda e: e["sort_key"], reverse=True)
    return results[:limit]


def build_best_k_bb_ratio(limit=TOP_N):
    """Top 5: strikeout-to-walk ratio (K/BB) for pitchers -- swing-and-
    miss stuff with control both accounted for in one number. Pure
    season-row arithmetic. Requires at least 1 walk allowed on record
    (undefined/infinite otherwise) as well as the usual min-IP guard."""
    all_pitching = _filter_active(cbl_api.get_pitching())
    results = []
    for row in all_pitching:
        if _ip_from_row(row) < MIN_IP_FOR_RATE:
            continue
        so, bb = row.get("strikeoutsPitching") or 0, row.get("walksAllowed") or 0
        if bb <= 0:
            continue
        ratio = so / bb
        results.append({
            "playerId": row.get("playerId"), "name": row.get("fullName"), "team": row.get("teamName"),
            "value": stats.fmt2(ratio), "sort_key": ratio,
        })
    results.sort(key=lambda e: e["sort_key"], reverse=True)
    return results[:limit]


def build_highest_babip(limit=TOP_N):
    """Top 5: BABIP (batting average on balls actually put in play,
    strips out strikeouts and home runs) -- how much of a hitter's
    average comes from balls finding grass versus swing-and-miss or
    going yard entirely. Reuses analytics.batting_advanced's own
    BABIP formula (documented judgment call on sacrifice flies noted
    there) rather than recomputing it here. Pure season-row
    arithmetic, no game-log walk."""
    all_batting = _filter_active(cbl_api.get_batting())
    results = []
    for row in all_batting:
        if (row.get("plateAppearances") or 0) < MIN_PA_FOR_RATE:
            continue
        babip = analytics.batting_advanced(row).get("babip")
        if babip is None:
            continue
        results.append({
            "playerId": row.get("playerId"), "name": row.get("fullName"), "team": row.get("teamName"),
            "value": stats.fmt3(babip), "sort_key": babip,
        })
    results.sort(key=lambda e: e["sort_key"], reverse=True)
    return results[:limit]


def build_best_contact_rate(limit=TOP_N):
    """Top 5: Contact Rate (1 - SO/PA) -- how often a hitter puts the
    ball in play (or reaches some other way) rather than striking
    out, regardless of what happens after contact. Pure season-row
    arithmetic."""
    all_batting = _filter_active(cbl_api.get_batting())
    results = []
    for row in all_batting:
        pa = row.get("plateAppearances") or 0
        if pa < MIN_PA_FOR_RATE:
            continue
        so = row.get("strikeouts") or 0
        rate = 1 - (so / pa)
        results.append({
            "playerId": row.get("playerId"), "name": row.get("fullName"), "team": row.get("teamName"),
            "value": stats.fmt_pct(rate), "sort_key": rate,
        })
    results.sort(key=lambda e: e["sort_key"], reverse=True)
    return results[:limit]


def build_best_gb_fb_ratio(limit=TOP_N):
    """Top 5: groundball-to-flyball ratio -- pitchers who keep the
    ball on the ground the most, relative to fly balls allowed (a
    real, if old-school, scouting-report descriptor: "he's a
    groundball pitcher"). Reuses analytics.pitching_advanced's own
    gb_fb_ratio rather than recomputing it. Pure season-row
    arithmetic."""
    all_pitching = _filter_active(cbl_api.get_pitching())
    results = []
    for row in all_pitching:
        if _ip_from_row(row) < MIN_IP_FOR_RATE:
            continue
        ratio = analytics.pitching_advanced(row).get("gb_fb_ratio")
        if ratio is None:
            continue
        results.append({
            "playerId": row.get("playerId"), "name": row.get("fullName"), "team": row.get("teamName"),
            "value": stats.fmt2(ratio), "sort_key": ratio,
        })
    results.sort(key=lambda e: e["sort_key"], reverse=True)
    return results[:limit]


def _count_runners_on_base(before):
    """Count of baserunners occupying 1st/2nd/3rd from a
    baseRunnersBeforePlay dict -- same base-occupancy shape used
    elsewhere on this site (e.g. splits.py's RISP logic), just
    counting occupied bases instead of checking one specific base."""
    if not before:
        return 0
    return sum(1 for base in ("first", "second", "third") if before.get(base))


def build_most_runners_stranded(limit=TOP_N):
    """Top 5: runners left on base (LOB) charged to the BATTER -- the
    real, official definition, not an approximation: for each plate
    appearance where the batter's OWN at-bat produces the half-
    inning's THIRD out, credit that batter with however many runners
    were on base at the start of that specific play and did not score
    on it. This is deliberately NOT "every runner on base whenever
    this batter made an out" -- a runner left on 2nd after a 1-out
    flyout isn't "left on base" in the official sense if the inning
    keeps going afterward; it only counts when the inning actually
    ends on this specific play.

    Requires walking each game's at-bats IN ORDER (unlike most other
    situational splits on this site, which only need to look at each
    play in isolation) and tracking a running out-count per half-
    inning that resets whenever the inning or half-inning changes --
    there's no way to know "did this specific play end the inning"
    from a single at-bat record by itself.

    Walks the full season schedule ONCE (gamelog._all_games -- every
    game in the league), crediting whichever player was actually at
    bat on the inning-ending play. Filtered to active, min-PA-
    qualified batters at the end by cross-referencing season batting
    rows, since this game-level walk has no season-total context of
    its own."""
    lob_by_player = {}  # player_id -> total runners stranded, across the whole season

    for g in gamelog._all_games():
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

        current_key = None  # (inning, halfInning) -- resets the out counter on change
        outs_this_half = 0
        for ab in gameday.get_at_bats(gd):
            if not ab.get("isComplete"):
                continue
            key = (ab.get("inning"), ab.get("halfInning"))
            if key != current_key:
                current_key = key
                outs_this_half = 0

            before_outs = outs_this_half
            outs_this_half += ab.get("outsRecorded") or 0

            if before_outs < 3 <= outs_this_half:
                # This specific play crossed the 3-out threshold --
                # the batter here made (or was part of) the inning-
                # ending out.
                runners_before = _count_runners_on_base(ab.get("baseRunnersBeforePlay"))
                runs_scored = len(ab.get("runsScored") or [])
                stranded = max(runners_before - runs_scored, 0)
                if stranded > 0:
                    batter_id = ab.get("batterId")
                    if batter_id:
                        lob_by_player[batter_id] = lob_by_player.get(batter_id, 0) + stranded

    if not lob_by_player:
        return []

    all_batting = _filter_active(cbl_api.get_batting())
    by_id = {r.get("playerId"): r for r in all_batting if r.get("playerId")}

    results = []
    for player_id, stranded in lob_by_player.items():
        row = by_id.get(player_id)
        if not row or (row.get("plateAppearances") or 0) < MIN_PA_FOR_RATE:
            continue
        results.append({
            "playerId": player_id, "name": row.get("fullName"), "team": row.get("teamName"),
            "value": str(stranded), "sort_key": stranded,
        })
    results.sort(key=lambda e: e["sort_key"], reverse=True)
    return results[:limit]


def build_most_times_left_on_base(limit=TOP_N):
    """Top 5: how many times a player has personally been left on
    base AS A RUNNER when a half-inning ended -- the complementary
    stat to Most Runners Left On Base just above (that one credits
    the BATTER whose own at-bat produces the inning-ending out; this
    one credits the RUNNER(S) actually stranded by it).

    Uses the exact same inning-ending-play detection as that stat
    (walk each game's at-bats in order, track a running out-count per
    half-inning that resets on every inning/half change -- see that
    function's docstring for the full reasoning on why this can't be
    determined from a single at-bat record in isolation). Deliberately
    kept as its own independent full walk rather than sharing one pass
    with build_most_runners_stranded -- consistent with how other
    related-but-distinct streak pairs on this page (e.g. the HR-less
    streak and the scoreless streak) are each their own walk on this
    site; simplicity over micro-optimizing a second league-wide pass.

    Where this differs mechanically: baseRunnersBeforePlay gives the
    specific PLAYER ID occupying each base, not just an occupancy
    count, and runsScored is also a list of specific player IDs (not
    just a count) -- confirmed elsewhere on this site, e.g.
    gameday.build_batting_box's own run-crediting loop reads it the
    same way. That lets this credit the EXACT runner(s) left on base,
    excluding whichever specific runner(s) actually scored on that
    same play, rather than just a count of how many were stranded."""
    times_stranded = {}  # player_id (as a runner) -> count of times personally left on base

    for g in gamelog._all_games():
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

        current_key = None
        outs_this_half = 0
        for ab in gameday.get_at_bats(gd):
            if not ab.get("isComplete"):
                continue
            key = (ab.get("inning"), ab.get("halfInning"))
            if key != current_key:
                current_key = key
                outs_this_half = 0

            before_outs = outs_this_half
            outs_this_half += ab.get("outsRecorded") or 0

            if before_outs < 3 <= outs_this_half:
                before = ab.get("baseRunnersBeforePlay") or {}
                scorers = set(ab.get("runsScored") or [])
                for base in ("first", "second", "third"):
                    runner_id = before.get(base)
                    if runner_id and runner_id not in scorers:
                        times_stranded[runner_id] = times_stranded.get(runner_id, 0) + 1

    if not times_stranded:
        return []

    all_batting = _filter_active(cbl_api.get_batting())
    by_id = {r.get("playerId"): r for r in all_batting if r.get("playerId")}

    results = []
    for player_id, count in times_stranded.items():
        row = by_id.get(player_id)
        if not row or (row.get("plateAppearances") or 0) < MIN_PA_FOR_RATE:
            continue
        results.append({
            "playerId": player_id, "name": row.get("fullName"), "team": row.get("teamName"),
            "value": str(count), "sort_key": count,
        })
    results.sort(key=lambda e: e["sort_key"], reverse=True)
    return results[:limit]


def build_most_team_lob(limit=TOP_N):
    """Top 5 TEAMS (not players) by total runners left on base this
    season -- the standard "Team LOB" number shown in every real box
    score, aggregated across the whole season rather than one game.

    Uses the exact same inning-ending-play detection as
    build_most_runners_stranded and build_most_times_left_on_base
    just above (walk each game's at-bats in order, track a running
    out-count per half-inning that resets on every change -- see
    those functions' docstrings for the full reasoning), but credits
    the stranded-runner COUNT to the batting TEAM for that half-
    inning rather than to any individual player. Batting team is
    home if halfInning is "bottom", away if "top" -- the same
    convention used throughout this app (gameday.py's own side
    determination for batting box scores works the same way).

    Kept as its own independent full walk rather than sharing a pass
    with the two player-level LOB stats, consistent with how every
    other related-but-distinct stat pairing on this page already
    works."""
    lob_by_team = {}  # team_name -> total runners left on base

    for g in gamelog._all_games():
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

        home = gamelog._field(g, "homeTeam", "home_team", "home-team")
        away = gamelog._field(g, "awayTeam", "away_team", "away-team")

        current_key = None
        outs_this_half = 0
        for ab in gameday.get_at_bats(gd):
            if not ab.get("isComplete"):
                continue
            key = (ab.get("inning"), ab.get("halfInning"))
            if key != current_key:
                current_key = key
                outs_this_half = 0

            before_outs = outs_this_half
            outs_this_half += ab.get("outsRecorded") or 0

            if before_outs < 3 <= outs_this_half:
                runners_before = _count_runners_on_base(ab.get("baseRunnersBeforePlay"))
                runs_scored = len(ab.get("runsScored") or [])
                stranded = max(runners_before - runs_scored, 0)
                if stranded > 0:
                    batting_team = home if (ab.get("halfInning") or "top") == "bottom" else away
                    if batting_team:
                        lob_by_team[batting_team] = lob_by_team.get(batting_team, 0) + stranded

    results = []
    for team_name, total in lob_by_team.items():
        results.append({
            "playerId": None, "name": team_name, "team": team_name,
            "value": str(total), "sort_key": total,
        })
    results.sort(key=lambda e: e["sort_key"], reverse=True)
    return results[:limit]


def build_most_total_bases(limit=TOP_N):
    """Top 5: total bases (1 for a single, 2 for a double, 3 for a
    triple, 4 for a home run) -- a cumulative power/production number
    distinct from home run count alone, since it also credits doubles
    and triples a HR-only leaderboard would ignore entirely. Pure
    season-row arithmetic (derived from hits/doubles/triples/HR, not
    a field CBL returns directly)."""
    all_batting = _filter_active(cbl_api.get_batting())
    results = []
    for row in all_batting:
        if (row.get("plateAppearances") or 0) < MIN_PA_FOR_RATE:
            continue
        h = row.get("hits") or 0
        doubles = row.get("doubles") or 0
        triples = row.get("triples") or 0
        hr = row.get("homeRuns") or 0
        singles = max(h - doubles - triples - hr, 0)
        tb = singles + 2 * doubles + 3 * triples + 4 * hr
        results.append({
            "playerId": row.get("playerId"), "name": row.get("fullName"), "team": row.get("teamName"),
            "value": str(tb), "sort_key": tb,
        })
    results.sort(key=lambda e: e["sort_key"], reverse=True)
    return results[:limit]


def build_highest_walk_rate(limit=TOP_N):
    """Top 5: Walk Rate (BB/PA) -- how often a hitter's plate
    appearance ends in a walk specifically, as a share of every trip
    to the plate. Distinct framing from the BB/K ratio category above
    (a rate against total PA, not a ratio against strikeouts
    specifically) -- a hitter can lead one without leading the other.
    Pure season-row arithmetic."""
    all_batting = _filter_active(cbl_api.get_batting())
    results = []
    for row in all_batting:
        pa = row.get("plateAppearances") or 0
        if pa < MIN_PA_FOR_RATE:
            continue
        bb = row.get("walks") or 0
        rate = bb / pa
        results.append({
            "playerId": row.get("playerId"), "name": row.get("fullName"), "team": row.get("teamName"),
            "value": stats.fmt_pct(rate), "sort_key": rate,
        })
    results.sort(key=lambda e: e["sort_key"], reverse=True)
    return results[:limit]


DETAIL_LIMIT = 50  # how many rows a single-category detail page shows, vs TOP_N=5 on the summary page

# Single source of truth for every category on the Obscure Stats page --
# both build_all_obscure_stats (the summary page, top 5 each) and
# build_single_obscure_stat (a full leaderboard for one category, used
# by the "click the headline" detail view) read from this same list,
# so there's exactly one place that defines what categories exist and
# what each one is called/does.
OBSCURE_STAT_CATEGORIES = [
    {"slug": "ip-without-hr", "title": "Most IP Without Allowing a HR (Active Streak)",
     "subtitle": "Pitchers currently working the longest streak of innings without giving one up",
     "builder": build_most_ip_without_hr},
    {"slug": "scoreless-streak", "title": "Longest Active Scoreless Streak",
     "subtitle": "Consecutive appearances in a row without allowing a run, right now",
     "builder": build_longest_scoreless_streak},
    {"slug": "hit-streak", "title": "Longest Active Hit Streak",
     "subtitle": "Consecutive games in a row with a hit, right now",
     "builder": build_longest_hit_streak},
    {"slug": "onbase-streak", "title": "Longest Active On-Base Streak",
     "subtitle": "Consecutive games in a row reaching base -- hit, walk, or HBP -- right now",
     "builder": build_longest_onbase_streak},
    {"slug": "iso", "title": "Highest Isolated Power (ISO)",
     "subtitle": "SLG minus AVG -- how much of a hitter's slugging is extra bases, not just hits",
     "builder": build_highest_iso},
    {"slug": "babip", "title": "Highest BABIP",
     "subtitle": "Batting average on balls actually put in play -- strips out strikeouts and homers entirely",
     "builder": build_highest_babip},
    {"slug": "contact-rate", "title": "Best Contact Rate",
     "subtitle": "How often a hitter puts the ball in play instead of striking out, regardless of the result",
     "builder": build_best_contact_rate},
    {"slug": "total-bases", "title": "Most Total Bases",
     "subtitle": "1 for a single, 2 for a double, 3 for a triple, 4 for a home run -- credits doubles and triples a HR count alone would ignore",
     "builder": build_most_total_bases},
    {"slug": "walk-rate", "title": "Highest Walk Rate",
     "subtitle": "Share of plate appearances that end in a walk",
     "builder": build_highest_walk_rate},
    {"slug": "bb-k-ratio", "title": "Best BB/K Ratio (Batting)",
     "subtitle": "Walks per strikeout -- plate discipline in one number",
     "builder": build_best_bb_k_ratio},
    {"slug": "k-bb-ratio", "title": "Best K/BB Ratio (Pitching)",
     "subtitle": "Strikeouts per walk -- swing-and-miss stuff with control, both at once",
     "builder": build_best_k_bb_ratio},
    {"slug": "gb-fb-ratio", "title": "Best GB/FB Ratio (Pitching)",
     "subtitle": "Groundballs per flyball allowed -- the old-school \"groundball pitcher\" label, quantified",
     "builder": build_best_gb_fb_ratio},
    {"slug": "runners-stranded", "title": "Most Runners Left On Base",
     "subtitle": "Runners stranded on the batter's own inning-ending out -- the official LOB definition, not just any out with runners on",
     "builder": build_most_runners_stranded},
    {"slug": "times-left-on-base", "title": "Most Times Left On Base",
     "subtitle": "The flip side -- how often a player has personally been the runner stranded when the inning ended",
     "builder": build_most_times_left_on_base},
    {"slug": "team-lob", "title": "Most Runners Left On Base (Team)",
     "subtitle": "The standard \"Team LOB\" box score number, totaled across the whole season",
     "builder": build_most_team_lob},
]


def build_all_obscure_stats():
    """Every leaderboard on the summary page, top TOP_N each, each
    independently wrapped so one failing category (e.g. a broken
    game-log fetch) doesn't take the others down with it."""
    result = []
    for cat in OBSCURE_STAT_CATEGORIES:
        try:
            rows = cat["builder"](TOP_N)
        except Exception:
            rows = []
        result.append({"title": cat["title"], "subtitle": cat["subtitle"], "rows": rows, "slug": cat["slug"]})
    return result


def build_single_obscure_stat(slug, limit=DETAIL_LIMIT):
    """One category's full leaderboard (DETAIL_LIMIT rows by default,
    not just the summary page's top 5) -- powers the detail page a
    category's headline links to. Returns None if slug doesn't match
    any known category, so the route can 404 cleanly rather than
    render an empty page for a typo'd or stale URL."""
    for cat in OBSCURE_STAT_CATEGORIES:
        if cat["slug"] == slug:
            try:
                rows = cat["builder"](limit)
            except Exception:
                rows = []
            return {"title": cat["title"], "subtitle": cat["subtitle"], "rows": rows, "slug": slug}
    return None
