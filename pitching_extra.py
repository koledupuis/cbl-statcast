"""
Season-aggregated pitcher stats, built by walking a pitcher's completed
games and summing CBL's own real per-game fields on
`liveGame.playerPitchingStats[pitcherId]` -- same pattern already used
by pitching_splits.py's Quality Starts and baserunning.py's stolen
bases: prefer CBL's own server-computed per-game numbers over
re-deriving them from the raw at-bat/pitch log, since CBL has more
context (e.g. true earned-run tracking) than this app does.

Confirmed real per-game fields used here (verified against a real
gameday payload, not assumed):
  pitchCount, balls, strikes, fouls, strikesLooking, strikesSwinging,
  strikeoutsLooking, strikeoutsSwinging, battersFaced, hits, walks,
  hitBatters, singlesAllowed, doublesAllowed, triplesAllowed,
  homeRunsAllowed, sacrificeBuntsAllowed, sacrificeFliesAllowed, runs,
  holds, saveOpportunities, blownSaves, inheritedRunners,
  inheritedRunnersScored.

Two confirmed relationships worth knowing before touching this file:
  - pitchCount == balls + strikes (every pitch is one or the other)
  - strikes == strikesLooking + strikesSwinging + fouls (a foul ball
    counts as a "strike" pitch-result-wise, distinct from the at-bat-
    level strikeoutsLooking/strikeoutsSwinging outcome counts)

Games this app can't find a playerPitchingStats entry for are simply
skipped rather than treated as zero -- so a missing game undercounts
silently rather than crashing, consistent with the rest of this app's
"degrade gracefully" philosophy. If EVERY game is missing this section,
build_pitcher_extra_stats() returns None so the caller can show an
empty state instead of a wall of zeroes.
"""
import analytics
import cbl_api
import gameday
import gamelog


def _blank():
    return {
        "pitch_count": 0, "balls": 0, "strikes": 0, "fouls": 0,
        "strikes_looking": 0, "strikes_swinging": 0,
        "strikeouts_looking": 0, "strikeouts_swinging": 0,
        "bf": 0, "h": 0, "bb": 0, "hbp": 0, "runs": 0,
        "singles": 0, "doubles": 0, "triples": 0, "hr": 0,
        "sac_bunts": 0, "sac_flies": 0,
        "holds": 0, "save_opportunities": 0, "blown_saves": 0,
        "inherited_runners": 0, "inherited_runners_scored": 0,
    }


def _accumulate(totals, pstats):
    hits_allowed = pstats.get("hits") or 0
    doubles = pstats.get("doublesAllowed") or 0
    triples = pstats.get("triplesAllowed") or 0
    hr = pstats.get("homeRunsAllowed") or 0
    singles = pstats.get("singlesAllowed")
    if singles is None:
        singles = max(hits_allowed - doubles - triples - hr, 0)

    totals["pitch_count"] += pstats.get("pitchCount") or 0
    totals["balls"] += pstats.get("balls") or 0
    totals["strikes"] += pstats.get("strikes") or 0
    totals["fouls"] += pstats.get("fouls") or 0
    totals["strikes_looking"] += pstats.get("strikesLooking") or 0
    totals["strikes_swinging"] += pstats.get("strikesSwinging") or 0
    totals["strikeouts_looking"] += pstats.get("strikeoutsLooking") or 0
    totals["strikeouts_swinging"] += pstats.get("strikeoutsSwinging") or 0
    totals["bf"] += pstats.get("battersFaced") or 0
    totals["h"] += hits_allowed
    totals["bb"] += pstats.get("walks") or 0
    totals["hbp"] += pstats.get("hitBatters") or 0
    totals["runs"] += pstats.get("runs") or 0
    totals["singles"] += singles
    totals["doubles"] += doubles
    totals["triples"] += triples
    totals["hr"] += hr
    totals["sac_bunts"] += pstats.get("sacrificeBuntsAllowed") or 0
    totals["sac_flies"] += pstats.get("sacrificeFliesAllowed") or 0
    totals["holds"] += pstats.get("holds") or 0
    totals["save_opportunities"] += pstats.get("saveOpportunities") or 0
    totals["blown_saves"] += pstats.get("blownSaves") or 0
    totals["inherited_runners"] += pstats.get("inheritedRunners") or 0
    totals["inherited_runners_scored"] += pstats.get("inheritedRunnersScored") or 0


