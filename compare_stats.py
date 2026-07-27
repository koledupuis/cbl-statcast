"""
Row-building helpers for the player/team comparison page. Every stat
shown here is already computed elsewhere in this app (analytics.py's
batting/pitching advanced stats, team_schedule.py's season record) --
this module's only job is arranging those numbers into rows with one
column per compared entity, formatting each value with this app's
existing formatters (stats.py), and flagging which cell in a row (if
any) is the best value so the template can bold it without doing any
math itself.

"Best" is only computed when 2+ compared entities actually have a
non-None value for that row -- a single value has nothing to be "best"
relative to, so it's shown plain rather than misleadingly bolded.
"""
import stats


def _rows(entities, specs):
    rows = []
    for label, getter, formatter, better in specs:
        raw_values = [getter(e) for e in entities]
        numeric = [v for v in raw_values if v is not None]
        best_val = None
        if better and len(numeric) > 1:
            best_val = max(numeric) if better == "high" else min(numeric)
        cells = []
        for v in raw_values:
            is_best = best_val is not None and v == best_val
            cells.append({"display": formatter(v) if v is not None else "---", "is_best": is_best})
        rows.append({"label": label, "cells": cells})
    return rows


def _int_fmt(v):
    return str(int(v))


def _signed_int_fmt(v):
    return f"{int(v):+d}"


def _signed1_fmt(v):
    return f"{v:+.1f}"


def build_batting_compare_rows(players):
    """players: list of dicts, each optionally carrying 'b_row' (the raw
    season batting row) and 'batting' (analytics.batting_advanced +
    batting_plus_stats output)."""
    def bv(key):
        return lambda e: (e.get("batting") or {}).get(key)

    def rv(key):
        return lambda e: (e.get("b_row") or {}).get(key)

    specs = [
        ("Games", rv("games"), _int_fmt, None),
        ("PA", rv("plateAppearances"), _int_fmt, None),
        ("AB", rv("atBats"), _int_fmt, None),
        ("AVG", bv("avg"), stats.fmt3, "high"),
        ("OBP", bv("obp"), stats.fmt3, "high"),
        ("SLG", bv("slg"), stats.fmt3, "high"),
        ("OPS", bv("ops"), stats.fmt3, "high"),
        ("ISO", bv("iso"), stats.fmt3, "high"),
        ("wOBA", bv("woba"), stats.fmt3, "high"),
        ("OPS+", bv("ops_plus"), _int_fmt, "high"),
        ("HR", rv("homeRuns"), _int_fmt, "high"),
        ("RBI", rv("rbi"), _int_fmt, "high"),
        ("Runs", rv("runs"), _int_fmt, "high"),
        ("BB %", bv("bb_pct"), stats.fmt_pct, "high"),
        ("K %", bv("k_pct"), stats.fmt_pct, "low"),
        ("K &minus; BB %", bv("k_minus_bb_pct"), stats.fmt_pct, "low"),
        ("BABIP", bv("babip"), stats.fmt3, None),
        ("Runs Created", bv("rc"), stats.fmt1, "high"),
        ("Est. wRC", bv("est_wrc"), stats.fmt1, "high"),
    ]
    return _rows(players, specs)


def build_pitching_compare_rows(players):
    """players: list of dicts, each optionally carrying 'p_row' (raw
    season pitching row) and 'pitching' (analytics.pitching_advanced +
    pitching_plus_stats output)."""
    def pv(key):
        return lambda e: (e.get("pitching") or {}).get(key)

    def rv(key):
        return lambda e: (e.get("p_row") or {}).get(key)

    specs = [
        ("Games", rv("games"), _int_fmt, None),
        ("IP", pv("ip"), stats.fmt1, "high"),
        ("ERA", pv("era"), stats.fmt2, "low"),
        ("WHIP", pv("whip"), stats.fmt2, "low"),
        ("FIP", pv("fip"), stats.fmt2, "low"),
        ("xFIP", pv("xfip"), stats.fmt2, "low"),
        ("ERA+", pv("era_plus"), _int_fmt, "high"),
        ("K/9", pv("k9"), stats.fmt2, "high"),
        ("BB/9", pv("bb9"), stats.fmt2, "low"),
        ("HR/9", pv("hr9"), stats.fmt2, "low"),
        ("K %", pv("k_pct"), stats.fmt_pct, "high"),
        ("BB %", pv("bb_pct"), stats.fmt_pct, "low"),
        ("K &minus; BB %", pv("k_minus_bb_pct"), stats.fmt_pct, "high"),
        ("BABIP Against", pv("babip_against"), stats.fmt3, "low"),
        ("Wins", rv("wins"), _int_fmt, "high"),
        ("Losses", rv("losses"), _int_fmt, "low"),
        ("Saves", rv("saves"), _int_fmt, "high"),
        ("Strikeouts", rv("strikeoutsPitching"), _int_fmt, "high"),
    ]
    return _rows(players, specs)


def build_team_batting_compare_rows(teams):
    """teams: list of dicts, each optionally carrying 'batting'
    (analytics.team_batting_stats output for that team)."""
    def tv(key):
        return lambda e: (e.get("batting") or {}).get(key)

    specs = [
        ("Games", tv("games"), _int_fmt, None),
        ("Runs", tv("runs"), _int_fmt, "high"),
        ("AVG", tv("avg"), stats.fmt3, "high"),
        ("OBP", tv("obp"), stats.fmt3, "high"),
        ("SLG", tv("slg"), stats.fmt3, "high"),
        ("OPS", tv("ops"), stats.fmt3, "high"),
        ("ISO", tv("iso"), stats.fmt3, "high"),
        ("wOBA", tv("woba"), stats.fmt3, "high"),
        ("BABIP", tv("babip"), stats.fmt3, None),
        ("Runs / Game", tv("runs_per_game"), stats.fmt2, "high"),
    ]
    return _rows(teams, specs)


def build_team_pitching_compare_rows(teams):
    """teams: list of dicts, each optionally carrying 'pitching'
    (analytics.team_pitching_stats output for that team)."""
    def tv(key):
        return lambda e: (e.get("pitching") or {}).get(key)

    specs = [
        ("IP", tv("ip"), stats.fmt1, "high"),
        ("ERA", tv("era"), stats.fmt2, "low"),
        ("WHIP", tv("whip"), stats.fmt2, "low"),
        ("FIP", tv("fip"), stats.fmt2, "low"),
        ("GB %", tv("gb_pct"), stats.fmt_pct, "high"),
        ("FB %", tv("fb_pct"), stats.fmt_pct, None),
        ("Runs Allowed / Game", tv("runs_allowed_per_game"), stats.fmt2, "low"),
    ]
    return _rows(teams, specs)


def build_team_record_compare_rows(teams):
    """teams: list of dicts, each optionally carrying 'record'
    (team_schedule.build_team_season_record output for that team)."""
    def tv(key):
        return lambda e: (e.get("record") or {}).get(key)

    specs = [
        ("Wins", tv("wins"), _int_fmt, "high"),
        ("Losses", tv("losses"), _int_fmt, "low"),
        ("Win %", tv("win_pct"), stats.fmt3, "high"),
        ("Run Differential", tv("run_differential"), _signed_int_fmt, "high"),
        ("Pythagorean Win %", tv("pythagorean_win_pct"), stats.fmt3, "high"),
        ("Wins Above Expected", tv("wins_above_expected"), _signed1_fmt, "high"),
        ("One-Run Win %", tv("one_run_win_pct"), stats.fmt3, "high"),
    ]
    return _rows(teams, specs)
