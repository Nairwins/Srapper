import json
import re
from pathlib import Path


# ============================================================
# LOAD REFERENCE DATA
# ============================================================

LOCATIONS_FILE = Path(__file__).parent / "locations.json"

with open(LOCATIONS_FILE, "r", encoding="utf-8") as f:
    LOCATION_DATA = json.load(f)


POPULAR_COUNTRIES = LOCATION_DATA["countries"]
POPULAR_CITIES = LOCATION_DATA["cities"]


# ============================================================
# LOOKUP TABLES
# ============================================================

def _build_country_lookup():
    lookup = [
        (alias.lower(), canonical)
        for canonical, aliases in POPULAR_COUNTRIES.items()
        for alias in aliases
    ]

    lookup.sort(
        key=lambda pair: len(pair[0]),
        reverse=True
    )

    return lookup


def _build_city_lookup():
    lookup = [
        (city.lower(), city, country)
        for country, cities in POPULAR_CITIES.items()
        for city in cities
    ]

    lookup.sort(
        key=lambda triple: len(triple[0]),
        reverse=True
    )

    return lookup


_COUNTRY_LOOKUP = _build_country_lookup()
_CITY_LOOKUP = _build_city_lookup()


# ============================================================
# MATCHING
# ============================================================

def _word_boundary_search(needle, haystack):
    pattern = r"\b" + re.escape(needle) + r"\b"
    return re.search(
        pattern,
        haystack,
        re.IGNORECASE
    ) is not None


def guess_location(raw):
    if not raw or not raw.strip():
        return None

    text = raw.strip()

    # Cities first
    for city_lower, city_canonical, country in _CITY_LOOKUP:

        if _word_boundary_search(city_lower, text):
            return {
                "city": city_canonical,
                "country": country
            }

    # Countries second
    for alias, canonical in _COUNTRY_LOOKUP:

        if _word_boundary_search(alias, text):
            return {
                "country": canonical
            }

    return None


def guess_locations(raw_list):
    results = []

    for raw in raw_list or []:
        match = guess_location(raw)

        if match and match not in results:
            results.append(match)

    return results


# ============================================================
# CANONICALIZE STRUCTURED DATA
# ============================================================
# For ATS fields that are already a clean, standalone value - not free
# text to search within - e.g. Ashby's postalAddress.addressCountry /
# addressLocality. We trust these as definitively correct (no need to
# re-guess the geography), but we still want the SAME spelling every
# other scraper ends up with when geoloc recognizes that value, so the
# "country"/"city" fields line up across Lever/Ashby/Workday/Greenhouse
# output. This is an exact-match lookup, not a substring search - if
# the value doesn't match a known alias exactly, it's returned
# untouched rather than guessed at.

def canonicalize_country(text):
    if not text:
        return text

    key = text.strip().lower()

    for alias, canonical in _COUNTRY_LOOKUP:
        if alias == key:
            return canonical

    return text


def canonicalize_city(text):
    if not text:
        return text

    key = text.strip().lower()

    for city_lower, city_canonical, _country in _CITY_LOOKUP:
        if city_lower == key:
            return city_canonical

    return text


# ============================================================
# STRUCTURAL PARSING (no popular-list matching)
# ============================================================
# Cheap, structural fallback - splitting a raw free-text location
# string on commas. Used only to fill in whatever the popular-list
# matcher (guess_location) didn't recognize, so we never throw away a
# raw city/country just because it wasn't in our reference lists.

_REMOTE_PREFIX_RE = re.compile(
    r"^remote\s*[-,]\s*(.+)$",
    re.IGNORECASE
)


def parse_location_string(raw):
    if not raw:
        return None

    cleaned = raw.strip()

    if not cleaned:
        return None

    remote_match = _REMOTE_PREFIX_RE.match(cleaned)

    if remote_match:
        cleaned = remote_match.group(1).strip()
    elif cleaned.lower() == "remote":
        return None  # no usable geography at all

    parts = [
        p.strip()
        for p in cleaned.split(",")
        if p.strip()
    ]

    if not parts:
        return None

    if len(parts) >= 2:
        # "City, State/Country" -> first token is city, last is country/region
        return {
            "country": parts[-1],
            "city": parts[0]
        }

    # Single token: ambiguous, most single-value locations at this
    # granularity are country/region-level (e.g. "Germany"), so treat
    # it as country.
    return {
        "country": parts[0]
    }


_MULTI_LOCATION_SPLIT_RE = re.compile(
    r"\s*(?:;|\bor\b)\s*",
    re.IGNORECASE
)


def split_multi_location(raw):
    """Some ATSes (Workday, Greenhouse) cram multiple locations into a
    single free-text field instead of a list, e.g.
    "New York, NY; Remote - USA" or "San Francisco or Remote". Split
    that into individual raw location strings so each piece gets its
    own shot at guess_location()/parse_location_string(). A plain
    single-location string passes through untouched as a 1-item list.
    """
    if not raw:
        return []

    return [
        p.strip()
        for p in _MULTI_LOCATION_SPLIT_RE.split(raw)
        if p.strip()
    ]


def _resolve_piece(piece):
    """Resolve one raw location piece into {"country":..., "city":...}.

    geoloc's popular-list matcher is the MAIN detection method now, not
    a last resort - it runs first, and whatever it recognizes wins (in
    the common, canonical spelling). The cheap comma-split is only used
    to fill in a country/city that geoloc didn't recognize, so a real
    but unlisted city (e.g. "Leeds, UK") still comes through as
    {"country": "United Kingdom", "city": "Leeds"} instead of being
    dropped just because "Leeds" isn't in our reference list.
    """
    geo = guess_location(piece)
    structural = parse_location_string(piece)

    if geo is None:
        return structural

    country = geo.get("country") or (structural or {}).get("country")
    city = geo.get("city") or (structural or {}).get("city")

    entry = {}
    if country:
        entry["country"] = country
    if city:
        entry["city"] = city

    return entry or None


def extract_locations(raw_strings):
    """The one function every scraper should call to go from "some raw
    free-text location strings the ATS gave us" to our standard
    `locations` output shape: a de-duplicated list of
    {"country":..., "city":...} dicts (city omitted when unknown),
    using the SAME canonical spelling everywhere a location is
    recognized.

    geoloc's popular-list matching is the main detection method -
    it's tried first for every piece. The comma-split structural
    parse only fills in whatever geoloc didn't recognize (never
    replaces it), and a piece that resolves neither way is silently
    dropped, same as every scraper already did before this existed.

    This is for scrapers whose ATS only gives free text. If the ATS
    gives you actual structured, definitive city/country fields (e.g.
    Ashby's postalAddress), skip this entirely and use
    canonicalize_country()/canonicalize_city() on those instead - no
    need to re-guess geography we were already told directly.
    """
    locations = []

    for raw in raw_strings or []:
        for piece in split_multi_location(raw):
            entry = _resolve_piece(piece)

            if entry and entry not in locations:
                locations.append(entry)

    return locations


def resolve_locations(
    raw_list,
    existing_locations,
    default_factory=None
):
    if existing_locations:
        return existing_locations

    resolved = []

    for raw in raw_list or []:

        match = _resolve_piece(raw)

        if match is None and default_factory is not None:
            match = default_factory(raw)

        if match and match not in resolved:
            resolved.append(match)

    return resolved