import json
import re
import time
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from .codes.date import days_since
from .codes.exp import extract_experience_level
from .codes.remoty import is_remote
from .codes.geoloc import extract_locations as _geoloc_extract_locations


# ============================================================
# CONFIG
# ============================================================

NAME = "LEVER"
TENANTS_FILE = "data/tenants.json"

BASE_URL = "https://api.lever.co/v0/postings/{token}?mode=json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; job-research-script/1.0)"
}

MAX_RETRIES = 2
RETRY_BACKOFF_SECONDS = 2.0

# exp.py's detector falls through to this bucket whenever nothing in
# the text matched any of its keyword patterns at all (e.g. commitment
# == "Full-time"). Used below to tell "genuinely mid" apart from "no
# signal, keep looking".
_NO_SIGNAL_LEVEL = "mid"

# Lever sometimes uses a hybrid label like "Mid-Senior" / "Mid/Senior".
# exp.py's plain keyword matcher would tag that "senior" (the word is
# right there) - this catches it first and forces "mid" instead.
_MID_SENIOR_RE = re.compile(r"\bmid[\s\-/]*(?:to[\s\-/]*)?senior\b", re.IGNORECASE)


# ============================================================
# LOAD TENANTS
# ============================================================

def load_tenants(tenants_file=None):
    """Parse the Lever tenants file into a list of tenant_info dicts.

    Each tenant_info has:
        key:   unique id used internally (the job board token/slug)
        label: display name / output filename stem
        token: the Lever job board slug (e.g. "netflix" from
               https://jobs.lever.co/netflix)
    """
    path = tenants_file or TENANTS_FILE

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    tenants = []
    for entry in data:
        if isinstance(entry, str):
            token = entry
        elif isinstance(entry, dict):
            token = entry.get("token") or entry.get("slug") or entry.get("name")
        else:
            token = None

        if not token:
            print(f"  [!] Skipping unrecognized tenant entry: {entry}")
            continue

        token = token.strip()
        tenants.append({
            "key": token,
            "label": token,
            "token": token,
        })

    return tenants


# ============================================================
# HTTP
# ============================================================

def _fetch_json(url):
    req = Request(url, headers=HEADERS)
    with urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ============================================================
# PARSE - experience level
# ============================================================
#
# Priority: categories.commitment first (sometimes an explicit signal,
# e.g. commitment == "Intern"), then fall back to the title-based
# keyword detector - same "explicit signal first, keyword fallback
# second" pattern ashscrap.py uses for remote status.
#
# Output keeps exp.py's native four buckets as-is: intern, junior,
# mid, senior (director/VP/chief/etc. already fold into "senior"
# inside exp.py itself, no change needed there). Nothing gets
# collapsed - a "mid" result stays "mid".

def _classify_text(text):
    """
    Classify one piece of text (commitment or title) into an
    experience level.

    Returns (level, decisive):
        decisive=True  -> the text actually said something about
                           seniority (a real keyword hit, or the
                           "Mid-Senior" override below) - safe to use
                           as the final answer.
        decisive=False -> the text was empty, or had no keyword signal
                           at all and exp.py just fell through to its
                           default "mid" bucket - caller should keep
                           looking (e.g. try the title next).
    """
    if not text or not text.strip():
        return _NO_SIGNAL_LEVEL, False

    if _MID_SENIOR_RE.search(text):
        return "mid", True

    level = extract_experience_level(text)
    return level, level != _NO_SIGNAL_LEVEL


def _classify_experience(categories, title):
    commitment = (categories or {}).get("commitment") or ""

    commitment_level, commitment_decisive = _classify_text(commitment)
    if commitment_decisive:
        return commitment_level

    title_level, _ = _classify_text(title)
    return title_level


# ============================================================
# PARSE - remote status
# ============================================================
#
# Lever models workplace type explicitly via categories.workplaceType
# ("remote" / "hybrid" / "onsite", when the board sets it at all).
# When present, that's treated the same way ashscrap.py treats
# Ashby's isRemote bool: an explicit ATS-provided flag that overrides
# keyword sniffing. When absent, fall back to keyword search across
# the location text + title, same as ashscrap.py's fallback branch.

def _classify_remote(categories, title):
    workplace_type = (categories or {}).get("workplaceType")
    location = categories.get("location") or ""
    all_locations = " ".join(categories.get("allLocations") or [])

    if isinstance(workplace_type, str) and workplace_type.strip():
        remote_flag = workplace_type.strip().lower() == "remote"
        return is_remote(location, title, ats_remote_flag=remote_flag)

    remote_signal = " ".join(filter(None, [location, all_locations]))
    return is_remote(remote_signal, title)


