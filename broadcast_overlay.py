"""
Live broadcast overlay: given a CBL public_game_id that's actively
being scored, figures out who's at bat right now and builds a
broadcast-ready stat card for them, plus the current scoreboard
(score, inning, outs, line score).

Deliberately NOT a scorekeeper -- this doesn't track pitches, doesn't
let anyone enter plays, and has no state of its own beyond "which game
am I watching." Every poll just re-reads CBL's own live gameday feed
fresh (cbl_api.get_gameday is already cached briefly, so polling every
few seconds doesn't hammer CBL's API) and derives everything from
that -- if CBL's own scorekeeper app isn't actively updating a game,
this has nothing to show and says so plainly rather than guessing.

Current batter, in order of preference:
  1. `liveGame.currentAtBat` -- present and non-null while an at-bat is
     actually in progress; confirmed null on a completed game in a
     real payload, so this is trusted when present.
  2. `liveGame.currentBatterIndex[side]` combined with
     `liveGame.awayLineup` / `homeLineup` (flat arrays of player IDs in
     batting order, confirmed real fields) and `halfInning` (which side
     is actually batting right now) -- used as a fallback for whatever
     gap currentAtBat doesn't cover (e.g. between at-bats).
If neither source resolves to a player ID, there's genuinely no live
at-bat to show (game hasn't started, is over, or isn't being scored
live) and callers should say so rather than show stale/wrong data.
"""
import time

import analytics
import cbl_api
import gameday
import gamelog
import pitching_splits
import player_merge
import rolling
import splits
import stats

# Short-lived in-memory cache for the two EXPENSIVE per-poll lookups below
# (build_batter_situational_splits / build_pitcher_situational_splits),
# which each walk every completed game for a team's whole season the same
# way splits.py's own player-page Splits tab does. The live overlay page
# polls /broadcast/state.json every 2 seconds, and re-walking a full
# season on every single poll (for both the current batter AND pitcher)
# is real work even with cbl_api's own gameday disk-cache doing the heavy
# lifting underneath -- this just avoids redoing that walk more than once
# every SITUATIONAL_CACHE_TTL seconds per player, same pattern cbl_api.py
# already uses for its own _cached_get. A player who's *currently* at bat
# doesn't gain a new game in their log mid-poll, so a short staleness
# window here costs nothing in accuracy.
_situational_cache = {}
SITUATIONAL_CACHE_TTL = 60


def _cached_situational(cache_key, builder):
    hit = _situational_cache.get(cache_key)
    now = time.time()
    if hit and (now - hit[0]) < SITUATIONAL_CACHE_TTL:
        return hit[1]
    value = builder()
    _situational_cache[cache_key] = (now, value)
    return value


def _live_pitch_sequence(current_at_bat):
    """Pitch-by-pitch sequence for the at-bat in progress right now,
    each entry tagged with the ball-strike count it was thrown on.
    Same reconstruction logic as gameday.py's own _pitch_sequence
    (kept as a local copy here rather than importing that module-
    private helper), built off the real nested `pitches` list on
    currentAtBat -- each pitch has id/result/timestamp, confirmed real
    fields per gameday.py's own module docstring.

    Returns [] if there's no at-bat in progress, or it hasn't seen a
    pitch yet (freshly started at-bat)."""
    if not current_at_bat:
        return []
    pitches = current_at_bat.get("pitches") or []
    seq = []
    balls = strikes = 0
    for p in pitches:
        result = p.get("result") or ""
        seq.append({"result": result, "balls": balls, "strikes": strikes})
        if result in gameday.BALL_RESULTS:
            balls += 1
        elif result in gameday.STRIKE_RESULTS:
            strikes = min(strikes + 1, 3)
        elif result in gameday.FOUL_RESULTS:
            strikes = min(strikes + 1, 2)
    return seq


