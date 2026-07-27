"""
Derived views over the CBL `/feed/public-gameday` payload.

CONFIRMED against a real, complete gameday response: `snapshot.liveGame`
actually carries TWO parallel representations of the same game:

  1. `atBats` -- a pre-built list, one dict per plate appearance, with
     outcome, batterId, pitcherId, fielders, rbiCount, runsScored,
     outsRecorded, baseRunnersBeforePlay, runnersInScoringPosition,
     isLeadoffBatter, isComplete, and a nested `pitches` list (each
     pitch: id, result, timestamp). This is the shape every function in
     this module was originally written against, and it's confirmed
     correct and complete -- it's used as the primary source below.

  2. `events` -- a flat, chronological stream of typed events (`pitch`,
     `at_bat_complete`, `runner_advance`, `inning_change`,
     `substitution`, `position_change`, ...). This carries things
     `atBats` does NOT: stolen bases / caught stealing show up only as
     standalone `runner_advance` events with `cause` of `stolen_base` /
     `caught_stealing` and a `runnerId` (confirmed against real data --
     see get_stolen_base_events() below), and pitching/defensive
     substitutions only show up here too.

get_at_bats() below returns the real `atBats` list directly when present
(the normal case) and only falls back to reconstructing an equivalent
list from `events` if `atBats` is ever missing -- see
_reconstruct_at_bats() -- so this module keeps working even if a future
payload shape only includes one of the two representations.

Outcome vocabulary observed on `atBats` entries (not guaranteed
exhaustive -- anything unrecognized just falls through safely):
  hits:        single, double, triple, home_run, bunt_single
               (bunt_single added based on a user report of a bunt hit
               not counting as a hit -- CBL already breaks out
               sacrifice_bunt as its own outcome distinct from a plain
               ground out, so a bunt that goes for a hit very likely
               gets its own distinct outcome value too, rather than
               being folded into plain "single". The exact string
               hasn't been directly confirmed against a raw payload the
               way the others below have -- if bunt hits are still
               missing after this fix, the actual string CBL uses is
               different from "bunt_single" and needs checking against
               a live response.)
  outs:        strikeout_looking, strikeout_swinging, dropped_third_strike_out,
               fly_out, pop_out, ground_out, line_out, double_play,
               sacrifice_bunt, sacrifice_fly, fielders_choice
  non at-bats: walk, sacrifice_bunt, sacrifice_fly, hit_by_pitch,
               catcher_interference
  other:       error (counts as an AB, not a hit -- confirmed against a
               real fixture; a batter reaching on a fielding error is
               tagged errorType on the at-bat, outcome "error")

Note: a live at-bat can also carry no outcome at all (isComplete: false,
outcome absent) while a plate appearance is still in progress -- callers
that iterate at-bats for a live game should treat a missing/falsy
`outcome` as "not yet decided" rather than an unrecognized value.

Also worth knowing: `liveGame.playerBattingStats[playerId]` and
`playerPitchingStats[playerId]` carry CBL's own pre-aggregated per-game
numbers (stolenBases, caughtStealing, qualityStarts, a `firstPitch`
breakdown, etc.) -- several modules in this app (baserunning.py,
pitching_splits.py, pitch_discipline.py) prefer these authoritative
numbers over re-deriving the same thing from the raw at-bat/event log
where CBL already computed it, falling back to their own derivation
only if a particular game's payload doesn't have that field.
"""
from collections import OrderedDict

HIT_OUTCOMES = {"single", "double", "triple", "home_run", "bunt_single"}
STRIKEOUT_OUTCOMES = {"strikeout_looking", "strikeout_swinging", "dropped_third_strike_out"}
NON_AB_OUTCOMES = {"walk", "sacrifice_bunt", "sacrifice_fly", "hit_by_pitch", "catcher_interference"}
BALL_RESULTS = {"ball"}
STRIKE_RESULTS = {"called_strike", "swinging_strike"}
FOUL_RESULTS = {"foul"}

STOLEN_BASE_CAUSES = {"stolen_base"}
CAUGHT_STEALING_CAUSES = {"caught_stealing"}

