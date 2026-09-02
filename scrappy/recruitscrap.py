import json
import re
import time
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from .codes.date import days_since
from .codes.exp import extract_experience_level
from .codes.remoty import is_remote
from .codes.geoloc import canonicalize_country, canonicalize_city, extract_locations as _geoloc_extract_locations


# ============================================================
# CONFIG
# ============================================================
#
# Recruitee's public "Careers Site" surface is a documented,
# unauthenticated endpoint per company subdomain:
#   GET https://{token}.recruitee.com/api/offers/
# -> {"offers": [ {...} ]}
#
# Field shapes below are confirmed against a real, live tenant
# response (not just the API docs, which describe a slightly
# different payload elsewhere - e.g. the webhook payload's "offer"
# object nests a raw "full_address" string, but this endpoint gives
# clean, separate "city"/"country"/"country_code" fields instead - so
# trust what's implemented here over generic Recruitee doc examples).
#
# Quirks worth knowing:
#   1. The same "offer" object models BOTH job postings and talent
#      pools, disambiguated by a "kind" field ("job" vs
#      "talent_pool"), with a "status" field as a second signal. We
#      filter to published jobs only (staying permissive when either
#      field is absent, in case an account's response omits it) so
#      talent pools never show up as fake job postings.
#   2. "country" text can be in the company's own locale rather than
#      English (e.g. "Nederland" for a Dutch account) - "country_code"
#      (ISO 3166-1 alpha-2, locale-independent) is preferred and
#      mapped to an English name before canonicalizing, falling back
#      to the raw "country" text only when there's no code.
#   3. Timestamps ("created_at"/"published_at"/"updated_at") come back
#      as "2026-08-26 13:17:31 UTC" - space-separated with a trailing
#      "UTC" literal, NOT real ISO-8601. That silently breaks strict
#      ISO parsing (including days_since()), so it's reshaped into
#      real ISO-8601 first.

NAME = "RECRUITEE"
TENANTS_FILE = "data/tenants.json"

BASE_URL = "https://{token}.recruitee.com/api/offers/"
JOB_URL = "https://{token}.recruitee.com/o/{slug}"

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
    """Parse the Recruitee tenants file into a list of tenant_info dicts.

    Each tenant_info has:
        key:   unique id used internally (the Recruitee subdomain)
        label: display name / output filename stem
        token: the Recruitee subdomain (e.g. "acme" from
               https://acme.recruitee.com)
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
# ISO 3166-1 ALPHA-2 -> COUNTRY NAME
# ============================================================
# Recruitee's structured locations give a reliable country_code (e.g.
# "DE", "PL") but not a spelled-out name, so it needs converting
# before it means anything to canonicalize_country() or lines up with
# what the other scrapers produce for the same country. Not
# exhaustive - just common business/tech geography - an unmapped code
# is left as-is (still usable, just not spelled out).

_ISO_ALPHA2_TO_COUNTRY = {
    "US": "United States", "GB": "United Kingdom", "CA": "Canada",
    "DE": "Germany", "FR": "France", "NL": "Netherlands", "IE": "Ireland",
    "ES": "Spain", "IT": "Italy", "PT": "Portugal", "BE": "Belgium",
    "AT": "Austria", "CH": "Switzerland", "SE": "Sweden", "NO": "Norway",
    "DK": "Denmark", "FI": "Finland", "PL": "Poland", "CZ": "Czech Republic",
    "RO": "Romania", "HU": "Hungary", "GR": "Greece", "BG": "Bulgaria",
    "HR": "Croatia", "SK": "Slovakia", "SI": "Slovenia", "LT": "Lithuania",
    "LV": "Latvia", "EE": "Estonia", "UA": "Ukraine", "IS": "Iceland",
    "LU": "Luxembourg", "MT": "Malta", "CY": "Cyprus",
    "IN": "India", "CN": "China", "JP": "Japan", "KR": "South Korea",
    "SG": "Singapore", "HK": "Hong Kong", "TW": "Taiwan", "PH": "Philippines",
    "ID": "Indonesia", "VN": "Vietnam", "TH": "Thailand", "MY": "Malaysia",
    "PK": "Pakistan", "BD": "Bangladesh",
    "AU": "Australia", "NZ": "New Zealand",
    "BR": "Brazil", "MX": "Mexico", "AR": "Argentina", "CL": "Chile",
    "CO": "Colombia", "PE": "Peru", "UY": "Uruguay",
    "IL": "Israel", "AE": "United Arab Emirates", "SA": "Saudi Arabia",
    "TR": "Turkey", "EG": "Egypt", "ZA": "South Africa", "NG": "Nigeria",
    "KE": "Kenya", "MA": "Morocco",
    "RU": "Russia",
}


def _country_name_from_code(code):
    if not code:
        return None
    return _ISO_ALPHA2_TO_COUNTRY.get(code.strip().upper(), code)


# ============================================================
# PARSE - dates
# ============================================================
# Confirmed against a live Recruitee tenant: "created_at"/
# "published_at"/"updated_at" come back as "2026-08-26 13:17:31 UTC" -
# space-separated (not "T"), with a trailing "UTC" literal instead of
# a "+00:00"/"Z" offset. That trips up standard ISO-8601 parsing
# (including whatever days_since() expects), which silently produced
# no date at all. Reshape it into real ISO-8601 first.

_RECRUITEE_TIMESTAMP_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})\s*UTC$",
    re.IGNORECASE
)


def _normalize_timestamp(raw):
    if not raw:
        return None

    match = _RECRUITEE_TIMESTAMP_RE.match(raw.strip())
    if match:
        return f"{match.group(1)}T{match.group(2)}+00:00"

    return raw  # unrecognized shape - pass through as-is, same as before


# ============================================================
# PARSE - locations
# ============================================================

def _location_entry_from_fields(d):
    """Build one {country, city} entry from a dict that carries direct
    "city"/"country"/"country_code" keys - true of both an entry in
    the "locations" array and the offer's own top-level fields.

    Real responses from Recruitee's public offers endpoint (confirmed
    against a live tenant) give these as clean, separate fields - no
    positional address-string parsing needed. One wrinkle: "country"
    can come back in the company's own locale rather than English
    (e.g. "Nederland" instead of "Netherlands" for a Dutch account),
    so country_code (locale-independent, ISO 3166-1 alpha-2) is
    preferred whenever present, and only the raw "country" text is
    used as a fallback when there's no code to work from."""
    if not d or not isinstance(d, dict):
        return None

    country_code = d.get("country_code")
    country = _country_name_from_code(country_code) if country_code else d.get("country")
    city = d.get("city")

    if not country and not city:
        return None

    entry = {}
    if country:
        entry["country"] = canonicalize_country(country)
    if city:
        entry["city"] = canonicalize_city(city)

    return entry or None


