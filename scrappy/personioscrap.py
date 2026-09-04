import json
import re
import time
import xml.etree.ElementTree as ET
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from .codes.date import days_since
from .codes.exp import extract_experience_level
from .codes.remoty import is_remote
from .codes.geoloc import extract_locations as _geoloc_extract_locations


# ============================================================
# CONFIG
# ============================================================

NAME = "PERSONIO"
TENANTS_FILE = "data/personio.json"

BASE_URL = "https://{token}.jobs.personio.de/xml?language=en"
JOB_URL = "https://{token}.jobs.personio.de/job/{job_id}"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; job-research-script/1.0)"
}

MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 3.0


# ============================================================
# LOAD TENANTS
# ============================================================

def load_tenants(tenants_file=None):
    """Parse the Personio tenants file into a list of tenant_info dicts.

    Each tenant_info has:
        key:   unique id used internally (the Personio subdomain)
        label: display name / output filename stem
        token: the Personio subdomain (e.g. "circus" from
               https://circus.jobs.personio.de)
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

def _fetch_xml_bytes(url):
    """Return the raw response bytes, undecoded. Personio's XML feed
    declares its own encoding in the prolog (encoding="UTF-8") - let
    ET.fromstring() read that declaration and decode correctly itself,
    rather than us guessing a codec up front. Force-decoding here as
    UTF-8 regardless of what the server actually sent is exactly how
    you get silent mojibake (e.g. "München" turning into "MÃ¼nchen")
    on any response that isn't already clean UTF-8."""
    req = Request(url, headers=HEADERS)
    with urlopen(req, timeout=15) as resp:
        return resp.read()


# ============================================================
# PARSE - experience level
# ============================================================
#
# Priority (explicit signal first, keyword fallback second - same
# pattern as leverscrap.py's commitment/title handling):
#   1. employmentType == "intern"/"trainee"  -> intern, decisive
#   2. seniority mapped through the table below, when it's one of
#      Personio's four known values           -> decisive
#   3. keyword search over the title           -> fallback
#
# seniority is an optional dropdown field companies frequently leave
# blank, so it's common to fall all the way through to the title.

_SENIORITY_MAP = {
    "student": "intern",
    "entry-level": "junior",
    "experienced": "mid",
    "executive": "senior",
}


def _classify_experience(employment_type, seniority, title):
    employment_type = (employment_type or "").strip().lower()
    if employment_type in ("intern", "trainee"):
        return "intern"

    mapped = _SENIORITY_MAP.get((seniority or "").strip().lower())
    if mapped:
        return mapped

    return extract_experience_level(title)


# ============================================================
# PARSE - misc
# ============================================================

_TZ_NO_COLON_RE = re.compile(r"([+-]\d{2})(\d{2})$")


def _iso_with_colon(timestamp):
    """Personio's createdAt offset has no colon ("+0200"). Older
    Pythons' fromisoformat() (which days_since() uses) only accept
    "+02:00", so normalize it defensively before handing it off."""
    if not timestamp:
        return timestamp
    return _TZ_NO_COLON_RE.sub(r"\1:\2", timestamp.strip())


# office text observed in real feeds sometimes leads with a workplace
# descriptor ("Hybrid - Barcelona, Spain", "On-site - Berlin"). geoloc's
# structural fallback already strips a "Remote -" prefix on its own,
# but not "Hybrid"/"On-site" - left alone, that prefix leaks into the
# parsed city (e.g. "Hybrid - Barcelona" as the "city") whenever the
# real city isn't in geoloc's popular-city list. Strip it here, before
# handing the string to geoloc, for location-extraction purposes only
# - the untouched original is still what's used for remote detection.
_WORKPLACE_PREFIX_RE = re.compile(r"^(?:hybrid|on-?site)\s*[-:,]\s*", re.IGNORECASE)


def _clean_for_geoloc(office):
    return _WORKPLACE_PREFIX_RE.sub("", office).strip()


def _extract_locations(offices):
    if not offices:
        return []
    return _geoloc_extract_locations([_clean_for_geoloc(o) for o in offices])


def _all_offices(position):
    """Personio jobs can have a primary <office> plus zero or more
    extra ones nested under <additionalOffices><office>...</office>
    (can be a single <office> or several). Collect every office string
    so multi-location postings ("Hybrid - Barcelona, Spain" +
    "Hybrid - London") don't silently lose all but the first."""
    offices = []

    primary = position.findtext("office")
    if primary and primary.strip():
        offices.append(primary.strip())

    additional = position.find("additionalOffices")
    if additional is not None:
        for office_el in additional.findall("office"):
            if office_el.text and office_el.text.strip():
                offices.append(office_el.text.strip())

    return offices


def _parse_job(position, token):
    job_id = position.findtext("id")
    title = position.findtext("name")
    offices = _all_offices(position)
    office_signal = " ".join(offices)
    employment_type = position.findtext("employmentType")
    seniority = position.findtext("seniority")
    created_at = position.findtext("createdAt")

    parsed = {
        "company": token,
        "title": title,
        "url": JOB_URL.format(token=token, job_id=job_id) if job_id else None,
        "date": days_since(_iso_with_colon(created_at)),
        "experience_level": _classify_experience(employment_type, seniority, title),
        "remote": is_remote(office_signal, title),
    }

    locations = _extract_locations(offices)
    if locations:
        parsed["locations"] = locations

    return parsed


# ============================================================
# FETCH JOBS
# ============================================================
# Personio's XML feed isn't paginated - one call returns every open
# job for a board. So this always finishes in a single round trip.
# `offset` is accepted only so the signature matches other scrapers;
# it's unused here.

def fetch_jobs(tenant_info, offset=0):
    """
    Fetch jobs for one Personio tenant.

    Returns a dict:
        jobs:        list of cleaned job dicts fetched in this call
        total:       total job count if known, else None
        error:       human-readable error message, or None on success
        error_type:  "not_found" (board genuinely doesn't exist),
                     "rate_limited", "network_error", "http_error",
                     "parse_error" (malformed/unexpected XML), or None
                     on success. Only "not_found" should ever be
                     treated as "this tenant is broken, remove it" -
                     the others are transient/ambiguous and may
                     succeed on retry.
        done:        True if there is nothing left to fetch after this call
        next_offset: offset to pass on the next call (None when done)
    """
    token = tenant_info["token"]
    url = BASE_URL.format(token=token)

    last_error = None
    last_error_type = None

    for attempt in range(MAX_RETRIES + 1):
        try:
            raw_bytes = _fetch_xml_bytes(url)

        except HTTPError as e:
            if e.code == 404:
                # Subdomain genuinely doesn't exist - no point retrying.
                return {
                    "jobs": [], "total": None,
                    "error": "no Personio job board found (404)",
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
            try:
                root = ET.fromstring(raw_bytes)
            except ET.ParseError as e:
                last_error = f"unparseable XML: {e}"
                last_error_type = "parse_error"
                wait = RETRY_BACKOFF_SECONDS * (attempt + 1)
            else:
                positions = root.findall(".//position")
                jobs = [_parse_job(position, token) for position in positions]

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