# Candidate paths (relative to snapshot.liveGame) for the flat event
# array -- "events" confirmed correct against a real payload; the rest
# are kept as harmless extra guesses in case a different game/season
# ever uses a different key.
EVENT_LIST_KEYS = ("events", "playEvents", "eventLog", "plays", "log")


def _snapshot(gd):
    return gd.get("snapshot") or {}


def _live(gd):
    return _snapshot(gd).get("liveGame") or {}


def _raw_events(gd):
    """The flat, chronological event stream for this game, or None if none
    of the candidate keys were found (caller should fall back safely)."""
    live = _live(gd)
    for key in EVENT_LIST_KEYS:
        events = live.get(key)
        if events:
            return events
    return None


def _reconstruct_at_bats(events):
    """Groups the flat event stream into one dict per plate appearance,
    matching the shape every consumer in this app expects: batterId,
    pitcherId, outcome, outsRecorded, runsScored, rbiCount,
    baseRunnersBeforePlay, pitches (list of {result, balls, strikes}),
    inning, halfInning, isComplete -- plus two fields the real feed
    doesn't provide directly but downstream splits code depends on,
    derived here instead:
      isLeadoffBatter -- True for the first plate appearance closed out
        in a given (inning, halfInning).
      runnersInScoringPosition -- True if baseRunnersBeforePlay shows a
        runner on second or third."""
    at_bats = []
    pending_pitches = []
    pending_inning = None
    pending_half = None
    seen_half_innings = set()

    for ev in events:
        data = ev.get("data") or {}
        etype = data.get("type")

        if etype == "pitch":
            pending_pitches.append({
                "result": data.get("result"),
                "balls": data.get("balls"),
                "strikes": data.get("strikes"),
            })
            pending_inning = ev.get("inning")
            pending_half = ev.get("halfInning")

        elif etype == "at_bat_complete":
            inning = ev.get("inning", pending_inning)
            half = ev.get("halfInning", pending_half)
            half_key = (inning, half)
            is_leadoff = half_key not in seen_half_innings
            seen_half_innings.add(half_key)

            base_runners = data.get("baseRunnersBeforePlay") or {}
            risp = bool(base_runners.get("second") or base_runners.get("third"))

            at_bats.append({
                "inning": inning,
                "halfInning": half,
                "batterId": data.get("batterId"),
                "pitcherId": data.get("pitcherId"),
                "outcome": data.get("outcome"),
                "outsRecorded": data.get("outsRecorded") or 0,
                "runsScored": data.get("runsScored") or [],
                "rbiCount": data.get("rbiCount") or 0,
                "baseRunnersBeforePlay": base_runners,
                "runnersInScoringPosition": risp,
                "isLeadoffBatter": is_leadoff,
                "fielders": data.get("fielders"),
                "errorType": data.get("errorType"),
                "battedBall": data.get("battedBall"),
                "isComplete": True,
                "pitches": pending_pitches,
            })
            pending_pitches = []
            pending_inning = None
            pending_half = None

        # "runner_advance" events are handled separately by
        # get_stolen_base_events() below -- they aren't part of any
        # single at-bat's summary. "inning_change" events aren't needed
        # for reconstruction since every event already self-reports its
        # own inning/halfInning.

    # Any pitches thrown but not yet closed out by an at_bat_complete
    # event (a plate appearance still in progress on a live game) --
    # surface as a trailing incomplete at-bat rather than dropping the
    # pitches on the floor.
    if pending_pitches:
        at_bats.append({
            "inning": pending_inning, "halfInning": pending_half,
            "batterId": None, "pitcherId": None, "outcome": None,
            "outsRecorded": 0, "runsScored": [], "rbiCount": 0,
            "baseRunnersBeforePlay": {}, "runnersInScoringPosition": False,
            "isLeadoffBatter": False, "isComplete": False,
            "pitches": pending_pitches,
        })

    return at_bats


