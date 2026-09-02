import json
import re

import requests

from .codes.exp import extract_experience_level
from .codes.remoty import is_remote
from .codes.date import parse_posted_days_ago
from .codes.geoloc import extract_locations as _geoloc_extract_locations


# ============================================================
# CONFIG
# ============================================================

NAME = "WORKDAY"
TENANTS_FILE = "data/tenants.json"
PAGE_SIZE = 20


# ============================================================
# SESSION
# ============================================================

_session = requests.Session()

_session.headers.update({
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json",
    "Content-Type": "application/json",
})

_adapter = requests.adapters.HTTPAdapter(
    pool_connections=16,
    pool_maxsize=16,
)

_session.mount("https://", _adapter)
_session.mount("http://", _adapter)


# ============================================================
# LOAD TENANTS
# ============================================================

def load_tenants(tenants_file=None):

    path = tenants_file or TENANTS_FILE

    with open(path, "r", encoding="utf-8") as f:
        raw_list = json.load(f)

    tenants = []

    for entry in raw_list:
        parts = entry.split("|")

        if len(parts) != 3:
            print(f"  [!] SKIPPING malformed entry: {entry}")
            continue

        tenant, wd, site = (p.strip() for p in parts)

        tenants.append({
            "key": f"{tenant}|{wd}|{site}",
            "label": tenant,
            "tenant": tenant,
            "wd": wd,
            "site": site,
        })

    return tenants


# ============================================================
# BUILD URL
# ============================================================

def _build_url(tenant, wd, site):
    return (
        f"https://{tenant}.{wd}.myworkdayjobs.com"
        f"/wday/cxs/{tenant}/{site}/jobs"
    )


# ============================================================
# FETCH ONE PAGE (raw HTTP)
# ============================================================

def _fetch_page(url, offset):
    payload = {
        "limit": PAGE_SIZE,
        "offset": offset,
        "searchText": "",
    }

    try:
        response = _session.post(url, json=payload, timeout=30)

        if response.status_code != 200:
            return None, f"HTTP {response.status_code}"

        return response.json(), None

    except requests.RequestException as e:
        return None, f"request error: {e}"
    except ValueError:
        return None, "invalid JSON response"


# ============================================================
# PARSE
# ============================================================

_LOCATION_FROM_PATH_RE = re.compile(r"/job/([^/]+)/")
_GENERIC_LOCATION_COUNT_RE = re.compile(r"^\d+\s+Locations?$", re.IGNORECASE)


def _extract_location_from_path(external_path):
    """Workday job URLs embed a location slug like "san-francisco-ca" or
    "new-york-ny" - city words joined by hyphens, then a trailing
    2-3 letter state/country code, also hyphen-joined. Blindly
    replacing every hyphen with a comma (the old approach) mangles any
    multi-word city ("san-francisco-ca" -> "san, francisco, ca", losing
    "francisco" once comma-split). Instead, treat only the LAST
    hyphen-segment as the region and join everything before it back
    into the city name."""
    if not external_path:
        return None

    match = _LOCATION_FROM_PATH_RE.search(external_path)
    if not match:
        return None

    parts = [p for p in match.group(1).split("-") if p]
    if not parts:
        return None

    if len(parts) == 1:
        return parts[0].replace("_", " ").title()

    city = " ".join(parts[:-1]).replace("_", " ").title()
    region = parts[-1]
    region = region.upper() if len(region) <= 3 else region.title()

    return f"{city}, {region}"


def _is_useful_location(text):
    if not text:
        return False
    return not _GENERIC_LOCATION_COUNT_RE.match(text.strip())


def _extract_locations(api_locations, path_location):
    """Same `locations` output shape as every other scraper: a list of
    {"country", "city"} dicts, "city" omitted when unknown.

    Prefer the API's own location text (it can list several locations
    joined with "; ", e.g. "New York, NY; Remote - USA" - geoloc's
    extract_locations() splits that apart). Only fall back to the
    URL-derived location when the API text isn't useful (e.g.
    Workday's generic "3 Locations" placeholder). Either way, geoloc's
    popular-list matcher does the actual detection and canonicalizes
    the spelling to match every other scraper's output."""
    if _is_useful_location(api_locations):
        raw_candidates = [api_locations]
    elif path_location:
        raw_candidates = [path_location]
    elif api_locations:
        raw_candidates = [api_locations]
    else:
        raw_candidates = []

    return _geoloc_extract_locations(raw_candidates)


def _parse_job(job, tenant, wd, site):
    """Turn a raw Workday job into our clean job shape."""
    external_path = job.get("externalPath")

    job_url = (
        f"https://{tenant}.{wd}.myworkdayjobs.com/{site}{external_path}"
        if external_path
        else None
    )

    api_locations = job.get(
        "locationsText",
        job.get("bulletFields", [None])[0],
    )
    path_location = _extract_location_from_path(external_path)

    locations = _extract_locations(api_locations, path_location)

    # Check both sources for "remote" - the URL segment is often the
    remote_signal = " ".join(filter(None, [api_locations, path_location]))

    remote = is_remote(
        remote_signal,
        job.get("title"),
        ats_remote_flag=job.get("IsRemote"),
    )

    posted_days = parse_posted_days_ago(job.get("postedOn"))

    clean_job = {
        "company": tenant,
        "title": job.get("title"),
        "url": job_url,
        "experience_level": extract_experience_level(job.get("title")),
    }

    if locations:
        clean_job["locations"] = locations

    if posted_days is not None:
        clean_job["date"] = posted_days

    if remote:
        clean_job["remote"] = True

    return clean_job


# ============================================================
# FETCH JOBS
# ============================================================

def fetch_jobs(tenant_info, offset):

    tenant = tenant_info["tenant"]
    wd = tenant_info["wd"]
    site = tenant_info["site"]

    url = _build_url(tenant, wd, site)
    data, error = _fetch_page(url, offset)

    if error:
        return {"jobs": [], "total": None, "error": error, "done": True, "next_offset": None}

    raw_jobs = data.get("jobPostings", [])
    total = data.get("total")

    if not raw_jobs:
        return {"jobs": [], "total": total, "error": None, "done": True, "next_offset": None}

    jobs = [_parse_job(j, tenant, wd, site) for j in raw_jobs]

    next_offset = offset + len(raw_jobs)
    done = bool(total) and next_offset >= total

    return {
        "jobs": jobs,
        "total": total,
        "error": None,
        "done": done,
        "next_offset": None if done else next_offset,
    }