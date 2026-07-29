"""
Season-long split tables for one batter, in the spirit of a Savant/
Baseball-Reference "Splits" page: baserunner state, outs, inning,
batting-order slot, home/away, day/night, and month.

Built the same way gamelog.py builds a game log: walk every completed
game for the player's team (gamelog.team_games), pull each game's raw
at-bat log (cbl_api.get_gameday, already cached), and keep only the
at-bats where this player is the batter. No new network surface --
this rides on the same cached gameday fetches gamelog.py triggers.

NOT built here: platoon splits (vs L/R). CBL's own
`/players/<id>/analytics` endpoint already returns those directly as
event_batting.splits.byPitcherHandedness -- app.py/player.html render
that split from `analytics`, not from this module.

Known gaps (feed doesn't carry the data, so these are left out rather
than faked):
  - Stolen bases / caught stealing: no SB/CS event in the outcome
    vocabulary today.
  - "Late & Close" game-type split: would need an inning-by-inning
    score differential, which isn't computed anywhere in this app yet.

Caveat shared with gamelog.py: PA here is treated as AB + BB (HBP and
sac plays aren't folded into PA/OBP), matching the convention already
used by gamelog._game_rates -- kept consistent rather than "more
correct but different from the Game Log tab" on the same page.

Batting-order splits only count plate appearances where the player
was in that game's *starting* lineup slot; a PA by a substitute who
entered without a starting battingOrder is simply skipped from that
one bucket (it still counts everywhere else -- baserunner state, outs,
inning, home/away, etc).
"""
from collections import OrderedDict

import cbl_api
import gameday
import gamelog
import stats

COUNTING_KEYS = ("pa", "ab", "r", "h", "doubles", "triples", "hr", "rbi", "bb", "so", "hbp")

DEEP_COUNT_PITCHES = 6  # "Deep Pitch Count" criterion -- the lower end of the commonly-cited 6-8+ range
BATTLE_BACK_PITCHES_AFTER_0_2 = 3  # "Battling Back" -- surviving this many more pitches after reaching an 0-2 count

LATE_CLOSE_INNING = 7      # 7th inning or later
LATE_CLOSE_MARGIN = 2      # score within this many runs (either direction) at the start of the PA
# "Late & Close" here is a SIMPLIFICATION of the traditional (Elias Sports
# Bureau) definition, which also requires the tying run to specifically be
# on base, at bat, or on deck -- not just "within N runs." This app doesn't
# track baserunner identity relative to the batting order closely enough
# to reconstruct that nuance reliably, so it sticks to the simpler
# inning + score-margin bar instead of pretending to be the official stat.
# Score is reconstructed by walking every at-bat in the game chronologically
# (not just this player's own) and crediting runs by half-inning as they're
# recorded -- known gap: doesn't include runs that scored via a wild pitch/
# passed ball/steal of home between at-bats (see gameday.get_extra_scoring_events,
# used for the site's own line score but not wired in here), so the
# reconstructed score can undercount by a run or two in that specific,
# fairly rare scenario.

QPA_STRIKE_RESULTS = ("called_strike", "swinging_strike")

# Display order for Count Splits -- every legal ball-strike count a
# plate appearance can end on, in the order broadcasters/scorekeepers
# conventionally read them (balls first, ascending strikes within each
# ball count), not whatever order they happen to be first encountered
# while walking a season.
_CANONICAL_COUNTS = [
    "0-0", "0-1", "0-2",
    "1-0", "1-1", "1-2",
    "2-0", "2-1", "2-2",
    "3-0", "3-1", "3-2",
]

QPA_FOUL_RESULTS = ("foul", "foul_bunt")


def _final_count(pitches):
    """Ball-strike count (e.g. '3-2', '0-2') the batter was actually
    facing when the plate appearance's LAST pitch was thrown -- i.e.
    the count the real-world outcome happened on ("struck out on a
    3-2 pitch" means the count WAS 3-2 before that final pitch
    arrived). None if there are no recorded pitches for this at-bat
    at all. Same count-reconstruction rules used elsewhere in this
    module (a foul doesn't add a 3rd strike once already at 2)."""
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
        elif result in QPA_STRIKE_RESULTS:
            strikes = min(strikes + 1, 2)
        elif result in QPA_FOUL_RESULTS:
            if strikes < 2:
                strikes += 1
    return f"{balls}-{strikes}"


