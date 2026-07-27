"""
Stolen base / caught stealing tracking.

Primary source: CBL's own per-game aggregate,
`liveGame.playerBattingStats[playerId].stolenBases` / `.caughtStealing`
-- confirmed present in a real gameday payload, and it's CBL's own tally
(computed server-side from more context than this app has), so it's
preferred over re-deriving the same thing here.

Fallback: if a game's payload doesn't have `playerBattingStats` for some
reason, walk the raw event stream directly for `runner_advance` events
with `cause` of `stolen_base` / `caught_stealing`, matched by `runnerId`
(gameday.get_stolen_base_events -- also confirmed against real data).
Both approaches were cross-checked against the same real game and
produced identical counts, so either is trustworthy on its own; this
just prefers the simpler, officially-aggregated one when available.
"""
import cbl_api
import gameday
import gamelog


def build_player_stolen_bases(player_id, team_name, season_year=None):
    """
    {"available": bool, "sb": int, "cs": int, "sb_pct": float|None}

    `available=False` means this app couldn't find stolen-base data
    (neither playerBattingStats nor a raw event stream) in ANY game
    checked this season -- not "this player stole 0 bases".
    """
    sb = cs = 0
    any_data_found = False

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

        stats = gameday.get_player_batting_stats(gd, player_id)
        if stats is not None:
            any_data_found = True
            sb += stats.get("stolenBases") or 0
            cs += stats.get("caughtStealing") or 0
            continue

        events = gameday.get_stolen_base_events(gd)
        if events is None:
            continue
        any_data_found = True
        for ev in events:
            if ev.get("runnerId") != player_id:
                continue
            if ev.get("cause") in gameday.STOLEN_BASE_CAUSES:
                sb += 1
            elif ev.get("cause") in gameday.CAUGHT_STEALING_CAUSES:
                cs += 1

    if not any_data_found:
        return {"available": False, "sb": 0, "cs": 0, "sb_pct": None}

    attempts = sb + cs
    return {"available": True, "sb": sb, "cs": cs, "sb_pct": (sb / attempts) if attempts else None}