def get_live_game_state(public_game_id):
    """
    {
      "found": bool,
      "is_game_over": bool,
      "home_team": str, "away_team": str,
      "home_score": int, "away_score": int,
      "inning": int, "half_inning": "top"|"bottom",
      "outs": int, "balls": int, "strikes": int,
      "inning_scores": [{"inning": int, "away": int, "home": int}, ...],
      "bases": {"first": bool, "second": bool, "third": bool} | None,
      "pitch_sequence": [{"result": str, "balls": int, "strikes": int}, ...],
      "current_batter_id": str|None,
      "current_pitcher_id": str|None,
    }
    "found": False means the game_id itself couldn't be resolved at
    all (bad ID, or cbl.ca unreachable) -- everything else defaults to
    empty/zero in that case, not fabricated.

    "bases" comes from liveGame.baseRunners -- a top-level, always-on
    field (confirmed present in real live payloads, e.g. {"first":
    "<playerId>", "second": null, "third": null}) that reflects the
    live base state at all times, including in the gap between at-bats.
    This is preferred over currentAtBat.baseRunnersBeforePlay, which
    turns out NOT to be reliably present while an at-bat is still in
    progress -- CBL appears to only populate it once the at-bat
    completes, so leaning on it left the bases graphic showing empty
    for the entire span of any live at-bat with runners already on.
    baseRunnersBeforePlay is kept as a fallback for older/odd payload
    shapes where baseRunners itself might be missing.

    "pitch_sequence" is the CURRENT at-bat's own pitches, in order,
    each with the count it was thrown on -- see _live_pitch_sequence.
    Empty list (not None) whenever there's no at-bat in progress or it
    hasn't seen a pitch yet, since "no pitches thrown" is a normal,
    valid state rather than missing data.
    """
    try:
        gd = cbl_api.get_gameday(public_game_id)
    except Exception:
        gd = None

    if not gd or not gd.get("snapshot"):
        return {
            "found": False, "is_game_over": False,
            "home_team": None, "away_team": None,
            "home_score": 0, "away_score": 0,
            "inning": None, "half_inning": None,
            "outs": None, "balls": None, "strikes": None,
            "inning_scores": [], "bases": None, "pitch_sequence": [],
            "current_batter_id": None, "current_pitcher_id": None,
        }

    live = gameday._live(gd)
    snapshot = gameday._snapshot(gd)
    home_team = (snapshot.get("homeTeam") or {}).get("name")
    away_team = (snapshot.get("awayTeam") or {}).get("name")

    half_inning = live.get("halfInning")
    is_home_batting = half_inning == "bottom"

    current_batter_id = None
    current_at_bat = live.get("currentAtBat")

    bases = None
    live_base_runners = live.get("baseRunners")
    if live_base_runners is not None:
        bases = {
            "first": bool(live_base_runners.get("first")),
            "second": bool(live_base_runners.get("second")),
            "third": bool(live_base_runners.get("third")),
        }
    elif current_at_bat and current_at_bat.get("baseRunnersBeforePlay") is not None:
        # Fallback only -- see docstring above. baseRunnersBeforePlay
        # is frequently absent on an in-progress at-bat, so this branch
        # mainly covers payload shapes that don't carry baseRunners at
        # all rather than being the everyday path.
        before = current_at_bat.get("baseRunnersBeforePlay") or {}
        bases = {
            "first": bool(before.get("first")),
            "second": bool(before.get("second")),
            "third": bool(before.get("third")),
        }

    if current_at_bat and current_at_bat.get("batterId"):
        current_batter_id = current_at_bat.get("batterId")
    else:
        idx = live.get("currentBatterIndex") or {}
        lineup = live.get("homeLineup") if is_home_batting else live.get("awayLineup")
        side_idx = idx.get("home" if is_home_batting else "away")
        if lineup and side_idx is not None and 0 <= side_idx < len(lineup):
            current_batter_id = lineup[side_idx]

    current_pitcher_id = live.get("homePitcherId") if not is_home_batting else live.get("awayPitcherId")

    raw_scores = live.get("inningScores") or []
    inning_scores = [
        {"inning": s.get("inning"), "away": s.get("awayRuns") or 0, "home": s.get("homeRuns") or 0}
        for s in raw_scores
    ]

    return {
        "found": True,
        "is_game_over": bool(live.get("isGameOver")),
        "home_team": home_team, "away_team": away_team,
        "home_score": live.get("homeScore") or 0, "away_score": live.get("awayScore") or 0,
        "inning": live.get("currentInning"), "half_inning": half_inning,
        "outs": live.get("outs"), "balls": live.get("balls"), "strikes": live.get("strikes"),
        "inning_scores": inning_scores,
        "bases": bases,
        "pitch_sequence": _live_pitch_sequence(current_at_bat),
        "current_batter_id": current_batter_id,
        "current_pitcher_id": current_pitcher_id,
    }