def get_at_bats(gd):
    """Single source of truth for "the list of plate appearances this
    game" -- returns the real `atBats` list directly when present
    (confirmed correct and complete against a real payload), and only
    falls back to reconstructing an equivalent list from the raw
    `events` stream if `atBats` is ever missing. See this module's
    docstring for the full writeup on why both exist."""
    real_at_bats = _live(gd).get("atBats")
    if real_at_bats:
        return real_at_bats
    events = _raw_events(gd)
    if events is not None:
        return _reconstruct_at_bats(events)
    return []


def get_stolen_base_events(gd):
    """[{"runnerId":..., "cause": "stolen_base"|"caught_stealing",
         "inning":..., "halfInning":...}, ...] pulled from "runner_advance"
    events in the raw stream. Returns None (not []) if the raw event
    stream itself wasn't found at all, so callers can distinguish
    "checked, zero steal attempts this game" from "couldn't find any
    event data to check.\""""
    events = _raw_events(gd)
    if events is None:
        return None
    out = []
    for ev in events:
        data = ev.get("data") or {}
        if data.get("type") != "runner_advance":
            continue
        cause = data.get("cause")
        if cause in STOLEN_BASE_CAUSES or cause in CAUGHT_STEALING_CAUSES:
            out.append({
                "runnerId": data.get("runnerId"),
                "cause": cause,
                "inning": ev.get("inning"),
                "halfInning": ev.get("halfInning"),
            })
    return out


def get_extra_scoring_events(gd):
    """[{"runnerId":..., "cause":..., "inning":..., "halfInning":...}, ...]
    -- every "runner_advance" event where `scored` is true.

    IMPORTANT, discovered from a real reported bug: a run can score
    independently of any batter's own plate appearance -- a wild pitch,
    a passed ball, a balk, defensive indifference, or a straight steal
    of home. None of those show up in any `at_bat_complete` event's
    `runsScored` list (that list only reflects runs tied to the CURRENT
    at-bat's own outcome, e.g. a runner driven in by a hit) -- they only
    exist as a `runner_advance` event with `scored: true`. Every run-
    counting function in this app (`build_line_score`, `build_batting_box`)
    previously only read `at_bat_complete`-based `runsScored`, so any game
    where the deciding run scored this way would come out with the wrong
    score entirely -- in the reported case, a real win came out as a
    false tie, which `team_schedule.py` then silently dropped from that
    team's record (ties "shouldn't happen" in baseball, so it's treated
    as a data-entry guard there, not as "this game legitimately doesn't
    count").

    Every function that tallies runs from at-bats now also adds these
    events in. Returns None (not []) if the raw event stream itself
    wasn't found, matching get_stolen_base_events()'s convention.

    Caveat: a `runner_advance` event has no pitcherId, so a run scored
    this way can be credited to the correct TEAM (line score) and the
    correct RUNNER's individual runs-scored stat, but not to a specific
    PITCHER's runs-allowed -- that would need inferring who was on the
    mound at that exact moment from nearby pitch events, which isn't
    done here yet. Pitching stats (ERA, runs allowed) can therefore
    still slightly undercount for a pitcher who allowed a run exactly
    this way, even though the team-level score and the batter's own
    runs-scored are now both correct.
    """
    events = _raw_events(gd)
    if events is None:
        return None
    out = []
    for ev in events:
        data = ev.get("data") or {}
        if data.get("type") != "runner_advance":
            continue
        if not data.get("scored"):
            continue
        out.append({
            "runnerId": data.get("runnerId"),
            "cause": data.get("cause"),
            "inning": ev.get("inning"),
            "halfInning": ev.get("halfInning"),
        })
    return out


def get_player_batting_stats(gd, player_id):
    """CBL's own pre-aggregated per-game batting numbers for one player
    (stolenBases, caughtStealing, firstPitch breakdown, etc.), or None if
    this game's payload doesn't have a playerBattingStats section at all."""
    stats = _live(gd).get("playerBattingStats")
    if not stats:
        return None
    return stats.get(player_id)


def get_player_pitching_stats(gd, player_id):
    """CBL's own pre-aggregated per-game pitching numbers for one pitcher
    (qualityStarts, firstPitch breakdown, etc.), or None if this game's
    payload doesn't have a playerPitchingStats section at all."""
    stats = _live(gd).get("playerPitchingStats")
    if not stats:
        return None
    return stats.get(player_id)