def _extract_locations(offer):
    # Prefer the structured "locations" array - definitive, ATS-
    # provided geography, one entry per posted location.
    structured = offer.get("locations")
    if isinstance(structured, list) and structured:
        entries = []
        for loc in structured:
            entry = _location_entry_from_fields(loc)
            if entry and entry not in entries:
                entries.append(entry)
        if entries:
            return entries

    # Fallback: the offer's own top-level city/country/country_code
    # fields (present even when "locations" is empty on some accounts).
    top_level_entry = _location_entry_from_fields(offer)
    if top_level_entry:
        return [top_level_entry]

    # Last resort: only a free-text "location" display string - run
    # that through geoloc's main detection path, same as Lever/
    # Greenhouse's free-text locations.
    location_text = offer.get("location")
    if location_text:
        return _geoloc_extract_locations([location_text])

    return []


# ============================================================
# PARSE - full job
# ============================================================

def _is_job_offer(offer):
    """The "offers" list also contains talent pools, which share the
    same object shape - only the "kind" field tells them apart. Stay
    permissive when a field is absent entirely rather than dropping
    everything, in case an account's response omits it. Also skip
    anything explicitly not "published" (confirmed present as a
    "status" field on live responses) - defensive belt-and-suspenders
    even though the public endpoint should only be listing published
    offers already."""
    kind = offer.get("kind")
    if kind is not None and kind != "job":
        return False

    status = offer.get("status")
    if status is not None and status != "published":
        return False

    return True


def _parse_job(offer, token):
    """Turn a raw Recruitee offer into our clean job shape."""
    title = offer.get("title")
    slug = offer.get("slug")
    company = offer.get("company_name") or token

    job_url = offer.get("careers_url") or offer.get("careers_apply_url")
    if not job_url and slug:
        job_url = JOB_URL.format(token=token, slug=slug)

    location_text = offer.get("location")
    remote_flag = offer.get("remote")

    if isinstance(remote_flag, bool):
        remote = is_remote(location_text, title, ats_remote_flag=remote_flag)
    else:
        remote = is_remote(location_text, title)

    parsed = {
        "company": company,
        "title": title,
        "url": job_url,
        "date": days_since(_normalize_timestamp(offer.get("published_at") or offer.get("created_at"))),
        "experience_level": extract_experience_level(title),
        "remote": remote,
    }

    locations = _extract_locations(offer)
    if locations:
        parsed["locations"] = locations

    return parsed


# ============================================================
# FETCH JOBS
# ============================================================
# Recruitee's public offers endpoint isn't paginated - one call
# returns every offer for an account. `offset` is accepted only so
# the signature matches other scrapers; it's unused here.

def fetch_jobs(tenant_info, offset=0):
    """
    Fetch jobs for one Recruitee tenant.

    Returns a dict:
        jobs:        list of cleaned job dicts fetched in this call
                     (talent pools filtered out)
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
                    "error": "no Recruitee account found (404)",
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
            raw_offers = data.get("offers") if isinstance(data, dict) else None

            if not isinstance(raw_offers, list):
                return {
                    "jobs": [], "total": None,
                    "error": f"unexpected Recruitee response shape: {type(data).__name__}",
                    "error_type": "not_found",
                    "done": True, "next_offset": None,
                }

            jobs = [
                _parse_job(offer, token)
                for offer in raw_offers
                if _is_job_offer(offer)
            ]

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