def _headshot_from_roster(gd, player_id):
    """Headshot URL for one player, from the CURRENT game's own roster
    data (snapshot.awayTeam.roster / homeTeam.roster -- confirmed real
    fields: id, headshotUrl, profileImageUrl). NOT assumed to exist on
    the season /stats/batting row, which hasn't been confirmed to carry
    a headshot field at all -- the roster is the confirmed source."""
    if not gd:
        return None
    snapshot = gameday._snapshot(gd)
    for side in ("awayTeam", "homeTeam"):
        for entry in (snapshot.get(side) or {}).get("roster") or []:
            if entry.get("id") == player_id:
                return entry.get("headshotUrl") or entry.get("profileImageUrl")
    return None


def _blended_batting_line(row, raw_today):
    """Season batting line blended with TODAY's own live in-game counts
    (raw_today = gameday.get_player_batting_stats' entry for this game,
    NOT the trimmed today_summary dict), so AVG/OBP/SLG/OPS/HR/RBI
    actually move at-bat by at-bat during a live game instead of only
    reflecting CBL's own /stats/batting endpoint -- which, per its own
    docstring in build_batter_card, only carries completed games.

    Uses the exact same simplified convention (no HBP/SF in OBP; TB
    from AB/H/2B/3B/HR) that player_merge.merge_batting_rows already
    uses to combine a traded player's multiple team-stint rows -- so a
    mid-game recompute here stays consistent with how every other
    number in this app is derived, rather than introducing a second,
    slightly different formula.

    Returns the season row's own totals unchanged (blended == season)
    when raw_today is missing or this player hasn't had a plate
    appearance yet today -- "has_today" tells the caller which case
    it is, so the UI can skip an "includes today" label until there's
    actually something new to include."""
    season_ab = row.get("atBats") or 0
    season_h = row.get("hits") or 0
    season_bb = row.get("walks") or 0
    season_pa = row.get("plateAppearances") or 0
    season_2b = row.get("doubles") or 0
    season_3b = row.get("triples") or 0
    season_hr = row.get("homeRuns") or 0
    season_rbi = row.get("rbi") or 0

    t = raw_today or {}
    t_pa = t.get("plateAppearances") or 0

    ab = season_ab + (t.get("atBats") or 0)
    h = season_h + (t.get("hits") or 0)
    bb = season_bb + (t.get("walks") or 0)
    pa = season_pa + t_pa
    doubles = season_2b + (t.get("doubles") or 0)
    triples = season_3b + (t.get("triples") or 0)
    hr = season_hr + (t.get("homeRuns") or 0)
    rbi = season_rbi + (t.get("rbi") or 0)

    singles = max(h - doubles - triples - hr, 0)
    tb = singles + 2 * doubles + 3 * triples + 4 * hr
    obp = stats.safe_div(h + bb, pa)
    slg = stats.safe_div(tb, ab)

    return {
        "avg": stats.safe_div(h, ab), "obp": obp, "slg": slg, "ops": obp + slg,
        "hr": hr, "rbi": rbi,
        "has_today": t_pa > 0,
    }


