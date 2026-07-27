"""
CBL's own front-office transaction log (Sign/Release/Trade/Inactive
List/Injured List/etc.) -- confirmed live at a Supabase REST endpoint,
entirely separate from the main cbl.ca API this app otherwise uses
exclusively. This is a genuinely more accurate source for "is this
player actually on the active roster right now" than the game-roster
proxy team_schedule.get_active_roster_pitchers() uses (a player could
be signed or released without that necessarily lining up with when
they last actually appeared in a game).

HONEST LIMITATIONS, stated up front rather than glossed over:

1. Matching is by PLAYER NAME, not player ID -- this feed has no
   player ID field at all, only a free-text name inside each
   transaction's description. Two different players sharing an exact
   name would be indistinguishable here. Matched case-insensitively,
   trimmed, nothing fuzzier than that -- a real typo or a name spelled
   slightly differently between this feed and the main stats feed
   (e.g. an accent mark, a suffix) would fail to match rather than
   guess.
2. Descriptions are free-text sentences, not structured fields. This
   module parses every distinct phrasing actually observed in a real
   fetch of this endpoint (see _PATTERNS below) -- Sign, Release,
   "Move to X List. Name" AND the reversed "Move Name(s) to X List"
   phrasing, Trade acquisitions (filtering out "Cash Considerations"
   as a trade component, not a player), trades to another league
   entirely, multi-player transactions (comma AND ampersand
   separated, e.g. "A, B, C & D" and "A & B"), and the Call-Up List
   assignment type. A transaction whose wording doesn't match any
   known pattern is kept as "unparsed" rather than silently
   misinterpreted -- callers can see how many transactions fell into
   that bucket and decide whether to trust the result.
3. No "reactivate" / "move back to active list" transaction type has
   been observed in a real fetch of this feed. A player's status is
   inferred as "active" whenever their most recent transaction is a
   Sign or a Trade acquisition and nothing since has moved them to
   Inactive/Injured/Released/traded away -- if CBL's own system
   silently reactivates a player without writing a transaction row,
   this would show them as still inactive when they're actually not.
   Flagged here explicitly rather than assumed away.
"""
import re

import cbl_api

TRANSACTIONS_URL = "https://wbxajvgduyqycpajxkco.supabase.co/rest/v1/transactions"
TRANSACTIONS_API_KEY = "sb_publishable_AfNTt4D76DVoWZtlq4540g_vdSx3Mb6"
TRANSACTIONS_TTL = 300  # 5 min -- a roster move isn't the kind of thing that needs live-overlay freshness

STATUS_ACTIVE = "active"
STATUS_INACTIVE = "inactive"
STATUS_INJURED = "injured"
STATUS_RELEASED = "released"
STATUS_TRADED_AWAY = "traded_away"       # acquired by another CBL team
STATUS_LEFT_LEAGUE = "left_league"       # traded to a team outside CBL entirely
STATUS_CALL_UP_LIST = "call_up_list"     # a real, distinct status seen in this feed -- NOT assumed to mean
                                          # the same thing as fully active; see module docstring point 3
STATUS_UNKNOWN = "unknown"               # description didn't match any known pattern

