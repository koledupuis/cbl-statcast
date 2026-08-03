"""
Modeled win-probability estimates for CBL's own scheduled games --
NOT real betting odds. No market data, no vig, no line movement, no
injury reports or weather, and this app isn't accepting any wagers.
A curiosity feature only, sitting behind an unlisted page rather than
anything linked from the main site.

Given how short this league's season is (48 games), these are
noisier, rougher estimates than anything from a full pro season --
early in the year especially, a team's Pythagorean win% is built on a
handful of games and can swing hard from one series to the next.

MODEL: the Log5 formula (Bill James), the standard sabermetric method
for estimating head-to-head win probability from each team's own
overall strength:

  P(A beats B) = (PA - PA*PB) / (PA + PB - 2*PA*PB)

PA/PB are each team's PYTHAGOREAN win% (not raw W-L record) --
already computed per team by team_schedule.build_team_season_record,
and the standard choice for this kind of model since it's less noisy
over a short season than raw record, being driven by full-season run
differential rather than how many one-run games happened to break a
team's way.

Adjusted by a home-field factor derived from THIS LEAGUE's own actual
home/away split (see league_home_field_win_pct) rather than an
assumed MLB-style ~54%, which has no particular reason to hold for a
short amateur summer season with different travel, ballparks, and
scheduling than affiliated pro ball.
"""
import math
import statistics

import cbl_api
import gamelog
import team_schedule

MIN_GAMES_FOR_HOME_FIELD_CALC = 20  # league-wide completed games needed before trusting a derived home-field split
FALLBACK_HOME_WIN_PCT = 0.54  # commonly-cited MLB long-run average; only used until the league has enough games of its own


def league_home_field_win_pct(team_names, season_year=None):
    """League-wide home-team win % across every completed game on
    record, derived from the same game_results every team's own
    season-record walk already produces (each entry already carries
    is_home). Each game is shared by two teams' own game_results lists
    (the home team's and the away team's), so entries are deduped by
    public_game_id -- taking either team's entry for a given game is
    fine, since "home team won" is determined by cross-referencing
    is_home against result, not by which side's list the entry came
    from (an away team's own "L" means the home team won that game
    just as validly as a home team's own "W" does).

    Falls back to FALLBACK_HOME_WIN_PCT if there aren't yet enough
    league-wide completed games to trust a derived number, or if the
    walk fails for any reason -- the return value clearly flags which
    case a caller got via "is_derived"."""
    home_wins = 0
    total = 0
    seen_game_ids = set()
    for team_name in team_names:
        try:
            record = team_schedule.build_team_season_record(team_name, season_year)
        except Exception:
            continue
        for g in (record or {}).get("game_results", []):
            gid = g.get("public_game_id")
            if gid:
                if gid in seen_game_ids:
                    continue
                seen_game_ids.add(gid)
            total += 1
            home_won = (g.get("is_home") and g.get("result") == "W") or \
                       (not g.get("is_home") and g.get("result") == "L")
            if home_won:
                home_wins += 1

    if total < MIN_GAMES_FOR_HOME_FIELD_CALC:
        return {"win_pct": FALLBACK_HOME_WIN_PCT, "games": total, "is_derived": False}
    return {"win_pct": home_wins / total, "games": total, "is_derived": True}


MIN_GAMES_FOR_RUN_STATS = 20  # league-wide completed games needed before trusting empirical run-differential/total spread
FALLBACK_DIFF_STDEV = 4.5  # rough MLB-scale game-to-game run-differential std dev, used only as a fallback
FALLBACK_TOTAL_MEAN = 9.0
FALLBACK_TOTAL_STDEV = 4.5


def league_run_stats(team_names, season_year=None):
    """Empirical mean and standard deviation of per-game run
    differential (home minus away) and per-game combined TOTAL runs,
    computed directly from every completed game on record -- the
    spread used for the run-line and total-runs normal-approximation
    estimates below, derived from this league's own actual results
    rather than an assumed number.

    Same sharing/dedup situation as league_home_field_win_pct: each
    completed game appears in two teams' own game_results lists.
    Total runs and |differential| are identical from either team's
    entry, but the SIGN of differential must be normalized to "home
    minus away" regardless of which team's entry got captured (an
    away team's own team_runs/opp_runs are already correct from ITS
    perspective, so the sign just needs flipping when the captured
    entry belongs to the away side).

    Falls back to rough MLB-scale constants if there aren't yet
    enough league-wide completed games to trust an empirical number --
    flagged via "is_derived" same as league_home_field_win_pct."""
    diffs = []
    totals = []
    seen_game_ids = set()
    for team_name in team_names:
        try:
            record = team_schedule.build_team_season_record(team_name, season_year)
        except Exception:
            continue
        for g in (record or {}).get("game_results", []):
            gid = g.get("public_game_id")
            if gid:
                if gid in seen_game_ids:
                    continue
                seen_game_ids.add(gid)
            team_runs, opp_runs = g.get("team_runs"), g.get("opp_runs")
            if team_runs is None or opp_runs is None:
                continue
            totals.append(team_runs + opp_runs)
            diffs.append((team_runs - opp_runs) if g.get("is_home") else (opp_runs - team_runs))

    if len(diffs) < MIN_GAMES_FOR_RUN_STATS:
        return {
            "diff_mean": 0.0, "diff_stdev": FALLBACK_DIFF_STDEV,
            "total_mean": FALLBACK_TOTAL_MEAN, "total_stdev": FALLBACK_TOTAL_STDEV,
            "games": len(diffs), "is_derived": False,
        }
    return {
        "diff_mean": statistics.mean(diffs), "diff_stdev": statistics.pstdev(diffs) or FALLBACK_DIFF_STDEV,
        "total_mean": statistics.mean(totals), "total_stdev": statistics.pstdev(totals) or FALLBACK_TOTAL_STDEV,
        "games": len(diffs), "is_derived": True,
    }