def _reached_two_strikes(pitches):
    """Did this plate appearance's count ever reach 2 strikes, regardless
    of the ball count (0-2, 1-2, 2-2, or 3-2 all qualify)? Same count-
    reconstruction rules as _reached_0_2_and_battled (a foul doesn't add
    a 3rd strike once already at 2)."""
    strikes = 0
    for p in (pitches or []):
        result = (p.get("result") or "").lower()
        if result in QPA_STRIKE_RESULTS:
            strikes = min(strikes + 1, 2)
        elif result in QPA_FOUL_RESULTS:
            if strikes < 2:
                strikes += 1
        if strikes == 2:
            return True
    return False


def _reached_0_2_and_battled(pitches):
    """"Battling Back": did this plate appearance reach an 0-2 count
    (0 balls, 2 strikes) and then survive BATTLE_BACK_PITCHES_AFTER_0_2
    or more additional pitches past that point (fouls off, taken
    pitches, etc.)? Walks the real pitch-by-pitch sequence and
    reconstructs the running count using standard count rules (a foul
    doesn't add a 3rd strike once already at 2 strikes)."""
    balls = 0
    strikes = 0
    reached_at = None
    for i, p in enumerate(pitches or []):
        result = (p.get("result") or "").lower()
        if result == "ball":
            balls += 1
        elif result in QPA_STRIKE_RESULTS:
            strikes = min(strikes + 1, 2)
        elif result in QPA_FOUL_RESULTS:
            if strikes < 2:
                strikes += 1
        if reached_at is None and balls == 0 and strikes == 2:
            reached_at = i
    if reached_at is None:
        return False
    return (len(pitches) - (reached_at + 1)) >= BATTLE_BACK_PITCHES_AFTER_0_2


def _is_quality_pa(ab):
    """A plate appearance counts as "quality" if it meets at least one
    of these criteria (coaching definition, not a sabermetric one --
    see build_player_splits docstring for the full list and what's
    NOT included):

      - Base hit (any outcome in gameday.HIT_OUTCOMES)
      - Walk or hit-by-pitch
      - Hard-hit ball, even if it results in an out -- uses the real
        per-at-bat battedBall.quality field (confirmed "hard"/"medium"/
        "soft" in real payload data), not an estimate
      - A sacrifice fly or sacrifice bunt -- CBL's own scorekeeping
        already distinguishes these from a plain out, so this doesn't
        need to independently verify a runner advanced
      - Any plate appearance where a run scored (runsScored non-empty),
        covering productive outs beyond just sacrifices
      - Deep pitch count (DEEP_COUNT_PITCHES or more pitches seen)
      - Battling back from an 0-2 count (see _reached_0_2_and_battled)

    NOT included: a plain groundout/flyout that merely advances a
    runner a base WITHOUT scoring (e.g. runner 1st-to-2nd on a
    groundout with one out). That needs a before/after base-state
    comparison this feed doesn't carry (baseRunnersBeforePlay has no
    "after" counterpart) -- rather than guess, this criterion is
    limited to plays that actually scored a run or were already
    scored as a sacrifice by the scorekeeper.
    """
    outcome = ab.get("outcome") or ""
    if outcome in gameday.HIT_OUTCOMES:
        return True
    if outcome in gameday.WALK_OUTCOMES or outcome == "hit_by_pitch":
        return True
    if outcome in ("sacrifice_fly", "sacrifice_bunt"):
        return True
    if len(ab.get("runsScored") or []) > 0:
        return True
    batted_ball = ab.get("battedBall") or {}
    if (batted_ball.get("quality") or "").lower() == "hard":
        return True
    pitches = ab.get("pitches") or []
    if len(pitches) >= DEEP_COUNT_PITCHES:
        return True
    if _reached_0_2_and_battled(pitches):
        return True
    return False

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

OUTS_LABELS = OrderedDict([(0, "No Outs"), (1, "One Out"), (2, "Two Outs")])

