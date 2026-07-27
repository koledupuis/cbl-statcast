"""
First-pitch plate-discipline stats.

Primary source: CBL's own per-game aggregate on `playerBattingStats` /
`playerPitchingStats` -- confirmed present in a real gameday payload:

  playerBattingStats[id].firstPitch  -> {balls, strikes, swings, ...}
  playerPitchingStats[id].firstPitch -> {balls, strikes}

For batters: total first-pitch PAs = balls + strikes (every PA's first
pitch is a ball or a strike in CBL's own tally), and `swings` gives the
swung-at count directly -- no need to classify pitch results ourselves.
For pitchers: strike% = strikes / (balls + strikes).

Fallback: if a game's payload doesn't have these fields, walk the raw
`atBats`/reconstructed at-bat list directly and classify each first
pitch (anything that isn't "ball" or "called_strike" counts as a swing
-- a reasonable proxy, not confirmed against every possible pitch-result
value CBL's feed might emit for a first-pitch ball in play).
"""
import cbl_api
import gameday
import gamelog

TAKE_RESULTS = {"ball", "called_strike"}


def _first_pitch_result(ab):
    pitches = ab.get("pitches") or []
    if not pitches:
        return None
    return pitches[0].get("result")


def _walk_team_games(team_name, season_year, on_game):
    """Shared game-walking loop: calls on_game(gd) for every completed
    game found for team_name."""
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
        on_game(gd)


def build_batter_first_pitch(player_id, team_name, season_year=None):
    """{"pa": int, "first_pitch_swing_pct": float|None, "first_pitch_take_pct": float|None}"""
    counts = {"total": 0, "swings": 0}

    def handle(gd):
        stats = gameday.get_player_batting_stats(gd, player_id)
        if stats is not None and "firstPitch" in stats:
            fp = stats["firstPitch"]
            total = (fp.get("balls") or 0) + (fp.get("strikes") or 0)
            counts["total"] += total
            counts["swings"] += fp.get("swings") or 0
            return

        # Fallback: classify each first pitch from the raw at-bat log.
        for ab in gameday.get_at_bats(gd):
            if ab.get("batterId") != player_id or not ab.get("isComplete"):
                continue
            fp_result = _first_pitch_result(ab)
            if fp_result is None:
                continue
            counts["total"] += 1
            if fp_result not in TAKE_RESULTS:
                counts["swings"] += 1

    _walk_team_games(team_name, season_year, handle)
    total = counts["total"]
    swings = counts["swings"]
    return {
        "pa": total,
        "first_pitch_swing_pct": (swings / total) if total else None,
        "first_pitch_take_pct": ((total - swings) / total) if total else None,
    }


def build_pitcher_first_pitch(player_id, team_name, season_year=None):
    """{"bf": int, "first_pitch_strike_pct": float|None, "first_pitch_ball_pct": float|None}"""
    counts = {"total": 0, "strikes": 0}

    def handle(gd):
        stats = gameday.get_player_pitching_stats(gd, player_id)
        if stats is not None and "firstPitch" in stats:
            fp = stats["firstPitch"]
            balls = fp.get("balls") or 0
            strikes = fp.get("strikes") or 0
            counts["total"] += balls + strikes
            counts["strikes"] += strikes
            return

        # Fallback: classify each first pitch from the raw at-bat log.
        for ab in gameday.get_at_bats(gd):
            if ab.get("pitcherId") != player_id or not ab.get("isComplete"):
                continue
            fp_result = _first_pitch_result(ab)
            if fp_result is None:
                continue
            counts["total"] += 1
            if fp_result != "ball":
                counts["strikes"] += 1

    _walk_team_games(team_name, season_year, handle)
    total = counts["total"]
    strikes = counts["strikes"]
    return {
        "bf": total,
        "first_pitch_strike_pct": (strikes / total) if total else None,
        "first_pitch_ball_pct": ((total - strikes) / total) if total else None,
    }