def _normal_cdf(x, mean, stdev):
    """Standard normal CDF via math.erf -- no scipy dependency needed
    for just this. Degenerates safely to a step function if stdev is
    somehow 0 (shouldn't happen given the fallbacks above, but never
    divide by zero on a real request regardless)."""
    if stdev <= 0:
        return 1.0 if x >= mean else 0.0
    return 0.5 * (1 + math.erf((x - mean) / (stdev * math.sqrt(2))))


def round_to_half(x):
    """Rounds to the nearest 0.5 -- standard sportsbook convention for
    a spread/total line, and deliberately avoids landing on a whole
    number (which would allow an exact push)."""
    return round(x * 2) / 2


def run_line_probability(expected_margin, diff_stdev, line):
    """Probability the HOME team covers `line` (given as a signed
    home-team spread, e.g. -1.5 means home is favored and must win by
    2+ runs to cover; +1.5 means home is the underdog and covers by
    losing by 1 or winning outright) -- a normal approximation around
    the modeled expected home-minus-away margin, using the league's
    own empirically derived (or fallback) game-to-game standard
    deviation. Real per-game run differential is a discrete, somewhat
    skewed quantity (a Skellam-style difference of two low counts),
    not a perfect bell curve -- a normal approximation is a reasonable
    simplification at typical baseball scoring levels, not an exact
    model."""
    threshold = -line
    return 1 - _normal_cdf(threshold, expected_margin, diff_stdev)


def total_runs_probability(expected_total, total_stdev, line):
    """Probability of going OVER `line`, same normal-approximation
    approach and same caveat as run_line_probability above."""
    return 1 - _normal_cdf(line, expected_total, total_stdev)


def log5_win_probability(team_a_pyth_pct, team_b_pyth_pct):
    """Bill James' Log5 formula -- head-to-head win probability for
    team A over team B, from each team's own Pythagorean win%.
    Clamped to a narrow [0.01, 0.99] range before the formula runs, so
    an early-season team that's literally 0-5 or 5-0 (a real, if
    extreme, possible input) never produces a divide-by-zero or a
    nonsensical 0%/100% probability against a real opponent."""
    pa = min(max(team_a_pyth_pct, 0.01), 0.99)
    pb = min(max(team_b_pyth_pct, 0.01), 0.99)
    denom = pa + pb - 2 * pa * pb
    if denom <= 0:
        return 0.5
    return (pa - pa * pb) / denom


def apply_home_field(prob_home_before_adjustment, home_field_win_pct):
    """Nudges a pre-home-field win probability toward the league's own
    derived (or fallback) home-field rate -- a simple blend (average
    of the Log5 estimate and the league's overall home-team win rate)
    rather than a more elaborate model, since with a 48-game season
    there isn't enough data to responsibly fit anything more precise
    than "the true number is probably somewhere between team strength
    alone and the league's overall home-field tendency."""
    return (prob_home_before_adjustment + home_field_win_pct) / 2


def prob_to_american_odds(p):
    """Win probability -> American odds format (e.g. -150 or +130) --
    standard sportsbook display convention (negative = favorite,
    positive = underdog) used here purely for familiarity. This is
    NOT a real market price: no vig/juice is baked in (a real book's
    two-sided line always implies combined probability over 100%;
    this is the raw modeled probability only), and it isn't rounded to
    any actual sportsbook's price ladder."""
    p = min(max(p, 0.01), 0.99)
    if p >= 0.5:
        return round(-100 * p / (1 - p))
    return round(100 * (1 - p) / p)


def _games_on_date(target_date, season_year=None):
    """Every game on record for target_date (YYYY-MM-DD string) that
    is NOT marked "completed" -- deliberately an exclusion filter
    rather than checking for a specific "scheduled"/"upcoming" status
    string, since this codebase has only ever confirmed what
    "completed" looks like in this feed, not the exact spelling CBL
    uses for a not-yet-played game. Excluding "completed" is the safe
    direction to get this right without guessing at an unconfirmed
    status value."""
    all_games = cbl_api.get_game_ids(season_year)
    if not isinstance(all_games, list):
        return []
    matches = []
    for g in all_games:
        date = gamelog._field(g, "gameDate", "game_date", "game-date", default="")
        status = gamelog._field(g, "status", default="")
        if date == target_date and status != "completed":
            matches.append(g)
    return matches