def build_batter_card(player_id, gd=None, opposing_pitcher_id=None):
    """Rich broadcast stat card for one batter -- reuses this app's
    existing analytics modules for the season line, plus two pie-chart
    data sources chosen specifically to avoid an expensive full-season
    walk on every single poll of a live-updating page:

    - "recent_pa": last-50-PA outcome breakdown (build_recent_pa_breakdown)
      -- a BOUNDED walk that stops once it's collected enough PAs,
      not a full season.
    - "contact_quality": hard/medium/soft batted-ball split, pulled
      straight from CBL's own precomputed analytics feed
      (eventAnalytics.batting.battedBalls.byQuality) -- this feed is
      already being fetched below for platoon splits, so this adds
      zero extra network calls, unlike a fresh full-season batted-ball
      walk would.

    Also carries "today": this game's own in-progress line (AB/H/R/RBI/
    BB/SO/2B/3B/HR) straight from CBL's pre-aggregated
    playerBattingStats for this game (gameday.get_player_batting_stats)
    -- NOT the season row, which only reflects completed games. Updates
    on the same poll as everything else in this game since it's read
    off the already-fetched `gd`, no separate cache/TTL.

    The headline avg/obp/slg/ops/hr/rbi are a LIVE BLEND of the season
    row plus today's own counts (see _blended_batting_line) -- so they
    actually move at-bat by at-bat during today's game, rather than
    sitting frozen at whatever CBL's /stats/batting endpoint last
    posted (which per its own docstring above only reflects completed
    games). The pure season-only numbers CBL itself would show
    (entering today) are still returned alongside as season_avg /
    season_obp / season_slg / season_ops / season_hr / season_rbi, and
    `includes_today` is False whenever there's no live at-bat data to
    blend in yet -- both exist so the UI can label which one it's
    showing rather than quietly presenting a recomputed number as if
    it were CBL's own official stat.

    Returns None if this player has no batting stat row on record at
    all (e.g. a pinch runner with no plate appearances yet, or an
    unrecognized ID).

    gd: the current game's already-fetched gameday data, if the caller
    has it (get_live_game_state's caller usually does) -- used for the
    headshot lookup so this doesn't need its own extra fetch."""
    all_batting = cbl_api.get_batting()
    rows = player_merge.find_player_rows(player_id, all_batting)
    if not rows:
        return None
    row = player_merge.merge_batting_rows(rows)
    team_names = player_merge.team_names_for_rows(rows)

    all_batting_ctx = analytics.league_batting_context(all_batting)
    adv = analytics.batting_advanced(row)
    adv.update(analytics.batting_plus_stats(adv, all_batting_ctx))

    try:
        recent_pa = build_recent_pa_breakdown(player_id, team_names)
    except Exception:
        recent_pa = None

    platoon = {}
    contact_quality = None
    try:
        feed = cbl_api.get_player_analytics(player_id)
    except Exception:
        feed = None
    if feed:
        bat = (feed.get("eventAnalytics") or {}).get("batting") or {}
        bat_splits = bat.get("splits") or {}
        for s in bat_splits.get("byPitcherHandedness") or []:
            raw_key = (s.get("key") or "").lower()
            if raw_key not in ("left", "right"):
                continue
            o = s.get("outcomes") or {}
            # Normalized to a fixed avg/obp/slg/ops shape -- these come
            # straight from CBL's own precomputed split (battingAverage
            # etc.), not recomputed here, so they're already correct
            # against whatever PA/AB convention CBL itself uses.
            platoon["vsLeft" if raw_key == "left" else "vsRight"] = {
                "pa": o.get("plateAppearances"), "ab": o.get("atBats"), "hr": o.get("homeRuns"),
                "avg": o.get("battingAverage"), "obp": o.get("onBasePercentage"),
                "slg": o.get("sluggingPercentage"), "ops": o.get("onBasePlusSlugging"),
            }
        quality = ((bat.get("battedBalls") or {}).get("byQuality")) or {}
        total_quality = (quality.get("hard") or 0) + (quality.get("medium") or 0) + (quality.get("soft") or 0)
        if total_quality:
            contact_quality = {
                "hard": quality.get("hard") or 0, "medium": quality.get("medium") or 0, "soft": quality.get("soft") or 0,
                "total": total_quality,
            }

    situational = build_batter_situational_splits(player_id, team_names)

    # TODAY's in-game line -- CBL's own pre-aggregated per-game batting
    # numbers for this player (gameday.get_player_batting_stats), not a
    # season aggregate. Same pattern as build_pitcher_card's `today`
    # below: this game's own playerBattingStats entry updates itself as
    # CBL's scorekeeper app records each new plate appearance, so it
    # updates on the SAME poll where the game state does -- no separate
    # cache/TTL of its own, and no extra network call since gd is
    # already fetched by the caller.
    raw_today = gameday.get_player_batting_stats(gd, player_id) if gd else None
    today_summary = None
    if raw_today:
        today_summary = {
            "ab": raw_today.get("atBats"),
            "h": raw_today.get("hits"),
            "r": raw_today.get("runs"),
            "rbi": raw_today.get("rbi"),
            "bb": raw_today.get("walks"),
            "hbp": raw_today.get("hitByPitch"),
            "so": (raw_today.get("strikeoutsLooking") or 0) + (raw_today.get("strikeoutsSwinging") or 0),
            "doubles": raw_today.get("doubles"),
            "triples": raw_today.get("triples"),
            "hr": raw_today.get("homeRuns"),
        }

    blended = _blended_batting_line(row, raw_today)

    return {
        "playerId": player_id,
        "name": row.get("fullName"),
        "team": " / ".join(team_names) if team_names else None,
        "position": row.get("position"),
        "headshotUrl": _headshot_from_roster(gd, player_id),
        "games": row.get("games"), "pa": row.get("plateAppearances"), "ab": row.get("atBats"),
        # Headline numbers are the LIVE blend (season + today) -- see
        # _blended_batting_line. The pure season-only figures (what
        # CBL's own /stats/batting shows, entering today's game) are
        # kept alongside as season_* so the UI can still show/label
        # them for transparency rather than silently swapping one
        # number for another.
        "avg": blended["avg"], "obp": blended["obp"], "slg": blended["slg"], "ops": blended["ops"],
        "hr": blended["hr"], "rbi": blended["rbi"],
        "includes_today": blended["has_today"],
        "season_avg": row.get("battingAvg"), "season_obp": row.get("obp"),
        "season_slg": row.get("slg"), "season_ops": row.get("ops"),
        "season_hr": row.get("homeRuns"), "season_rbi": row.get("rbi"),
        "ops_plus": adv.get("ops_plus"), "iso": adv.get("iso"),
        "today": today_summary,
        "recent_pa": recent_pa,
        "contact_quality": contact_quality,
        "platoon": platoon,
        "situational": situational,
    }