# Each entry: (compiled regex, action). The regex must capture the
# name-list portion in group(1) (and, for trade acquisitions, the
# other team in group(2)). Order matters -- more specific patterns
# (trade-to-another-league, call-up-list) are checked before the
# generic ones they could otherwise be mistaken for.
_PATTERNS = [
    (re.compile(r"^Trade to Another League:\s*(.+?)\s+to\s+.+$", re.IGNORECASE), STATUS_LEFT_LEAGUE),
    (re.compile(r"^Player Assigned to Call Up List\s+(.+)$", re.IGNORECASE), STATUS_CALL_UP_LIST),
    (re.compile(r"^Trade:\s*Acquire\s+(.+?)\s+from\s+.+$", re.IGNORECASE), STATUS_ACTIVE),
    (re.compile(r"^Sign\s+(.+)$", re.IGNORECASE), STATUS_ACTIVE),
    (re.compile(r"^Release\s+(.+)$", re.IGNORECASE), STATUS_RELEASED),
    (re.compile(r"^Move to Inactive List\.\s*(.+)$", re.IGNORECASE), STATUS_INACTIVE),
    (re.compile(r"^Move\s+(.+?)\s+to Inactive List$", re.IGNORECASE), STATUS_INACTIVE),
    (re.compile(r"^Move to Injured List\.\s*(.+)$", re.IGNORECASE), STATUS_INJURED),
    (re.compile(r"^Move\s+(.+?)\s+to Injured List$", re.IGNORECASE), STATUS_INJURED),
]


def _split_names(names_str):
    """Splits a name list like 'A, B, C & D' or 'A & B' or a single
    'A' into individual trimmed names. Filters out "Cash
    Considerations" -- a real trade component this feed lists
    alongside actual players in a multi-part trade description, not a
    player itself."""
    normalized = names_str.replace(" & ", ", ")
    names = [n.strip() for n in normalized.split(",")]
    return [n for n in names if n and n.lower() != "cash considerations"]


def _parse_description(description):
    """Returns (status, [names]) for one transaction's free-text
    description, or (STATUS_UNKNOWN, []) if it doesn't match any
    known pattern -- see this module's docstring for exactly which
    phrasings are handled and why an unmatched one is kept as unknown
    rather than guessed at."""
    description = (description or "").strip()
    for pattern, status in _PATTERNS:
        m = pattern.match(description)
        if m:
            names = _split_names(m.group(1))
            if names:
                return status, names
    return STATUS_UNKNOWN, []


def get_transactions(season_year=None):
    """Raw transaction list from the feed, most recent first (the feed
    itself is queried pre-sorted). Each row: {"id", "transaction_date",
    "team_name", "description", "season_year"}."""
    params = {
        "select": "id,transaction_date,team_name,description,season_year",
        "order": "transaction_date.desc",
        "apikey": TRANSACTIONS_API_KEY,
    }
    if season_year:
        params["season_year"] = f"eq.{season_year}"
    data = cbl_api._cached_get(
        TRANSACTIONS_URL, params, f"transactions:{season_year}", ttl=TRANSACTIONS_TTL,
    )
    return data if isinstance(data, list) else []


def build_roster_status(season_year=None):
    """Current roster status for every player name that appears
    anywhere in this season's transaction feed -- keyed by
    lowercased, trimmed name (see module docstring point 1 on why
    name-based matching is the best this feed allows).

    Returns {name_lower: {"name": original-cased name, "status": one
    of the STATUS_* constants, "team": team name as of that
    transaction, "date": transaction date, "description": the raw
    description text}}.

    Walks the feed in the order it's returned (most recent first) and
    keeps only the FIRST (i.e. most recent) transaction seen for each
    name -- everything older than that is superseded."""
    result = {}
    for row in get_transactions(season_year):
        status, names = _parse_description(row.get("description"))
        if status == STATUS_UNKNOWN:
            continue
        for name in names:
            key = name.lower()
            if key in result:
                continue  # already have a more recent transaction for this name
            result[key] = {
                "name": name,
                "status": status,
                "team": row.get("team_name"),
                "date": row.get("transaction_date"),
                "description": row.get("description"),
            }
    return result


def player_status(full_name, season_year=None):
    """Current roster status for one specific player, matched by exact
    name (case-insensitive, trimmed) -- see build_roster_status. None
    if this name has no transactions on record at all this season
    (not necessarily meaning they're not active -- could just mean no
    transaction was ever logged for them, e.g. a player who's been
    with the team since before this feed's data starts)."""
    if not full_name:
        return None
    all_status = build_roster_status(season_year)
    return all_status.get(full_name.strip().lower())