def get_player_fielding_stats(gd, player_id):
    """CBL's own pre-aggregated per-game fielding numbers for one player,
    including a positionStats breakdown when they played more than one
    position in the game (confirmed real shape:
    positionStats.{POSITION} = {games, gamesStarted, putouts, assists,
    fieldingErrors, throwingErrors, doublePlays, triplePlays, ...}),
    or None if this game's payload doesn't have a playerFieldingStats
    section at all."""
    stats = _live(gd).get("playerFieldingStats")
    if not stats:
        return None
    return stats.get(player_id)


def get_venue(gd):
    """The park/venue name for this game, confirmed to exist in CBL's
    real payload as "venue" -- exact casing and nesting depth hasn't
    been independently verified against a live response, so this tries
    several plausible spellings/locations rather than assuming one
    exact path. Returns None if none of them are present, so callers
    can fall back gracefully (e.g. to the home team's name) rather than
    show a blank/broken value.

    If this comes back None on every game once deployed, the real
    field is nested somewhere this function doesn't check yet -- add
    the actual location here once confirmed against a live response."""
    for container in (gd, gd.get("snapshot") or {}, (gd.get("snapshot") or {}).get("setup") or {}):
        for key in ("venue", "venueName", "venue_name", "park", "parkName", "location"):
            val = container.get(key)
            if val:
                return val
    return None


def get_home_plate_umpire(gd):
    """The home plate umpire's name for this game, or None.

    Confirmed against a real populated payload:
      snapshot.setup.umpireAssignments == {"homePlate": "...",
        "firstBase": "...", "thirdBase": "..."} -- a dict mapping role
      directly to name. This is the reliable source and is checked
      first.

    Also confirmed in that same payload: snapshot.setup.umpires is a
    plain list of every umpire's name for the game (3 entries in the
    confirmed example -- home plate, 1st base, 3rd base -- with no
    per-entry role, since the role mapping lives in umpireAssignments
    instead). Kept as a fallback for the case where umpireAssignments
    is missing but umpires isn't -- if there's more than one name with
    no way to tell which was behind the plate, this correctly returns
    None rather than guessing wrong; if there's exactly one, it's used
    directly.
    """
    setup = (gd.get("snapshot") or {}).get("setup") or {}

    assignments = setup.get("umpireAssignments") or {}
    for key in ("homePlate", "home_plate", "homeplate"):
        val = assignments.get(key)
        if val:
            return val

    umpires = setup.get("umpires")
    if not umpires:
        return None

    def _name_of(entry):
        if isinstance(entry, str):
            return entry
        if isinstance(entry, dict):
            for key in ("name", "umpireName", "fullName", "full_name"):
                if entry.get(key):
                    return entry[key]
        return None

    def _is_plate(entry):
        if not isinstance(entry, dict):
            return False
        for key in ("position", "role", "type", "assignment"):
            val = entry.get(key)
            if val and ("plate" in val.lower() or "home" in val.lower()):
                return True
        return False

    for entry in umpires:
        if _is_plate(entry):
            return _name_of(entry)

    if len(umpires) == 1:
        return _name_of(umpires[0])

    return None


def build_player_lookup(gd):
    """id -> roster entry (name, position, jerseyNumber, profileImageUrl, ...) for both teams."""
    snap = _snapshot(gd)
    lookup = {}
    for side in ("awayTeam", "homeTeam"):
        for p in (snap.get(side) or {}).get("roster", []):
            if p.get("id"):
                lookup[p["id"]] = p
    return lookup