def build_batter_situational_splits(player_id, team_names):
    """Broadcast-sized slice of splits.build_player_splits -- picks out
    the handful of situational rows a broadcast viewer actually cares
    about in the moment (runners in scoring position vs bases empty,
    home/away, two strikes, late & close) rather than the full
    multi-section Splits-tab breakdown. Each row already carries
    live/updating avg/obp/slg/ops (splits.py's own _finalize), computed
    fresh from every completed game on record, so this updates itself
    as the season goes rather than needing separate upkeep.

    Returns None if there's no team to walk a game log for, or if the
    walk itself fails for any reason (offline feed, no games yet)."""
    if not team_names:
        return None

    def _build():
        try:
            sections, quality_pa, batted_ball_profile = splits.build_player_splits(player_id, list(team_names))
        except Exception:
            return None
        lookup = {}
        for rows in sections.values():
            for label, totals in rows:
                lookup[label] = totals
        return {
            "risp": lookup.get("Scoring Position"),
            "bases_empty": lookup.get("Bases Empty"),
            "home": lookup.get("Home Games"),
            "away": lookup.get("Away Games"),
            "two_strikes": lookup.get("With 2 Strikes"),
            "late_close": lookup.get("Late & Close"),
            # These two were being silently discarded here before --
            # splits.build_player_splits already computes them as part
            # of the exact same at-bat walk this function needs anyway,
            # so exposing them costs nothing extra, not even a second
            # pass over the same games.
            "quality_pa": quality_pa,
            "batted_ball_profile": batted_ball_profile,
            # Exact-count splits (e.g. "2-0", "0-2") -- same story as
            # quality_pa/batted_ball_profile above: build_player_splits
            # already computes a full "Count Splits" section as part of
            # this same walk, previously left sitting in `sections` and
            # never pulled out here. Keyed by count string so the live
            # overlay can look up "whatever count is on right now"
            # directly, e.g. by_count.get("2-0"), rather than searching
            # a list every poll.
            "by_count": {label: totals for label, totals in sections.get("Count Splits", [])},
        }

    cache_key = ("batter_situational", tuple(sorted(team_names)), player_id)
    return _cached_situational(cache_key, _build)


