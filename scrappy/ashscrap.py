import json
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

NAME = "ASHBY"
TENANTS_FILE = "data/ashby.json"

BASE_URL = "https://api.ashbyhq.com/posting-api/job-board/{token}"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; job-research-script/1.0)"
}

# Retry transient failures (rate limits, network blips) instead of
MAX_RETRIES = 2
RETRY_BACKOFF_SECONDS = 2.0


# ============================================================
# LOAD TENANTS
# ============================================================

def load_tenants(tenants_file=None):
    """Parse the Ashby tenants file into a list of tenant_info dicts.

    Each tenant_info has:
        key:   unique id used internally (the job board token/slug)
        label: display name / output filename stem
        token: the Ashby job board slug (e.g. "notion" from
               https://jobs.ashbyhq.com/notion)
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

def _location_entry_from_postal(postal):
    """Build one {country, city} entry from a postalAddress object.
    `city` is omitted entirely when Ashby didn't provide one - never a
    null placeholder. Returns None if there's no country either (nothing
    usable in this address)."""
    if not postal:
        return None

    country = postal.get("addressCountry")
    city = postal.get("addressLocality")

    if not country:
        return None

    entry = {"country": country}
    if city:
        entry["city"] = city

    return entry


def _extract_locations(job):
    """Collect one {country, city} entry per address mentioned across the
    job's primary address AND all of its secondaryLocations, in order,
    de-duplicated. Returns a list - empty if Ashby gave us no address
    data at all.

    Structured postalAddress data (real ATS-provided city/country) is
    always preferred. Only when Ashby gives us NO postal address at
    all do we fall back, as a last resort, to parsing/matching the
    free-text `location` display field via geoloc."""
    locations = []

    def add(entry):
        if entry and entry not in locations:
            locations.append(entry)

    add(_location_entry_from_postal((job.get("address") or {}).get("postalAddress")))

    for secondary in job.get("secondaryLocations") or []:
        add(_location_entry_from_postal((secondary.get("address") or {}).get("postalAddress")))

    if not locations:
        for entry in _geoloc_extract_locations([job.get("location")]):
            add(entry)

    return locations


def _parse_job(job, token, org_name):
    """Turn a raw Ashby job into our clean job shape."""
    title = job.get("title")
    location_name = job.get("location") or ""

    # Ashby models remote status two ways: a nullable boolean `isRemote`,
    # or a free-text `workplaceType` ("Remote"/"Hybrid"/"InOffice"/None).
    # Same handling as Greenhouse's metadata: a real bool goes straight to
    # is_remote()'s explicit flag; otherwise workplaceType (if present)
    # gets folded into the keyword-search signal alongside location.
    is_remote_flag = job.get("isRemote")
    workplace_type = job.get("workplaceType")

    if isinstance(is_remote_flag, bool):
        remote = is_remote(location_name, title, ats_remote_flag=is_remote_flag)
    else:
        remote_text = workplace_type if isinstance(workplace_type, str) else None
        remote_signal = " ".join(filter(None, [location_name, remote_text]))
        remote = is_remote(remote_signal, title)

    parsed = {
        "company": org_name or token,
        "title": title,
        "url": job.get("jobUrl"),
        "date": days_since(job.get("publishedAt")),
        "experience_level": extract_experience_level(title),
        "remote": remote,
    }

    # NOTE: Ashby's "locations" (plural) is a list of {country, city}
    # pairs - deliberately NOT the same shape as Greenhouse/Workday's
    # "location" (singular), which is a plain display string. Keeping
    # the key names different avoids consumers silently treating a list
    # as a string (or vice versa) when merging output across scrapers.
    locations = _extract_locations(job)
    if locations:
        parsed["locations"] = locations

    return parsed


# ============================================================
# FETCH JOBS
# ============================================================
# Ashby's public job-board API isn't paginated - one call returns every
# open job for a board. So this always finishes in a single round trip.
# `offset` is accepted only so the signature matches other scrapers;
# it's unused here.

def fetch_jobs(tenant_info, offset=0):
    """
    Fetch jobs for one Ashby tenant.

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
                    "error": "no Ashby job board found (404)",
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
            # Ashby returns HTTP 200 even for an unknown job-board slug,
            # with the failure reported inside the body instead.
            if isinstance(data, dict) and data.get("errors"):
                return {
                    "jobs": [], "total": None,
                    "error": f"Ashby API error: {data['errors']}",
                    "error_type": "not_found",
                    "done": True, "next_offset": None,
                }

            org_name = data.get("organizationName") or token
            raw_jobs = data.get("jobs", [])
            jobs = [_parse_job(j, token, org_name) for j in raw_jobs]

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