def build_daily_odds(target_date, season_year=None):
    """Modeled win probability, run-line, and total-runs estimates for
    every not-yet-played game on record for target_date (YYYY-MM-DD
    string).

    Returns {"date", "home_field": {...}, "run_stats": {...}, "games":
    [{"home_team","away_team","home_win_pct","away_win_pct","home_odds",
    "away_odds","home_pythagorean","away_pythagorean","run_line",
    "home_cover_prob","away_cover_prob","home_cover_odds",
    "away_cover_odds","total_line","over_prob","under_prob",
    "over_odds","under_odds"}, ...]}. A game is skipped entirely (not
    shown with a placeholder) if either team doesn't have a
    Pythagorean win% AND runs-per-game on record yet (e.g. a brand
    new team with zero completed games) -- there's no responsible
    estimate to show in that case."""
    games = _games_on_date(target_date, season_year)
    if not games:
        return {"date": target_date, "home_field": None, "run_stats": None, "games": []}

    all_team_names = sorted({
        gamelog._field(g, "homeTeam", "home_team", "home-team") for g in games
    } | {
        gamelog._field(g, "awayTeam", "away_team", "away-team") for g in games
    })
    # Home-field and run-stats are both derived from the WHOLE league's
    # results, not just today's participants, since a handful of teams'
    # games wouldn't be enough of a sample on their own.
    try:
        standings_teams = [r["team"] for r in team_schedule.build_standings(all_team_names, season_year)]
    except Exception:
        standings_teams = all_team_names
    league_teams = standings_teams or all_team_names
    home_field = league_home_field_win_pct(league_teams, season_year)
    run_stats = league_run_stats(league_teams, season_year)

    team_cache = {}

    def team_info(team_name):
        """Pythagorean win% plus runs-per-game (scored/allowed), all
        from the one already-fetched season record -- cached per team
        name so a team appearing in multiple of today's games (not
        possible in a normal single-game-per-day schedule, but a safe
        guard regardless) doesn't refetch."""
        if team_name not in team_cache:
            try:
                record = team_schedule.build_team_season_record(team_name, season_year)
            except Exception:
                record = None
            record = record or {}
            games_played = record.get("games") or 0
            pyth = record.get("pythagorean_win_pct")
            rpg_scored = (record.get("runs_scored") / games_played) if games_played else None
            rpg_allowed = (record.get("runs_allowed") / games_played) if games_played else None
            team_cache[team_name] = {"pyth": pyth, "rpg_scored": rpg_scored, "rpg_allowed": rpg_allowed}
        return team_cache[team_name]

    results = []
    for g in games:
        home = gamelog._field(g, "homeTeam", "home_team", "home-team")
        away = gamelog._field(g, "awayTeam", "away_team", "away-team")
        if not home or not away:
            continue
        home_info, away_info = team_info(home), team_info(away)
        if home_info["pyth"] is None or away_info["pyth"] is None:
            continue

        raw_home_prob = log5_win_probability(home_info["pyth"], away_info["pyth"])
        home_prob = apply_home_field(raw_home_prob, home_field["win_pct"])
        away_prob = 1 - home_prob

        row = {
            "home_team": home, "away_team": away,
            "home_pythagorean": home_info["pyth"], "away_pythagorean": away_info["pyth"],
            "home_win_pct": home_prob, "away_win_pct": away_prob,
            "home_odds": prob_to_american_odds(home_prob),
            "away_odds": prob_to_american_odds(away_prob),
        }

        # Run line + total: needs both teams' runs-per-game on record,
        # separate from (but usually available alongside) Pythagorean
        # win%, so this is checked and added independently rather than
        # skipping the whole game if only these are missing.
        if None not in (home_info["rpg_scored"], home_info["rpg_allowed"],
                         away_info["rpg_scored"], away_info["rpg_allowed"]):
            expected_home_runs = (home_info["rpg_scored"] + away_info["rpg_allowed"]) / 2
            expected_away_runs = (away_info["rpg_scored"] + home_info["rpg_allowed"]) / 2
            expected_margin = expected_home_runs - expected_away_runs
            expected_total = expected_home_runs + expected_away_runs

            run_line = round_to_half(-expected_margin)  # e.g. home favored by 3 -> line of -2.5 or -3.5
            home_cover_prob = run_line_probability(expected_margin, run_stats["diff_stdev"], run_line)
            away_cover_prob = 1 - home_cover_prob

            total_line = round_to_half(expected_total)
            over_prob = total_runs_probability(expected_total, run_stats["total_stdev"], total_line)
            under_prob = 1 - over_prob

            row.update({
                "run_line": run_line,
                "home_cover_prob": home_cover_prob, "away_cover_prob": away_cover_prob,
                "home_cover_odds": prob_to_american_odds(home_cover_prob),
                "away_cover_odds": prob_to_american_odds(away_cover_prob),
                "total_line": total_line,
                "over_prob": over_prob, "under_prob": under_prob,
                "over_odds": prob_to_american_odds(over_prob),
                "under_odds": prob_to_american_odds(under_prob),
            })

        results.append(row)

    return {"date": target_date, "home_field": home_field, "run_stats": run_stats, "games": results}