def build_pitcher_situational_splits(player_id, team_names):
    """Pitching counterpart to build_batter_situational_splits, built
    off pitching_splits.build_pitcher_splits instead -- same idea, same
    handful of situational rows, same short cache."""
    if not team_names:
        return None

    def _build():
        try:
            sections = pitching_splits.build_pitcher_splits(player_id, list(team_names))
        except Exception:
            return None
        lookup = {}
        for rows in sections.values():
            for label, totals in rows:
                lookup[label] = totals
        return {
            "risp": lookup.get("Scoring Position"),
            "bases_empty": lookup.get("Bases Empty"),
            "home": lookup.get("Home Games"),
            "away": lookup.get("Away Games"),
        }

    cache_key = ("pitcher_situational", tuple(sorted(team_names)), player_id)
    return _cached_situational(cache_key, _build)


def build_pitcher_card(player_id, gd=None):
    """Broadcast stat card for the current pitcher -- season line plus
    TODAY's in-game workload (pitch count, ball/strike split), pulled
    from this specific game's own playerPitchingStats
    (gameday.get_player_pitching_stats), not a season aggregate.
    Returns None if this player has no pitching stat row on record.

    gd: the current game's already-fetched gameday data, if the caller
    has it -- used for both the headshot lookup and today's pitch
    count, so this doesn't need its own extra fetch."""
    all_pitching = cbl_api.get_pitching()
    rows = player_merge.find_player_rows(player_id, all_pitching)
    if not rows:
        return None
    row = player_merge.merge_pitching_rows(rows)
    team_names = player_merge.team_names_for_rows(rows)

    pitching_ctx = analytics.league_pitching_context(all_pitching)
    adv = analytics.pitching_advanced(row, pitching_ctx)

    throws = None
    platoon = {}
    try:
        feed = cbl_api.get_player_analytics(player_id)
    except Exception:
        feed = None
    if feed:
        throws = (feed.get("player") or {}).get("throws")
        pitch = (feed.get("eventAnalytics") or {}).get("pitching") or {}
        pitch_splits = pitch.get("splits") or {}
        for s in pitch_splits.get("byBatterHandedness") or []:
            raw_key = (s.get("key") or "").lower()
            if raw_key not in ("left", "right"):
                continue
            o = s.get("outcomes") or {}
            platoon["vsLeft" if raw_key == "left" else "vsRight"] = {
                "bf": o.get("plateAppearances") or o.get("battersFaced") or o.get("batters_faced") or o.get("bf"),
                "hr": o.get("homeRuns"), "bb": o.get("walks"), "so": o.get("strikeouts"),
                # NOTE: keyed as "avg_against", not "avg" -- the pitcher
                # splits table in broadcast_overlay.html reads
                # t['avg_against'] (matching the key pitching_splits.py's
                # own _finalize() uses for the RISP/bases-empty rows in
                # `situational`), so a platoon row keyed "avg" here was
                # silently rendering as "---" even with real data.
                "avg_against": o.get("battingAverage"),
            }

    situational = build_pitcher_situational_splits(player_id, team_names)

    today = gameday.get_player_pitching_stats(gd, player_id) if gd else None
    today_summary = None
    if today:
        today_summary = {
            "pitchCount": today.get("pitchCount"),
            "balls": today.get("balls"),
            "strikes": today.get("strikes"),
            "fouls": today.get("fouls"),
            "outs": today.get("outs"),
            "hits": today.get("h"),
            "runs": today.get("r"),
            "strikeouts": today.get("so"),
            "walks": today.get("bb"),
        }

    return {
        "playerId": player_id,
        "name": row.get("fullName"),
        "throws": throws,
        "headshotUrl": _headshot_from_roster(gd, player_id),
        "games": row.get("games"), "wins": row.get("wins"), "losses": row.get("losses"),
        "saves": row.get("saves"), "innings_pitched": row.get("inningsPitched"),
        "era": row.get("era"), "whip": row.get("whip"),
        "strikeouts": row.get("strikeoutsPitching"), "walks": row.get("walksAllowed"),
        "era_plus": adv.get("era_plus"), "fip": adv.get("fip"),
        "today": today_summary,
        "platoon": platoon,
        "situational": situational,
    }