def build_line_score(gd):
    """Runs per inning for each team, plus final R/H/E totals."""
    at_bats = get_at_bats(gd)

    away_runs, home_runs = {}, {}
    hits = {"away": 0, "home": 0}
    errors = {"away": 0, "home": 0}
    max_inning = 9

    for ab in at_bats:
        inning = ab.get("inning") or 1
        max_inning = max(max_inning, inning)
        is_home_batting = ab.get("halfInning") == "bottom"
        side = "home" if is_home_batting else "away"

        runs = len(ab.get("runsScored") or [])
        if runs:
            bucket = home_runs if is_home_batting else away_runs
            bucket[inning] = bucket.get(inning, 0) + runs

        if ab.get("outcome") in HIT_OUTCOMES:
            hits[side] += 1

        if ab.get("errorType"):
            # An error is charged to whichever team is on defense.
            errors["away" if is_home_batting else "home"] += 1

    # Runs that scored independently of any at-bat's own outcome (wild
    # pitch, passed ball, balk, steal of home) -- see get_extra_scoring_
    # events()'s docstring for why this matters: without this, a game
    # decided by exactly this kind of run comes out with the wrong
    # score entirely.
    for ev in (get_extra_scoring_events(gd) or []):
        inning = ev.get("inning") or 1
        max_inning = max(max_inning, inning)
        is_home_batting = ev.get("halfInning") == "bottom"
        bucket = home_runs if is_home_batting else away_runs
        bucket[inning] = bucket.get(inning, 0) + 1

    innings = list(range(1, max_inning + 1))

    def team_block(runs_by_inning, side):
        line = [runs_by_inning.get(i, 0) for i in innings]
        return {"line": line, "runs": sum(line), "hits": hits[side], "errors": errors[side]}

    return {
        "innings": innings,
        "away": team_block(away_runs, "away"),
        "home": team_block(home_runs, "home"),
    }


def _roster_ids(gd, side):
    return {p.get("id") for p in (_snapshot(gd).get(side) or {}).get("roster", []) if p.get("id")}


def _batting_order(gd, side):
    lineup = (_snapshot(gd).get(side) or {}).get("startingLineup", [])
    return {p["id"]: p.get("battingOrder", 99) for p in lineup if p.get("id")}


def build_batting_box(gd, lookup):
    """Per-player batting line for each team: AB, R, H, 2B, 3B, HR, RBI, BB, SO.

    Team side is determined per at-bat from its own `halfInning` field
    ("top" = away team batting, "bottom" = home team batting) -- NOT from
    whether the player happens to appear in that game's roster array.
    Roster data has been observed incomplete for some games (a real
    player's real hits computed correctly from the play-by-play then
    silently dropped because their ID wasn't in the roster listing for
    that specific game) -- halfInning is self-contained in the at-bat
    record itself and can't have that failure mode. `lookup` (built from
    the roster) is still used, but only for display enrichment (name,
    position), with a safe fallback to the raw ID if a player is missing
    from it."""
    at_bats = get_at_bats(gd)
    rows = {"away": {}, "home": {}}

    def row(side, pid):
        return rows[side].setdefault(pid, {"ab": 0, "r": 0, "h": 0, "doubles": 0, "triples": 0,
                                            "hr": 0, "rbi": 0, "bb": 0, "so": 0})

    for ab in at_bats:
        pid = ab.get("batterId")
        if not pid:
            continue
        side = "away" if (ab.get("halfInning") or "top") == "top" else "home"
        outcome = ab.get("outcome", "")
        r = row(side, pid)
        if outcome not in NON_AB_OUTCOMES:
            r["ab"] += 1
        if outcome in HIT_OUTCOMES:
            r["h"] += 1
            if outcome == "double":
                r["doubles"] += 1
            elif outcome == "triple":
                r["triples"] += 1
            elif outcome == "home_run":
                r["hr"] += 1
        elif outcome == "walk":
            r["bb"] += 1
        if outcome in STRIKEOUT_OUTCOMES:
            r["so"] += 1
        r["rbi"] += ab.get("rbiCount") or 0
        for scorer_id in ab.get("runsScored") or []:
            row(side, scorer_id)["r"] += 1

    # Runs scored independently of any at-bat (wild pitch, passed ball,
    # balk, steal of home) -- see gameday.get_extra_scoring_events()'s
    # docstring. No RBI is credited for these (there's no batter to
    # credit one to), only the runner's own run.
    for ev in (get_extra_scoring_events(gd) or []):
        runner_id = ev.get("runnerId")
        if not runner_id:
            continue
        side = "away" if (ev.get("halfInning") or "top") == "top" else "home"
        row(side, runner_id)["r"] += 1

    def to_rows(side, snapshot_key):
        order = _batting_order(gd, snapshot_key)
        out = []
        for pid, stat in rows[side].items():
            info = lookup.get(pid, {})
            out.append({"playerId": pid, "name": info.get("name", pid),
                        "position": info.get("position", ""), **stat})
        out.sort(key=lambda x: order.get(x["playerId"], 99))
        return out

    return {"away": to_rows("away", "awayTeam"), "home": to_rows("home", "homeTeam")}