def build_pitcher_extra_stats(player_id, team_name, p_row=None, season_year=None):
    """
    Returns None if no game this season had a playerPitchingStats entry
    for this player at all (nothing to report). Otherwise returns a
    dict with opponent triple-slash, pitch-level rates, and reliever
    stats -- see the keys below.

    `p_row` (the player's row from cbl_api.get_pitching(), if you have
    it) is optional but needed for Hold Rate, which is defined here as
    holds per relief appearance (games - gamesStarted from the season
    stat line) -- without it, hold_rate comes back None.
    """
    totals = _blank()
    any_data = False

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
        pstats = gameday.get_player_pitching_stats(gd, player_id)
        if pstats is None:
            continue
        any_data = True
        _accumulate(totals, pstats)

    if not any_data:
        return None

    pitch_count = totals["pitch_count"]
    bf = totals["bf"]
    ab_against = max(bf - totals["bb"] - totals["hbp"] - totals["sac_bunts"] - totals["sac_flies"], 0)
    tb_against = totals["singles"] + 2 * totals["doubles"] + 3 * totals["triples"] + 4 * totals["hr"]

    avg_against = analytics.safe_div(totals["h"], ab_against)
    obp_against = analytics.safe_div(totals["h"] + totals["bb"] + totals["hbp"], bf)
    slg_against = analytics.safe_div(tb_against, ab_against)
    ops_against = None
    iso_against = None
    if obp_against is not None and slg_against is not None:
        ops_against = obp_against + slg_against
        iso_against = slg_against - avg_against

    woba_num = (analytics.WOBA_WEIGHTS["bb"] * totals["bb"] + analytics.WOBA_WEIGHTS["hbp"] * totals["hbp"] +
                analytics.WOBA_WEIGHTS["single"] * totals["singles"] + analytics.WOBA_WEIGHTS["double"] * totals["doubles"] +
                analytics.WOBA_WEIGHTS["triple"] * totals["triples"] + analytics.WOBA_WEIGHTS["hr"] * totals["hr"])
    woba_against = analytics.safe_div(woba_num, bf)

    # LOB%: (H + BB + HBP - R) / (H + BB + HBP - 1.4*HR) -- standard
    # sabermetric formula, using this season's summed "runs" (all runs
    # allowed while pitching, same runs-allowed-vs-earned caveat as the
    # rest of this app) as R.
    lob_denom = totals["h"] + totals["bb"] + totals["hbp"] - 1.4 * totals["hr"]
    lob_pct = analytics.safe_div(totals["h"] + totals["bb"] + totals["hbp"] - totals["runs"], lob_denom) if lob_denom > 0 else None

    relief_games = None
    if p_row:
        relief_games = (p_row.get("games") or 0) - (p_row.get("gamesStarted") or 0)
    hold_rate = analytics.safe_div(totals["holds"], relief_games) if relief_games else None

    save_pct = analytics.safe_div(
        totals["save_opportunities"] - totals["blown_saves"], totals["save_opportunities"]
    )
    inherited_scoring_pct = analytics.safe_div(totals["inherited_runners_scored"], totals["inherited_runners"])

    return {
        "opp_avg": avg_against, "opp_obp": obp_against, "opp_slg": slg_against,
        "opp_ops": ops_against, "opp_iso": iso_against, "opp_woba": woba_against,
        "lob_pct": lob_pct,
        "strike_pct": analytics.safe_div(totals["strikes"], pitch_count),
        "ball_pct": analytics.safe_div(totals["balls"], pitch_count),
        "called_strike_pct": analytics.safe_div(totals["strikes_looking"], pitch_count),
        "swinging_strike_pct": analytics.safe_div(totals["strikes_swinging"], pitch_count),
        "k_looking_pct": analytics.safe_div(totals["strikeouts_looking"], bf),
        "k_swinging_pct": analytics.safe_div(totals["strikeouts_swinging"], bf),
        "holds": totals["holds"], "hold_rate": hold_rate,
        "save_opportunities": totals["save_opportunities"], "blown_saves": totals["blown_saves"],
        "save_pct": save_pct,
        "inherited_runners": totals["inherited_runners"],
        "inherited_runners_scored": totals["inherited_runners_scored"],
        "inherited_scoring_pct": inherited_scoring_pct,
        "pitch_count": pitch_count, "bf": bf,
    }