_BASE_STATE_MAP = {
    (): "empty",
    ("first",): "1st",
    ("second",): "2nd",
    ("third",): "3rd",
    ("first", "second"): "1st_2nd",
    ("first", "third"): "1st_3rd",
    ("second", "third"): "2nd_3rd",
    ("first", "second", "third"): "loaded",
}


def _blank():
    return {k: 0 for k in COUNTING_KEYS}


def _base_state_key(before):
    before = before or {}
    on = tuple(sorted(b for b in ("first", "second", "third") if before.get(b)))
    return _BASE_STATE_MAP.get(on, "empty")


def _is_night(game_time):
    """Rough day/night cut on the printed gameTime (e.g. '2:00 PM'); 6pm+ counts as night."""
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


def _ordinal(n):
    suf = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suf}"


def _accumulate(totals, outcome, is_ab, scored, rbi):
    totals["pa"] += 1
    if is_ab:
        totals["ab"] += 1
    if scored:
        totals["r"] += 1
    if outcome in gameday.HIT_OUTCOMES:
        totals["h"] += 1
        if outcome in ("double", "ground_rule_double"):
            totals["doubles"] += 1
        elif outcome == "triple":
            totals["triples"] += 1
        elif outcome == "home_run":
            totals["hr"] += 1
    elif outcome in gameday.WALK_OUTCOMES:
        totals["bb"] += 1
    elif outcome == "hit_by_pitch":
        totals["hbp"] += 1
    if outcome in gameday.STRIKEOUT_OUTCOMES:
        totals["so"] += 1
    totals["rbi"] += rbi


def _rates(t):
    ab = t.get("ab") or 0
    h = t.get("h") or 0
    bb = t.get("bb") or 0
    doubles = t.get("doubles") or 0
    triples = t.get("triples") or 0
    hr = t.get("hr") or 0
    singles = max(h - doubles - triples - hr, 0)
    tb = singles + 2 * doubles + 3 * triples + 4 * hr
    obp = stats.safe_div(h + bb, t.get("pa") or 0)
    slg = stats.safe_div(tb, ab)
    return {"avg": stats.safe_div(h, ab), "obp": obp, "slg": slg, "ops": obp + slg}


def _finalize(totals):
    totals.update(_rates(totals))
    return totals


def _accumulate_team(totals, outcome, is_ab, runs, rbi):
    """Same idea as _accumulate, but for a TEAM total rather than one
    player: "runs" here is the actual count of runs scored on this
    play (could be more than 1 -- a grand slam scores 4 different
    players on one play), not a single player's own 0/1 "did I score"
    boolean. Kept as a separate function rather than changing
    _accumulate's signature, since _accumulate's boolean is the
    correct semantic for a single player and every existing caller
    depends on that."""
    totals["pa"] += 1
    if is_ab:
        totals["ab"] += 1
    totals["r"] += runs
    if outcome in gameday.HIT_OUTCOMES:
        totals["h"] += 1
        if outcome in ("double", "ground_rule_double"):
            totals["doubles"] += 1
        elif outcome == "triple":
            totals["triples"] += 1
        elif outcome == "home_run":
            totals["hr"] += 1
    elif outcome in gameday.WALK_OUTCOMES:
        totals["bb"] += 1
    elif outcome == "hit_by_pitch":
        totals["hbp"] += 1
    if outcome in gameday.STRIKEOUT_OUTCOMES:
        totals["so"] += 1
    totals["rbi"] += rbi