def build_due_up(gd, count=3):
    """Next `count` batters coming up after whoever's at bat right now,
    cycling forward through the batting team's own lineup starting
    just past `currentBatterIndex` (wrapping around the bottom of the
    order back to the top, same as a real lineup does). Returns a list
    of {"playerId", "name", "avg", "hr", "headshotUrl"} dicts -- season
    stats, not live-blended, since these players haven't batted yet
    this trip through the order.

    Shorter than `count` if the batting team's own lineup has fewer
    entries than that (shouldn't normally happen with a real 9-man
    lineup, but a partial/incomplete lineup shouldn't crash this), and
    empty if there's no live batting-order data to work from at all."""
    if not gd or not gd.get("snapshot"):
        return []
    live = gameday._live(gd)
    half_inning = live.get("halfInning")
    is_home_batting = half_inning == "bottom"
    lineup = live.get("homeLineup") if is_home_batting else live.get("awayLineup")
    if not lineup:
        return []
    idx = live.get("currentBatterIndex") or {}
    side_idx = idx.get("home" if is_home_batting else "away")
    if side_idx is None:
        return []

    all_batting = cbl_api.get_batting()
    result = []
    seen = set()
    for i in range(1, len(lineup) + 1):
        if len(result) >= count:
            break
        next_idx = (side_idx + i) % len(lineup)
        pid = lineup[next_idx]
        if pid in seen:
            break  # wrapped all the way back around without finding count players
        seen.add(pid)
        rows = player_merge.find_player_rows(pid, all_batting)
        row = player_merge.merge_batting_rows(rows) if rows else {}
        result.append({
            "playerId": pid,
            "name": row.get("fullName") or pid,
            "avg": row.get("battingAvg"),
            "hr": row.get("homeRuns"),
            "headshotUrl": _headshot_from_roster(gd, pid),
        })
    return result


MIN_AB_FOR_LEADER = 40  # rate-stat qualification floor for the leader rotation below
MIN_IP_FOR_LEADER = 10  # matches broadcast_notes.py's own constant of the same name/value

# (display label, stat source, row key, ascending?, qualifier)
LEADER_CATEGORIES = [
    ("Home Run Leader", "batting", "homeRuns", False, None),
    ("RBI Leader", "batting", "rbi", False, None),
    ("Batting Average Leader", "batting", "battingAvg", False,
     lambda r: (r.get("atBats") or 0) >= MIN_AB_FOR_LEADER),
    ("ERA Leader", "pitching", "era", True,
     lambda r: ((r.get("advancedPitching") or {}).get("inningsPitchedOuts") or 0) / 3 >= MIN_IP_FOR_LEADER),
    ("Strikeout Leader", "pitching", "strikeoutsPitching", False, None),
]


