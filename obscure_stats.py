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
import cbl_api
import gamelog
import pitching_splits
import rolling
import stats

MIN_IP_FOR_STREAK = 10
MIN_PA_FOR_STREAK = 20
MIN_PA_FOR_RATE = 20
MIN_IP_FOR_RATE = 10
TOP_N = 5


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


def build_most_ip_without_hr():
    """Top 5: current active streak of innings pitched without
    allowing a home run, among pitchers with at least MIN_IP_FOR_STREAK
    innings pitched this season. One game-log walk per qualifying
    pitcher (see module docstring on cost)."""
    all_pitching = cbl_api.get_pitching()
    candidates = [r for r in all_pitching if _ip_from_row(r) >= MIN_IP_FOR_STREAK]

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
    return results[:TOP_N]


def build_longest_scoreless_streak():
    """Top 5: current active streak of consecutive scoreless
    appearances, among pitchers with at least MIN_IP_FOR_STREAK innings
    pitched this season. Reuses pitching_splits.build_pitcher_scoreless_streak
    -- same function the player page's own game log tab already calls
    per-player, just run across every qualifying pitcher here."""
    all_pitching = cbl_api.get_pitching()
    candidates = [r for r in all_pitching if _ip_from_row(r) >= MIN_IP_FOR_STREAK]

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
    return results[:TOP_N]


def build_longest_hit_streak():
    """Top 5: current active hit streak, among batters with at least
    MIN_PA_FOR_STREAK plate appearances this season. Reuses
    rolling.build_rolling_stats' own "hit_streak" field -- a plain
    consecutive-games-with-a-hit count, not the weighted momentum
    score built from it elsewhere (that score is a separate,
    subsequent combination step this never calls)."""
    all_batting = cbl_api.get_batting()
    candidates = [r for r in all_batting if (r.get("plateAppearances") or 0) >= MIN_PA_FOR_STREAK]

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
    return results[:TOP_N]


def build_highest_iso():
    """Top 5: Isolated Power (SLG - AVG) -- how much of a hitter's
    slugging comes from extra bases specifically, stripped of the
    part AVG already covers. Pure season-row arithmetic, no game-log
    walk. Same min-PA guard as every other rate-stat leaderboard on
    this site."""
    all_batting = cbl_api.get_batting()
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
    return results[:TOP_N]


def build_best_bb_k_ratio():
    """Top 5: walk-to-strikeout ratio (BB/K) for batters -- plate
    discipline distilled to one number. Pure season-row arithmetic.
    Requires at least 1 strikeout on record (undefined/infinite
    otherwise) as well as the usual min-PA guard."""
    all_batting = cbl_api.get_batting()
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
    return results[:TOP_N]


def build_best_k_bb_ratio():
    """Top 5: strikeout-to-walk ratio (K/BB) for pitchers -- swing-and-
    miss stuff with control both accounted for in one number. Pure
    season-row arithmetic. Requires at least 1 walk allowed on record
    (undefined/infinite otherwise) as well as the usual min-IP guard."""
    all_pitching = cbl_api.get_pitching()
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
    return results[:TOP_N]


def build_all_obscure_stats():
    """Every leaderboard on the page, each independently wrapped so
    one failing category (e.g. a broken game-log fetch) doesn't take
    the others down with it."""
    categories = [
        ("Most IP Without Allowing a HR (Active Streak)",
         "Pitchers currently working the longest streak of innings without giving one up",
         build_most_ip_without_hr),
        ("Longest Active Scoreless Streak",
         "Consecutive appearances in a row without allowing a run, right now",
         build_longest_scoreless_streak),
        ("Longest Active Hit Streak",
         "Consecutive games in a row with a hit, right now",
         build_longest_hit_streak),
        ("Highest Isolated Power (ISO)",
         "SLG minus AVG -- how much of a hitter's slugging is extra bases, not just hits",
         build_highest_iso),
        ("Best BB/K Ratio (Batting)",
         "Walks per strikeout -- plate discipline in one number",
         build_best_bb_k_ratio),
        ("Best K/BB Ratio (Pitching)",
         "Strikeouts per walk -- swing-and-miss stuff with control, both at once",
         build_best_k_bb_ratio),
    ]
    result = []
    for title, subtitle, builder in categories:
        try:
            rows = builder()
        except Exception:
            rows = []
        result.append({"title": title, "subtitle": subtitle, "rows": rows})
    return result
