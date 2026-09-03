import json
import time
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from .codes.date import days_since
from .codes.exp import extract_experience_level
from .codes.remoty import is_remote
from .codes.geoloc import canonicalize_country, canonicalize_city


# ============================================================
# CONFIG
# ============================================================

NAME = "WORKABLE"
TENANTS_FILE = "data/workable.json"

BASE_URL = "https://apply.workable.com/api/v1/widget/accounts/{token}?details=true"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; job-research-script/1.0)",
    "Accept": "application/json",
}

MAX_RETRIES = 2
RETRY_BACKOFF_SECONDS = 2.0


# ============================================================
# LOAD TENANTS
# ============================================================

def load_tenants(tenants_file=None):
    """Parse the Workable tenants file into a list of tenant_info dicts.

    Each tenant_info has:
        key:   unique id used internally (the Workable account slug)
        label: display name / output filename stem
        token: the Workable account slug (e.g. "huggingface" from
               https://apply.workable.com/huggingface/)
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


def _first_present(d, keys):
    if not d:
        return None
    for key in keys:
        value = d.get(key)
        if value not in (None, ""):
            return value
    return None


_ID_KEYS = ["shortcode", "id", "key"]
_URL_KEYS = ["url", "shortlink", "application_url"]
_POSTED_KEYS = ["created_at", "published_on"]

_COUNTRY_KEYS = ["country", "country_name", "country_code"]
_CITY_KEYS = ["city"]


# ============================================================
# PARSE - locations
# ============================================================
#
# Workable's job objects carry a structured "location" dict (country,
# city, telecommuting flag) and, for multi-location postings, a
# parallel "locations" array with one entry per posted location. Both
# are definitive, ATS-provided geography - same as Ashby's
# postalAddress or BambooHR's location object - so we never re-guess
# these through geoloc's popular-list matcher. We DO still run
# country/city through canonicalize_country()/canonicalize_city() (an
# exact-match lookup, not a search) so the spelling matches every
# other scraper's output for the same place.

def _location_entry_from_dict(loc):
    if not loc or not isinstance(loc, dict):
        return None

    country = _first_present(loc, _COUNTRY_KEYS)
    city = _first_present(loc, _CITY_KEYS)

    if not country and not city:
        return None

    entry = {}
    if country:
        entry["country"] = canonicalize_country(country)
    if city:
        entry["city"] = canonicalize_city(city)

    return entry or None


def _extract_locations(job):
    # Multi-location postings: prefer the "locations" array so we
    # don't collapse a job posted in several countries down to just
    # the primary one.
    multi = job.get("locations")
    if isinstance(multi, list) and multi:
        entries = []
        for loc in multi:
            entry = _location_entry_from_dict(loc)
            if entry and entry not in entries:
                entries.append(entry)
        if entries:
            return entries

    single = job.get("location")
    entry = _location_entry_from_dict(single)
    if entry:
        return [entry]

    # Last resort: neither structured field gave us anything, but
    # there's a free-text "location_str" display value - run that
    # through geoloc's main detection path same as the other scrapers.
    location_str = single.get("location_str") if isinstance(single, dict) else None
    if location_str:
        from .codes.geoloc import extract_locations as _geoloc_extract_locations
        return _geoloc_extract_locations([location_str])

    return []


# ============================================================
# PARSE - remote status
# ============================================================
#
# Workable models this explicitly via location.telecommuting (a real
# bool) and/or location.workplace_type ("remote"/"hybrid"/"on_site").
# Same pattern as every other scraper: a real bool goes straight to
# is_remote()'s explicit flag; text gets folded into the keyword
# signal alongside the location string and title.

def _extract_remote(job, location):
    location_str = (location or {}).get("location_str") if isinstance(location, dict) else None
    title = job.get("title")

    telecommuting = (location or {}).get("telecommuting") if isinstance(location, dict) else None
    workplace_type = (location or {}).get("workplace_type") if isinstance(location, dict) else None

    if isinstance(telecommuting, bool):
        return is_remote(location_str, title, ats_remote_flag=telecommuting)

    remote_text = workplace_type if isinstance(workplace_type, str) else None
    remote_signal = " ".join(filter(None, [location_str, remote_text]))
    return is_remote(remote_signal, title)


# ============================================================
# PARSE - full job
# ============================================================

def _parse_job(job, token):
    """Turn a raw Workable job into our clean job shape."""
    title = job.get("title")
    location = job.get("location") if isinstance(job.get("location"), dict) else None

    job_id = _first_present(job, _ID_KEYS)
    job_url = _first_present(job, _URL_KEYS)

    parsed = {
        "company": token,
        "title": title,
        "url": job_url,
        "date": days_since(_first_present(job, _POSTED_KEYS)),
        "experience_level": extract_experience_level(title),
        "remote": _extract_remote(job, location),
    }

    locations = _extract_locations(job)
    if locations:
        parsed["locations"] = locations

    return parsed


# ============================================================
# FETCH JOBS
# ============================================================
# Workable's public widget endpoint isn't paginated - one call returns
# every open job for an account. `offset` is accepted only so the
# signature matches other scrapers; it's unused here.

def fetch_jobs(tenant_info, offset=0):
    """
    Fetch jobs for one Workable tenant.

    Returns a dict:
        jobs:        list of cleaned job dicts fetched in this call
        total:       total job count if known, else None
        error:       human-readable error message, or None on success
        error_type:  "not_found" (account genuinely doesn't exist),
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
                # Account genuinely doesn't exist - no point retrying.
                return {
                    "jobs": [], "total": None,
                    "error": "no Workable account found (404)",
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
            # A healthy account returns {"name": ..., "jobs": [...]}.
            # Anything without a "jobs" list means a bad/unrecognized
            # slug or an unexpected response shape.
            raw_jobs = data.get("jobs") if isinstance(data, dict) else None

            if not isinstance(raw_jobs, list):
                return {
                    "jobs": [], "total": None,
                    "error": f"unexpected Workable response shape: {type(data).__name__}",
                    "error_type": "not_found",
                    "done": True, "next_offset": None,
                }

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