def build_team_situational_batting(team_name, season_year=None):
    """Team-level version of the Baserunner Splits build_player_splits
    builds per player -- every plate appearance by ANY of this team's
    batters, not just one player's own at-bats. Covers both "Team RISP"
    and "Team w/ Bases Loaded" in one pass: "Bases Loaded" is already
    one of the 8 base-state buckets, same as it is for a single player.

    Walks the team's schedule ONCE (not once per player, which would
    refetch the exact same games' gameday JSON once per roster spot for
    no reason) -- for each game, only counts at-bats from whichever
    half-inning this specific team actually bats in (bottom if they're
    home, top if they're away), so the opponent's at-bats in the same
    game are correctly excluded.

    Returns a flat list of (label, totals) tuples -- the same shape
    build_player_splits' own baserunner_rows list uses (8 base-state
    rows, then one final "Scoring Position" row) -- so the page can
    reuse that exact rendering pattern.
    """
    baserunners = OrderedDict((k, _blank()) for k in BASE_STATE_LABELS)
    risp = _blank()

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
        team_half = "bottom" if is_home else "top"

        for ab in gameday.get_at_bats(gd):
            half = ab.get("halfInning") or "top"
            if half != team_half or not ab.get("isComplete"):
                continue

            outcome = ab.get("outcome") or ""
            is_ab = not gameday.is_non_ab_outcome(ab)
            runs = len(ab.get("runsScored") or [])
            rbi = ab.get("rbiCount") or 0
            before = ab.get("baseRunnersBeforePlay")

            state_key = _base_state_key(before)
            _accumulate_team(baserunners[state_key], outcome, is_ab, runs, rbi)
            if ab.get("runnersInScoringPosition"):
                _accumulate_team(risp, outcome, is_ab, runs, rbi)

    for totals in list(baserunners.values()) + [risp]:
        _finalize(totals)

    baserunner_rows = [(label, baserunners[key]) for key, label in BASE_STATE_LABELS.items()]
    baserunner_rows.append(("Scoring Position", risp))
    return baserunner_rows