# ============================================================
# PARSE - locations
# ============================================================
#
# Lever gives free-text location strings (e.g. "San Francisco, CA",
# "London, UK", "Remote - USA", "Germany") rather than Ashby's
# structured postal address, so this is a best-effort split rather
# than a guaranteed-accurate geocode. Same shape as every other
# scraper's "locations" output though: a list of {"country", "city"}
# dicts, "city" omitted entirely when there's nothing usable, never a
# null placeholder. The actual parsing (comma-split first, popular
# city/country matching as a last resort) lives in geoloc.py so every
# scraper shares one implementation.

def _extract_locations(categories):
    raw_locations = (categories or {}).get("allLocations") or []

    if not raw_locations:
        single = categories.get("location")
        raw_locations = [single] if single else []

    return _geoloc_extract_locations(raw_locations)


# ============================================================
# PARSE - full job
# ============================================================

def _parse_job(posting, token):
    title = posting.get("text")
    categories = posting.get("categories") or {}

    parsed = {
        "company": token,
        "title": title,
        "url": posting.get("hostedUrl"),
        "date": days_since(posting.get("createdAt")),
        "experience_level": _classify_experience(categories, title),
        "remote": _classify_remote(categories, title),
    }

    locations = _extract_locations(categories)
    if locations:
        parsed["locations"] = locations

    return parsed


# ============================================================
# FETCH JOBS
# ============================================================
# Like Ashby, Lever's public postings API isn't paginated - one call
# (mode=json) returns every open job for a board as a bare JSON array.
# So this always finishes in a single round trip. `offset` is accepted
# only so the signature matches other scrapers; it's unused here.

def fetch_jobs(tenant_info, offset=0):
    """
    Fetch jobs for one Lever tenant.

    Returns a dict:
        jobs:        list of cleaned job dicts fetched in this call
        total:       total job count if known, else None
        error:       human-readable error message, or None on success
        error_type:  "not_found" (board genuinely doesn't exist),
                     "rate_limited", "network_error", "http_error",
                     or None on success. Only "not_found" should ever
                     be treated as "this tenant is broken, remove it" -
                     the others are transient and may succeed on retry.
        done:        True if there is nothing left to fetch after this call
        next_offset: offset to pass on the next call (None when done)
    """
    token = tenant_info["token"]
    url = BASE_URL.format(token=token)

    last_error = None
    last_error_type = None

    for attempt in range(MAX_RETRIES + 1):
        try:
            data = _fetch_json(url)

        except HTTPError as e:
            if e.code == 404:
                # Board genuinely doesn't exist - no point retrying.
                return {
                    "jobs": [], "total": None,
                    "error": "no Lever job board found (404)",
                    "error_type": "not_found",
                    "done": True, "next_offset": None,
                }

            if e.code == 429:
                last_error = "rate limited (429)"
                last_error_type = "rate_limited"
                retry_after = e.headers.get("Retry-After") if e.headers else None
                wait = float(retry_after) if retry_after and retry_after.isdigit() else RETRY_BACKOFF_SECONDS * (attempt + 1)
            else:
                last_error = f"HTTP error {e.code}"
                last_error_type = "http_error"
                wait = RETRY_BACKOFF_SECONDS * (attempt + 1)

        except URLError as e:
            last_error = f"network error: {e}"
            last_error_type = "network_error"
            wait = RETRY_BACKOFF_SECONDS * (attempt + 1)

        else:
            # A healthy board returns a bare JSON array. Anything else
            # (e.g. an error object) means the slug is bad/unrecognized.
            if not isinstance(data, list):
                return {
                    "jobs": [], "total": None,
                    "error": f"unexpected Lever response shape: {type(data).__name__}",
                    "error_type": "not_found",
                    "done": True, "next_offset": None,
                }

            jobs = [_parse_job(p, token) for p in data]

            return {
                "jobs": jobs,
                "total": len(jobs),
                "error": None,
                "error_type": None,
                "done": True,
                "next_offset": None,
            }

        if attempt < MAX_RETRIES:
            time.sleep(wait)

    return {
        "jobs": [], "total": None,
        "error": last_error, "error_type": last_error_type,
        "done": True, "next_offset": None,
    }