import json
import time
from datetime import datetime, timezone
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from .codes.exp import extract_experience_level
from .codes.remoty import is_remote
from .codes.geoloc import extract_locations as _geoloc_extract_locations


# ============================================================
# CONFIG
# ============================================================

NAME = "GREENHOUSE"
TENANTS_FILE = "data/greenhouse.json"

BASE_URL = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; job-research-script/1.0)"
}

# Metadata field names (lowercased) that indicate remote/workplace type.
REMOTE_METADATA_KEYS = ["workplace type", "remote", "work location type", "location type"]
MAX_RETRIES = 2
RETRY_BACKOFF_SECONDS = 2.0


# ============================================================
# LOAD TENANTS
# ============================================================

def load_tenants(tenants_file=None):
    """Parse the Greenhouse tenants file into a list of tenant_info dicts.

    Each tenant_info has:
        key:   unique id used internally (the board token)
        label: display name / output filename stem
        token: the Greenhouse board token
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
# PARSE
# ============================================================

def _extract_remote(job):
    """Look through job metadata for a workplace-type / remote field.
    Returns the raw text value (e.g. "Remote", "Hybrid", "Onsite") or None -
    used as a keyword signal for is_remote(), not as a boolean itself,
    since its wording varies by company."""
    metadata = job.get("metadata")
    if not metadata:
        return None

    for m in metadata:
        name = (m.get("name") or "").strip().lower()
        if any(key in name for key in REMOTE_METADATA_KEYS):
            return m.get("value")

    return None


def _days_since(iso_timestamp):
    """Convert Greenhouse's ISO 'updated_at' timestamp into the same
    days-ago int that Workday's parse_posted_days_ago() produces, so
    both scrapers' 'date' field means the same thing."""
    if not iso_timestamp:
        return None

    try:
        posted = datetime.fromisoformat(str(iso_timestamp).replace("Z", "+00:00"))
    except ValueError:
        return None

    if posted.tzinfo is None:
        posted = posted.replace(tzinfo=timezone.utc)

    delta = datetime.now(timezone.utc) - posted

    return max(delta.days, 0)


def _parse_job(job, token):
    """Turn a raw Greenhouse job into our clean job shape."""
    title = job.get("title")
    location_name = (job.get("location") or {}).get("name", "")
    remote_field = _extract_remote(job)

    # Some companies model "Remote" as a boolean checkbox field in
    # Greenhouse (True/False), others as free text (e.g. "Remote",
    # "Hybrid"). Handle both: a bool goes straight to is_remote()'s
    # explicit flag; text gets folded into the keyword-search signal.
    if isinstance(remote_field, bool):
        remote = is_remote(location_name, title, ats_remote_flag=remote_field)
    else:
        remote_text = remote_field if isinstance(remote_field, str) else None
        remote_signal = " ".join(filter(None, [location_name, remote_text]))
        remote = is_remote(remote_signal, title)

    # Same `locations` output shape as every other scraper: a list of
    # {"country", "city"} dicts, "city" omitted when unknown. Greenhouse
    # only gives us a single free-text display string, which can itself
    # list several locations (e.g. "New York or Remote") - geoloc's
    # extract_locations() splits that apart, structural-parses each
    # piece, and only falls back to popular city/country matching as a
    # last resort.
    locations = _geoloc_extract_locations([location_name])

    parsed = {
        "company": token,
        "title": title,
        "url": job.get("absolute_url"),
        "date": _days_since(job.get("updated_at")),
        "experience_level": extract_experience_level(title),
        "remote": remote,
    }

    if locations:
        parsed["locations"] = locations

    return parsed


# ============================================================
# FETCH JOBS
# ============================================================
# Greenhouse's public board API isn't paginated - one call returns every
# open job for a board. So this always finishes in a single round trip.
# `offset` is accepted only so the signature matches other scrapers;
# it's unused here.

def fetch_jobs(tenant_info, offset=0):
    """
    Fetch jobs for one Greenhouse tenant.

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
                    "error": "no Greenhouse board found (404)",
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
            raw_jobs = data.get("jobs", [])
            jobs = [_parse_job(j, token) for j in raw_jobs]

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