def build_player_splits(player_id, team_name, season_year=None):
    """
    Returns (sections, quality_pa):

    sections -- an OrderedDict of section_name -> list of (label, totals)
    rows, ready to render as one stat-table per section:
      "Monthly Splits", "Batting Order Splits", "Baserunner Splits",
      "Outs Splits", "Inning Splits", "Game Type Splits", "Two-Strike Splits",
      "Late & Close Splits", "Count Splits"
    Each `totals` dict has the raw counting stats plus avg/obp/slg/ops.

    quality_pa -- {"total_pa": int, "quality_pa_count": int, "pct": float|None}.
    Uses the standard coaching definition of a Quality Plate Appearance
    (a PA meeting at least one of several criteria), not a narrower
    proxy -- see _is_quality_pa()'s own docstring for the exact list
    and, importantly, the one criterion that's deliberately left out
    (a plain groundout that merely advances a runner without scoring,
    which this feed's at-bat data can't distinguish from a
    non-productive groundout).

    batted_ball_profile -- {"total": int, "by_type": {...}, "by_type_pct":
    {...}, "by_direction": {...}, "by_direction_pct": {...}}. Built from
    the real per-at-bat `battedBall.type`/`.direction` fields (confirmed
    in a real payload), counted only on at-bats where CBL actually
    recorded a battedBall entry -- not every plate appearance has one
    (a strikeout or walk has no batted ball at all), so `total` here is
    smaller than total_pa above, and percentages are of batted balls,
    not of all plate appearances.
    """
    months = OrderedDict()
    batting_order = OrderedDict()
    baserunners = OrderedDict((k, _blank()) for k in BASE_STATE_LABELS)
    risp = _blank()
    two_strikes = _blank()
    late_close = _blank()
    counts = {}
    outs = OrderedDict((k, _blank()) for k in OUTS_LABELS)
    innings = OrderedDict()
    game_type = OrderedDict([
        ("home", _blank()), ("away", _blank()),
        ("day", _blank()), ("night", _blank()),
        ("leadoff", _blank()),
    ])
    total_pa = 0
    quality_pa_count = 0
    # Confirmed real per-at-bat keys (snake_case) -- NOT the same casing as
    # the season-aggregate battedBalls.byType breakdown used elsewhere in
    # this app (stats.py), which is camelCase. Verified against a real
    # uploaded game file: ground_ball / line_drive / fly_ball / (popup
    # presumed, not seen in that specific game's sample).
    bb_by_type = OrderedDict([("ground_ball", 0), ("line_drive", 0), ("fly_ball", 0), ("popup", 0), ("unknown", 0)])
    bb_by_direction = OrderedDict([("left", 0), ("center", 0), ("right", 0), ("unknown", 0)])
    bb_total = 0

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
        side = "home" if is_home else "away"

        date_str = gamelog._field(g, "gameDate", "game_date", "game-date", default="")
        game_time = gamelog._field(g, "gameTime", "game_time", "game-time", default="")
        is_night = _is_night(game_time)

        order_lookup = gameday._batting_order(gd, "homeTeam" if is_home else "awayTeam")
        player_order = order_lookup.get(player_id)

        at_bats = gameday.get_at_bats(gd)
        outs_before = {}  # (inning, half) -> cumulative outs already on the board
        away_score = 0
        home_score = 0  # both reset per game -- running score reconstruction for Late & Close

        for ab in at_bats:
            inning = ab.get("inning") or 1
            half = ab.get("halfInning") or "top"
            inning_key = (inning, half)
            cur_outs = outs_before.get(inning_key, 0)

            if ab.get("batterId") == player_id and ab.get("isComplete"):
                outcome = ab.get("outcome") or ""
                is_ab = not gameday.is_non_ab_outcome(ab)
                scored = player_id in (ab.get("runsScored") or [])
                rbi = ab.get("rbiCount") or 0

                total_pa += 1
                if _is_quality_pa(ab):
                    quality_pa_count += 1

                batted_ball = ab.get("battedBall") or {}
                if batted_ball:
                    bb_total += 1
                    bb_type = batted_ball.get("type") or "unknown"
                    bb_by_type[bb_type] = bb_by_type.get(bb_type, 0) + 1
                    bb_dir = batted_ball.get("direction") or "unknown"
                    bb_by_direction[bb_dir] = bb_by_direction.get(bb_dir, 0) + 1

                month = months.setdefault(gamelog._month_name(date_str), _blank())
                _accumulate(month, outcome, is_ab, scored, rbi)

                if player_order is not None:
                    slot_label = f"Batting {_ordinal(player_order)}"
                    slot = batting_order.setdefault(slot_label, _blank())
                    _accumulate(slot, outcome, is_ab, scored, rbi)

                _accumulate(baserunners[_base_state_key(ab.get("baseRunnersBeforePlay"))],
                            outcome, is_ab, scored, rbi)
                if ab.get("runnersInScoringPosition"):
                    _accumulate(risp, outcome, is_ab, scored, rbi)
                if _reached_two_strikes(ab.get("pitches")):
                    _accumulate(two_strikes, outcome, is_ab, scored, rbi)
                final_count = _final_count(ab.get("pitches"))
                if final_count:
                    _accumulate(counts.setdefault(final_count, _blank()), outcome, is_ab, scored, rbi)
                if inning >= LATE_CLOSE_INNING and abs(away_score - home_score) <= LATE_CLOSE_MARGIN:
                    _accumulate(late_close, outcome, is_ab, scored, rbi)

                _accumulate(outs[min(cur_outs, 2)], outcome, is_ab, scored, rbi)

                inning_label = f"{_ordinal(inning)} Inning" if inning <= 9 else "Extra Innings"
                inning_bucket = innings.setdefault(inning_label, _blank())
                _accumulate(inning_bucket, outcome, is_ab, scored, rbi)

                _accumulate(game_type["home" if is_home else "away"], outcome, is_ab, scored, rbi)
                if is_night is not None:
                    _accumulate(game_type["night" if is_night else "day"], outcome, is_ab, scored, rbi)
                if ab.get("isLeadoffBatter"):
                    _accumulate(game_type["leadoff"], outcome, is_ab, scored, rbi)

            if ab.get("isComplete"):
                runs_this_play = len(ab.get("runsScored") or [])
                if runs_this_play:
                    if half == "top":
                        away_score += runs_this_play
                    else:
                        home_score += runs_this_play

            outs_before[inning_key] = cur_outs + (ab.get("outsRecorded") or 0)

    for bucket in (months, batting_order, innings):
        for totals in bucket.values():
            _finalize(totals)
    for totals in baserunners.values():
        _finalize(totals)
    _finalize(risp)
    _finalize(two_strikes)
    _finalize(late_close)
    for totals in counts.values():
        _finalize(totals)
    for totals in outs.values():
        _finalize(totals)
    for totals in game_type.values():
        _finalize(totals)

    baserunner_rows = [(label, baserunners[key]) for key, label in BASE_STATE_LABELS.items()]
    baserunner_rows.append(("Scoring Position", risp))

    sections = OrderedDict([
        ("Monthly Splits", list(months.items())),
        ("Batting Order Splits", list(batting_order.items())),
        ("Baserunner Splits", baserunner_rows),
        ("Outs Splits", [(label, outs[key]) for key, label in OUTS_LABELS.items()]),
        ("Inning Splits", list(innings.items())),
        ("Game Type Splits", [
            ("Home Games", game_type["home"]),
            ("Away Games", game_type["away"]),
            ("Day Games", game_type["day"]),
            ("Night Games", game_type["night"]),
            ("Leading Off Inning", game_type["leadoff"]),
        ]),
        ("Two-Strike Splits", [("With 2 Strikes", two_strikes)]),
        ("Late & Close Splits", [("Late & Close", late_close)]),
        ("Count Splits", [
            (count, counts[count]) for count in _CANONICAL_COUNTS if count in counts
        ]),
    ])
    quality_pa = {
        "total_pa": total_pa, "quality_pa_count": quality_pa_count,
        "pct": stats.safe_div(quality_pa_count, total_pa),
    }
    batted_ball_profile = {
        "total": bb_total,
        "by_type": bb_by_type,
        "by_type_pct": OrderedDict((k, stats.safe_div(v, bb_total)) for k, v in bb_by_type.items()),
        "by_direction": bb_by_direction,
        "by_direction_pct": OrderedDict((k, stats.safe_div(v, bb_total)) for k, v in bb_by_direction.items()),
    }
    return sections, quality_pa, batted_ball_profile