def build_leader_rotation(home_team, away_team):
    """For each of a handful of "fun" season-stat categories, checks
    the top 5 of that leaderboard for a player from either of TODAY's
    two teams -- if one's there, includes an entry the broadcast
    overlay can cycle through. A category that has nobody from either
    team in its top 5 is skipped entirely rather than padded with an
    irrelevant leader, so the rotation only ever shows things actually
    relevant to this specific matchup.

    ERA sorts ascending (lower is better); everything else descending.
    Rate-stat categories (AVG, ERA) apply the same playing-time
    minimums used elsewhere in this app (broadcast_notes.py's own
    MIN_IP_FOR_LEADER, and an equivalent MIN_AB_FOR_LEADER here) so a
    1-for-1 hitter can't show up as a "league leader."

    Cached the same way the situational splits are (SITUATIONAL_CACHE_TTL)
    -- league standings don't meaningfully shift possible within a few
    seconds of each other, so there's no reason to re-sort the entire
    league on every single poll of a page that polls every 2 seconds."""
    def _build():
        teams = {t for t in (home_team, away_team) if t}
        if not teams:
            return []
        results = []
        for label, source, key, ascending, qualifier in LEADER_CATEGORIES:
            rows = cbl_api.get_batting() if source == "batting" else cbl_api.get_pitching()
            if qualifier:
                rows = [r for r in rows if qualifier(r)]
            rows = [r for r in rows if r.get(key) is not None]
            if not rows:
                continue
            ranked = sorted(rows, key=lambda r: r.get(key), reverse=not ascending)
            for rank, r in enumerate(ranked[:5], start=1):
                if r.get("teamName") in teams:
                    results.append({
                        "label": label, "player": r.get("fullName"), "team": r.get("teamName"),
                        "value": r.get(key), "rank": rank, "is_rate": key in ("battingAvg", "era"),
                    })
                    break  # only the best-ranked qualifying player from either team per category
        return results

    cache_key = ("leader_rotation", home_team, away_team)
    return _cached_situational(cache_key, _build)


def build_recent_pa_breakdown(player_id, team_names, target_pa=50):
    """Outcome breakdown of this player's most recent plate appearances
    (single/double/triple/HR/BB+HBP/strikeout/other-out), walking
    backward from the most recent completed game until target_pa PAs
    are collected (or the season runs out). Meant for a pie/donut chart
    -- returns {"total": int, "counts": {...}, "pct": {...}}.

    Walks actual at-bats (not the pre-aggregated per-game rows the
    Game Log tab uses), since an outcome-type breakdown needs each
    individual PA's result, not just a game's H/AB totals. Most of the
    games involved are already disk-cached (completed games never
    change), so repeated calls -- like this page's own live polling --
    are fast after the first one; only the most recent game (still
    being actively scored) gets fetched fresh each time.
    """
    counts = {"single": 0, "double": 0, "triple": 0, "hr": 0, "bb_hbp": 0, "so": 0, "other_out": 0}
    collected = 0

    games = list(gamelog.team_games(team_names))
    games.reverse()  # most recent first

    for g in games:
        if collected >= target_pa:
            break
        public_id = gamelog._field(g, "publicGameId", "public_game_id", "public-game-id")
        if not public_id:
            continue
        try:
            gd = cbl_api.get_gameday(public_id)
        except Exception:
            continue
        if not gd or not gd.get("snapshot"):
            continue

        at_bats = [ab for ab in gameday.get_at_bats(gd) if ab.get("batterId") == player_id and ab.get("isComplete")]
        at_bats.reverse()  # most recent PA in this game first

        for ab in at_bats:
            if collected >= target_pa:
                break
            outcome = ab.get("outcome") or ""
            if outcome == "single" or outcome == "bunt_single":
                counts["single"] += 1
            elif outcome in ("double", "ground_rule_double"):
                counts["double"] += 1
            elif outcome == "triple":
                counts["triple"] += 1
            elif outcome == "home_run":
                counts["hr"] += 1
            elif outcome in gameday.WALK_OUTCOMES or outcome == "hit_by_pitch":
                counts["bb_hbp"] += 1
            elif outcome in ("strikeout_looking", "strikeout_swinging"):
                counts["so"] += 1
            elif gameday.is_non_ab_outcome(ab):
                continue  # sac bunts/flies etc -- not a hit, walk, or strikeout; skip from this specific chart
            else:
                counts["other_out"] += 1
            collected += 1

    pct = {k: stats.safe_div(v, collected) for k, v in counts.items()}
    return {"total": collected, "counts": counts, "pct": pct}
