"""
Rolling-window batting stats, built on top of the flat per-game rows
gamelog.py already produces for a player (build_player_game_log). No new
network surface -- this is pure aggregation over that game log.

Games where the player didn't appear are already excluded from the game
log (gamelog.py: "player didn't appear (DNP / not on this roster yet)"),
which is actually the *correct* behavior for hitting/on-base streaks --
a real streak isn't broken by a game the player simply didn't play, only
by a game they played and went hitless/reached-base-less. So the streak
functions here just walk the already-DNP-filtered game log backwards.

Known caveats (inherited from gamelog.py):
  - PA is treated as AB + BB (HBP/SF aren't tracked at the per-game
    level), so "on base" for streak purposes is H + BB, not the true
    OBP-event definition.
  - "Last 30 days" is a calendar-day window ending at the player's most
    recent completed game in the log, not literally "the last 30 days
    from today" -- the site's own season may be over, mid-break, etc.
"""
from datetime import datetime, timedelta

import gamelog


def _flatten_rows(game_log):
    """Chronological list of every game row across all months. team_games()
    (which gamelog.py walks) already sorts by date, and OrderedDict month
    buckets are created in the order first encountered, so this is already
    in date order without needing to re-sort."""
    rows = []
    for bucket in (game_log or {}).get("months", {}).values():
        rows.extend(bucket["rows"])
    return rows


def _sum_rows(rows):
    totals = gamelog._blank_totals()
    for r in rows:
        gamelog._accumulate(totals, r)
    totals.update(gamelog._game_rates(totals))
    totals["games"] = len(rows)
    return totals


def _parse_date(date_str):
    try:
        return datetime.strptime(date_str, "%Y-%m-%d")
    except (ValueError, TypeError):
        return None


def _last_n_games(rows, n):
    return _sum_rows(rows[-n:])


def _last_n_days(rows, n):
    dated = [(r, _parse_date(r.get("date"))) for r in rows]
    dated = [(r, d) for r, d in dated if d is not None]
    if not dated:
        return _sum_rows([])
    most_recent = max(d for _, d in dated)
    cutoff = most_recent - timedelta(days=n - 1)
    window = [r for r, d in dated if d >= cutoff]
    return _sum_rows(window)


def _last_n_pa(rows, n):
    """Walk backwards from the most recent game accumulating whole games
    until at least n PA are covered (or the log runs out) -- matches the
    conventional "last N PA" stat, which is built from whole games, not a
    partial game cut mid-log."""
    window = []
    pa_total = 0
    for r in reversed(rows):
        window.insert(0, r)
        pa_total += r.get("pa") or 0
        if pa_total >= n:
            break
    return _sum_rows(window)


def _hit_streak(rows):
    streak = 0
    for r in reversed(rows):
        if (r.get("h") or 0) > 0:
            streak += 1
        else:
            break
    return streak


def _on_base_streak(rows):
    streak = 0
    for r in reversed(rows):
        if (r.get("h") or 0) + (r.get("bb") or 0) > 0:
            streak += 1
        else:
            break
    return streak


def build_rolling_stats(game_log):
    """
    {
      "last_5": totals, "last_10": totals, "last_15": totals,
      "last_7_days": totals, "last_30_days": totals,
      "last_30_pa": totals, "last_50_pa": totals,
      "hit_streak": int, "on_base_streak": int, "games_available": int,
    }
    Every `totals` dict has the raw counting line plus pa/avg/obp/slg/ops,
    same shape as a gamelog.py game-log row's totals.
    """
    rows = _flatten_rows(game_log)
    return {
        "last_5": _last_n_games(rows, 5),
        "last_10": _last_n_games(rows, 10),
        "last_15": _last_n_games(rows, 15),
        "last_7_days": _last_n_days(rows, 7),
        "last_30_days": _last_n_days(rows, 30),
        "last_30_pa": _last_n_pa(rows, 30),
        "last_50_pa": _last_n_pa(rows, 50),
        "hit_streak": _hit_streak(rows),
        "on_base_streak": _on_base_streak(rows),
        "games_available": len(rows),
    }


# ---------------------------------------------------------- Momentum Meter --
#
# A "broadcast metric" in the spirit of the original wishlist's Custom
# League Analytics section: an openly-invented composite score, built
# entirely from real underlying numbers (nothing here is fabricated data,
# only the weighting formula is a house creation). The formula:
#
#   50% - recent OPS (last 7 days, falling back to last 10 games if the
#         player had no games in the last 7 days) vs. season OPS, scaled
#         so "exactly at season pace" = 50 points
#   30% - current hit streak, capped at a 10-game streak = full marks
#   20% - RBI production over the last 10 games, capped at 8 RBI in 10
#         games = full marks (roughly "1 RBI every 1.25 games" pace)
#
# These weights and caps are a judgment call, not a derived constant --
# treat the score as an entertainment-style summary, not a rigorous
# sabermetric. Change MOMENTUM_WEIGHTS / the caps below to retune it.

MOMENTUM_WEIGHTS = {"recent_ops": 0.5, "hit_streak": 0.3, "recent_rbi": 0.2}
MOMENTUM_STREAK_CAP_GAMES = 10
MOMENTUM_RBI_CAP = 8

MOMENTUM_LABELS = [
    (80, "\U0001F525 Hot"),
    (60, "\u2197 Heating Up"),
    (40, "\u27a1 Neutral"),
    (20, "\u2198 Cooling"),
    (0, "\u2744 Cold"),
]


def _momentum_label(score):
    for threshold, label in MOMENTUM_LABELS:
        if score >= threshold:
            return label
    return MOMENTUM_LABELS[-1][1]


def build_momentum_meter(rolling_stats, season_ops):
    """Returns None if there isn't enough recent-game data to say anything
    (e.g. a player with zero logged games this season) rather than
    reporting a misleadingly precise score off of nothing."""
    if not rolling_stats or not season_ops:
        return None

    recent = rolling_stats.get("last_7_days") or {}
    fallback = rolling_stats.get("last_10") or {}
    used_window = "last_7_days"
    if not recent.get("games"):
        recent = fallback
        used_window = "last_10"
    if not recent.get("games"):
        return None

    recent_ops = recent.get("ops") or 0.0
    ops_ratio = safe_div(recent_ops, season_ops) or 0.0
    ops_component = max(0.0, min(100.0, 50.0 + (ops_ratio - 1.0) * 100.0))

    streak = rolling_stats.get("hit_streak") or 0
    streak_component = max(0.0, min(1.0, streak / MOMENTUM_STREAK_CAP_GAMES)) * 100.0

    recent_rbi = (rolling_stats.get("last_10") or {}).get("rbi") or 0
    rbi_component = max(0.0, min(1.0, recent_rbi / MOMENTUM_RBI_CAP)) * 100.0

    score = round(
        MOMENTUM_WEIGHTS["recent_ops"] * ops_component +
        MOMENTUM_WEIGHTS["hit_streak"] * streak_component +
        MOMENTUM_WEIGHTS["recent_rbi"] * rbi_component
    )
    score = max(0, min(100, score))

    return {
        "score": score, "label": _momentum_label(score),
        "recent_ops": recent_ops, "season_ops": season_ops,
        "used_window": used_window, "hit_streak": streak, "recent_rbi": recent_rbi,
    }


def safe_div(n, d):
    return (n / d) if d else None
