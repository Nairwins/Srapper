import re
from datetime import datetime, timezone


# ============================================================
# POSTED DATE PARSER (Workday - fuzzy text)
# ============================================================

DAYS_AGO_RE = re.compile(
    r"(\d+)(\+)?\s*Day",
    re.IGNORECASE
)


def parse_posted_days_ago(posted_on):
    """
    Convert Workday's fuzzy 'postedOn' text into a compact value.

    Examples:
        "Posted today"       -> 0
        "Today"              -> 0
        "Posted yesterday"   -> 1
        "Yesterday"          -> 1
        "2 Days Ago"         -> 2
        "30+ Days Ago"       -> "30+"

    Returns:
        int, str, or None
    """

    if not posted_on:
        return None

    text = str(posted_on).strip().lower()

    # --------------------------------------------------------
    # TODAY
    # --------------------------------------------------------

    if "today" in text:
        return 0

    # --------------------------------------------------------
    # YESTERDAY
    # --------------------------------------------------------

    if "yesterday" in text:
        return 1

    # --------------------------------------------------------
    # DAYS AGO
    # --------------------------------------------------------

    match = DAYS_AGO_RE.search(text)

    if match:
        number = int(match.group(1))
        has_plus = match.group(2) is not None

        if has_plus:
            return f"{number}+"

        return number

    return None


# ============================================================
# POSTED DATE PARSER (Ashby / Lever - real timestamps)
# ============================================================
#
# Unlike Workday, Ashby and Lever both give a real, unambiguous
# timestamp for when a job was posted - Ashby as an ISO-8601 string
# ("publishedAt"), Lever as epoch milliseconds ("createdAt"). This
# single function accepts either shape and turns it into the same
# "days ago" int that Workday's postedOn effectively encodes, so
# every scraper's "date" field means the same thing. Kept here
# (instead of duplicated per-scraper) so there's one place to fix
# if the convention ever needs to change.

def days_since(timestamp):
    """
    Convert an ISO-8601 timestamp string or epoch-millisecond value
    into the number of whole days elapsed since then (UTC).

    Examples:
        "2024-01-15T10:30:00.000Z"  -> e.g. 12
        1705315800000                -> e.g. 12
        "1705315800000"              -> e.g. 12

    Returns:
        int (>= 0), or None if timestamp is missing/unparseable.
    """

    if not timestamp:
        return None

    posted = None

    # --------------------------------------------------------
    # EPOCH MILLISECONDS (int/float, or a numeric string - Lever)
    # --------------------------------------------------------

    epoch_ms = None

    if isinstance(timestamp, (int, float)):
        epoch_ms = timestamp
    elif isinstance(timestamp, str) and timestamp.strip().isdigit():
        epoch_ms = int(timestamp.strip())

    if epoch_ms is not None:
        try:
            posted = datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc)
        except (ValueError, OSError, OverflowError):
            return None

    # --------------------------------------------------------
    # ISO-8601 STRING (Ashby)
    # --------------------------------------------------------

    else:
        try:
            posted = datetime.fromisoformat(str(timestamp).strip().replace("Z", "+00:00"))
        except ValueError:
            return None

        if posted.tzinfo is None:
            posted = posted.replace(tzinfo=timezone.utc)

    delta = datetime.now(timezone.utc) - posted

    return max(delta.days, 0)