def build_pitching_box(gd, lookup):
    """Per-pitcher line for each team: IP, H, R, BB, SO, HR, pitch count.

    Team side is the DEFENSIVE side for each at-bat, derived from
    halfInning the same way build_batting_box derives the batting side
    (and for the same reason -- not gated on roster completeness)."""
    at_bats = get_at_bats(gd)
    rows = {"away": {}, "home": {}}
    appearance_order = {"away": [], "home": []}

    def row(side, pid):
        if pid not in rows[side]:
            appearance_order[side].append(pid)
            rows[side][pid] = {"outs": 0, "h": 0, "r": 0, "bb": 0, "so": 0, "hr": 0, "pitches": 0}
        return rows[side][pid]

    for ab in at_bats:
        pid = ab.get("pitcherId")
        if not pid:
            continue
        # Pitcher is on defense: "top" half (away batting) means home is pitching.
        side = "home" if (ab.get("halfInning") or "top") == "top" else "away"
        r = row(side, pid)
        r["outs"] += ab.get("outsRecorded") or 0
        r["pitches"] += len(ab.get("pitches") or [])
        outcome = ab.get("outcome", "")
        if outcome in HIT_OUTCOMES:
            r["h"] += 1
            if outcome == "home_run":
                r["hr"] += 1
        elif outcome == "walk":
            r["bb"] += 1
        if outcome in STRIKEOUT_OUTCOMES:
            r["so"] += 1
        r["r"] += len(ab.get("runsScored") or [])

    def to_rows(side):
        out = []
        for pid in appearance_order[side]:
            info = lookup.get(pid, {})
            stat = rows[side][pid]
            outs = stat["outs"]
            out.append({
                "playerId": pid,
                "name": info.get("name", pid),
                "ip": f"{outs // 3}.{outs % 3}",
                **stat,
            })
        return out

    return {"away": to_rows("away"), "home": to_rows("home")}


def _pitch_sequence(pitches):
    """Annotate each pitch with the count it was thrown on and a short label."""
    seq = []
    balls = strikes = 0
    for p in pitches or []:
        result = p.get("result", "")
        seq.append({"result": result, "count": f"{balls}-{strikes}"})
        if result in BALL_RESULTS:
            balls += 1
        elif result in STRIKE_RESULTS:
            strikes = min(strikes + 1, 3)
        elif result in FOUL_RESULTS:
            strikes = min(strikes + 1, 2)
    return seq


def build_play_by_play(gd, lookup):
    """OrderedDict: inning -> {"top": [...], "bottom": [...]} of at-bat summaries."""
    at_bats = get_at_bats(gd)
    innings = OrderedDict()

    for ab in at_bats:
        inning = ab.get("inning") or 1
        half = ab.get("halfInning") or "top"
        innings.setdefault(inning, {"top": [], "bottom": []})

        batter = lookup.get(ab.get("batterId"), {})
        pitcher = lookup.get(ab.get("pitcherId"), {})
        outcome = ab.get("outcome") or ""

        innings[inning][half].append({
            "batter": batter.get("name", ab.get("batterId")),
            "pitcher": pitcher.get("name", ab.get("pitcherId")),
            "outcome": outcome,
            "outcome_label": outcome.replace("_", " ").capitalize(),
            "rbi": ab.get("rbiCount") or 0,
            "runs_scored": [lookup.get(rid, {}).get("name", rid) for rid in (ab.get("runsScored") or [])],
            "pitches": _pitch_sequence(ab.get("pitches")),
            "pitch_count": len(ab.get("pitches") or []),
            "batted_ball": ab.get("battedBall"),
            "is_error": bool(ab.get("errorType")),
        })

    return innings
