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

NAME = "BAMBOOHR"
TENANTS_FILE = "data/bamboohr.json"

BASE_URL = "https://{token}.bamboohr.com/careers/list"
JOB_URL = "https://{token}.bamboohr.com/careers/{job_id}"

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
    """Parse the BambooHR tenants file into a list of tenant_info dicts.

    Each tenant_info has:
        key:   unique id used internally (the BambooHR subdomain)
        label: display name / output filename stem
        token: the BambooHR subdomain (e.g. "acme" from
               https://acme.bamboohr.com/careers)
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
# FIELD-NAME RESILIENCE
# ============================================================
# The widget's JSON has drifted between BambooHR releases in the
# wild, so every field is read via a list of plausible candidate
# keys rather than one hardcoded name.

def _first_present(d, keys):
    if not d:
        return None
    for key in keys:
        value = d.get(key)
        if value not in (None, ""):
            return value
    return None


_TITLE_KEYS = ["jobOpeningName", "jobTitle", "title", "name"]
_ID_KEYS = ["id", "jobOpeningId", "jobId"]
_POSTED_KEYS = ["postedDate", "datePosted", "createdDate", "publishedDate"]
_REMOTE_FLAG_KEYS = ["isRemote", "remote"]
_LOCATION_OBJ_KEYS = ["location", "jobLocation", "atsLocation"]
_LOCATION_DISPLAY_KEYS = ["locationLabel", "locationText", "displayLocation"]

_LOCATION_CITY_KEYS = ["city", "addressLocality"]
_LOCATION_STATE_KEYS = ["state", "region", "addressRegion"]
_LOCATION_COUNTRY_KEYS = ["country", "addressCountry"]


# ============================================================
# PARSE - locations
# ============================================================
#
# BambooHR's location field is a structured object (city/state/
# country), similar in spirit to Ashby's postalAddress - so, same as
# Ashby, we treat it as definitive ATS-provided data and never re-run
# it through geoloc's popular-list guesser. We DO still run country
# and city through canonicalize_country()/canonicalize_city() (an
# exact-match lookup, not a search) so the spelling matches every
# other scraper's output for the same place. If BambooHR gives us no
# structured location object at all, fall back to whatever plain
# display-text field is present, parsed the same way Lever/Workday/
# Greenhouse's free text is.

def _location_entry_from_object(location_obj):
    if not location_obj or not isinstance(location_obj, dict):
        return None

    country = _first_present(location_obj, _LOCATION_COUNTRY_KEYS)
    city = _first_present(location_obj, _LOCATION_CITY_KEYS)
    state = _first_present(location_obj, _LOCATION_STATE_KEYS)

    if not country and not city and not state:
        return None

    entry = {}

    if country:
        entry["country"] = canonicalize_country(country)
    elif state:
        # No country given at all - some BambooHR boards only fill in
        # state for US-based roles. Better than nothing, left
        # uncanonicalized since it's a state, not a country.
        entry["country"] = state

    if city:
        entry["city"] = canonicalize_city(city)

    return entry or None


def _extract_locations(job):
    location_obj = _first_present(job, _LOCATION_OBJ_KEYS)
    entry = _location_entry_from_object(location_obj)

    if entry:
        return [entry]

    # Last resort: no structured location object at all - try a plain
    # display-text field via geoloc's main detection path.
    display_text = _first_present(job, _LOCATION_DISPLAY_KEYS)
    if display_text:
        from .codes.geoloc import extract_locations as _geoloc_extract_locations
        return _geoloc_extract_locations([display_text])

    return []


# ============================================================
# PARSE - full job
# ============================================================

def _parse_job(job, token):
    """Turn a raw BambooHR job into our clean job shape."""
    title = _first_present(job, _TITLE_KEYS)
    job_id = _first_present(job, _ID_KEYS)

    job_url = JOB_URL.format(token=token, job_id=job_id) if job_id else None

    location_obj = _first_present(job, _LOCATION_OBJ_KEYS)
    location_text = " ".join(filter(None, [
        (location_obj or {}).get("city") if isinstance(location_obj, dict) else None,
        (location_obj or {}).get("state") if isinstance(location_obj, dict) else None,
        (location_obj or {}).get("country") if isinstance(location_obj, dict) else None,
        _first_present(job, _LOCATION_DISPLAY_KEYS),
    ]))

    remote_flag = _first_present(job, _REMOTE_FLAG_KEYS)
    if isinstance(remote_flag, bool):
        remote = is_remote(location_text, title, ats_remote_flag=remote_flag)
    else:
        # Some boards send "Yes"/"No" strings instead of a real bool.
        remote_text = remote_flag if isinstance(remote_flag, str) else None
        remote_signal = " ".join(filter(None, [location_text, remote_text]))
        remote = is_remote(remote_signal, title)

    parsed = {
        "company": token,
        "title": title,
        "url": job_url,
        "date": days_since(_first_present(job, _POSTED_KEYS)),
        "experience_level": extract_experience_level(title),
        "remote": remote,
    }

    locations = _extract_locations(job)
    if locations:
        parsed["locations"] = locations

    return parsed


# ============================================================
# FETCH JOBS
# ============================================================
# Like the other public board endpoints, this isn't paginated - one
# call returns every open job for a board. `offset` is accepted only
# so the signature matches other scrapers; it's unused here.

def fetch_jobs(tenant_info, offset=0):
    """
    Fetch jobs for one BambooHR tenant.

    Returns a dict:
        jobs:        list of cleaned job dicts fetched in this call
        total:       total job count if known, else None
        error:       human-readable error message, or None on success
        error_type:  "not_found" (board genuinely doesn't exist, or
                     this tenant doesn't have the careers widget
                     enabled), "rate_limited", "network_error",
                     "http_error", or None on success. Only
                     "not_found" should ever be treated as "this
                     tenant is broken, remove it" - the others are
                     transient and may succeed on retry.
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
                # No BambooHR careers widget for this tenant - no point retrying.
                return {
                    "jobs": [], "total": None,
                    "error": "no BambooHR careers board found (404)",
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

        except json.JSONDecodeError as e:
            # Invalid or empty JSON response means the careers widget
            # endpoint isn't working properly - treat as a dead link.
            return {
                "jobs": [], "total": None,
                "error": f"invalid JSON response: {e}",
                "error_type": "not_found",
                "done": True, "next_offset": None,
            }

        else:
            # Known widget response shapes: a bare list, or a dict with
            # the jobs under "result" (most common) / "jobs" / "data".
            if isinstance(data, list):
                raw_jobs = data
            elif isinstance(data, dict):
                raw_jobs = data.get("result")
                if raw_jobs is None:
                    raw_jobs = data.get("jobs")
                if raw_jobs is None:
                    raw_jobs = data.get("data")
            else:
                raw_jobs = None

            if raw_jobs is None:
                # Genuinely unrecognized shape - likely means the
                # widget's JSON has drifted from what this scraper
                # expects. Flagged as not_found rather than silently
                # returning zero jobs, so it gets noticed and fixed
                # rather than mistaken for "no open roles".
                return {
                    "jobs": [], "total": None,
                    "error": f"unexpected BambooHR response shape: {type(data).__name__}",
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