def build_team_situational_batting(team_name, season_year=None):
    """Team-level version of the RISP / base-state splits already built
    per-player above -- but built as a dedicated single pass over the
    team's own schedule, crediting every at-bat where the TEAM was
    actually batting (using is_home + halfInning, not a specific
    player_id), rather than calling build_player_splits() once per
    batter on the roster. That approach would work (in-memory gameday
    caching means the network cost is shared across players, since
    they're walking the same games), but it'd build a full Monthly/
    Outs/Inning/Game-Type breakdown for every single batter just to
    extract one baserunner-state bucket from each and sum them --
    wasted computation this walks past directly instead.

    Run counting: unlike the per-player version (where "scored" is a
    0-or-1 flag -- a batter can only score once on their own plate
    appearance), a team-level "R" column needs the REAL run count per
    play, since a single at-bat can score multiple teammates at once
    (a grand slam scores 4, not 1). _accumulate() itself always adds
    exactly 1 when its "scored" flag is truthy, so this calls it with
    scored=False for every play (letting it handle H/AB/BB/SO/RBI/etc.
    correctly) and adds the real run count separately.

    Returns a list of (label, totals) rows, same shape as one of
    build_player_splits' baserunner_rows, ready for the same table
    rendering used elsewhere on the site.
    """
    baserunners = OrderedDict((k, _blank()) for k in BASE_STATE_LABELS)
    risp = _blank()

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
        our_half = "bottom" if is_home else "top"

        for ab in gameday.get_at_bats(gd):
            if ab.get("halfInning") != our_half or not ab.get("isComplete"):
                continue
            outcome = ab.get("outcome") or ""
            is_ab = not gameday.is_non_ab_outcome(ab)
            runs_this_play = len(ab.get("runsScored") or [])
            rbi = ab.get("rbiCount") or 0

            bucket = baserunners[_base_state_key(ab.get("baseRunnersBeforePlay"))]
            _accumulate(bucket, outcome, is_ab, False, rbi)
            bucket["r"] += runs_this_play

            if ab.get("runnersInScoringPosition"):
                _accumulate(risp, outcome, is_ab, False, rbi)
                risp["r"] += runs_this_play

    for totals in baserunners.values():
        _finalize(totals)
    _finalize(risp)

    rows = [(label, baserunners[key]) for key, label in BASE_STATE_LABELS.items()]
    rows.append(("Scoring Position", risp))
    return rows
