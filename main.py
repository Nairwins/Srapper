"""
Vercel serverless API - serves scraped jobs from the gzip chunks in
`output/` (whatever run_scraper()/scrap.py produced), filtered and
ranked by whatever the caller asks for.

Single-file function at the project root (see vercel.json - it routes
every request straight to this file, no /api folder needed).

Deployed URL: https://srapper-bay.vercel.app/  (any path works, e.g.
also https://srapper-bay.vercel.app/jobs - see vercel.json routes)

Pure stdlib - no requirements.txt needed, nothing to install on
Vercel's build step.

--------------------------------------------------------------------
QUERY PARAMETERS
--------------------------------------------------------------------
q             REQUIRED. free-text search (matched against job title +
              company). Results are RANKED by closeness to this text.
              Missing/empty -> 400 error.
company       optional, comma-separated list, substring match against
              company e.g. company=netflix,airbnb
exp           comma-separated list of: intern, junior, mid, senior
              (alias: exp_lvl, experience, level)
country       comma-separated list, substring match against each
              job's location country (e.g. country=usa,germany)
remote        true  -> only remote jobs
              false -> only non-remote jobs
              (omit -> no filtering on remote status)
days          integer - only jobs posted within the last N days
amount        how many jobs to return. default 20, hard-capped at 100
              (alias: limit)
offset        how many matched jobs to skip before taking `amount`
              (for simple pagination). default 0

Example:
  /api/jobs?q=backend+engineer&exp=mid,senior&country=usa&days=30&amount=15

--------------------------------------------------------------------
RESPONSE SHAPE
--------------------------------------------------------------------
{
  "filters": { ...normalized filters actually applied... },
  "total_matched": 137,
  "count": 15,
  "jobs": [ {...job...}, ... ]
}
"""

import difflib
import glob
import gzip
import json
import os
import re
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# ============================================================
# CONFIG
# ============================================================

# main.py lives at the project root already.
_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))

# Where to look for scraped output. Override with the JOBS_DATA_DIR
# env var if you keep it somewhere else (e.g. mounted storage / a
# different folder committed to the repo).
DATA_DIR = os.environ.get("JOBS_DATA_DIR", os.path.join(_PROJECT_ROOT, "output"))

DEFAULT_AMOUNT = 20
MAX_AMOUNT = 100

VALID_EXP_LEVELS = {"intern", "junior", "mid", "senior"}

# ============================================================
# DATA LOADING (cached at module level - reused across warm
# invocations of the same serverless instance; a cold start just
# reloads it once).
# ============================================================

_cache = {"jobs": None, "signature": None}


def _data_signature():
    """Cheap fingerprint of what's on disk (paths + mtimes + sizes),
    so we only re-parse everything if the data actually changed."""
    paths = sorted(
        glob.glob(os.path.join(DATA_DIR, "**", "*.json.gz"), recursive=True)
        + glob.glob(os.path.join(DATA_DIR, "**", "*_jobs.json"), recursive=True)
    )
    sig = []
    for path in paths:
        try:
            stat = os.stat(path)
            sig.append((path, stat.st_mtime_ns, stat.st_size))
        except OSError:
            continue
    return tuple(sig)


def _load_one_file(path):
    try:
        if path.endswith(".gz"):
            with gzip.open(path, "rt", encoding="utf-8") as f:
                data = json.load(f)
        else:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
    except (OSError, ValueError, gzip.BadGzipFile):
        return []

    if isinstance(data, list):
        return data
    return []


def load_jobs(force=False):
    """Load + cache every job from every gzip chunk (and any loose
    *_jobs.json left un-chunked) under DATA_DIR."""
    signature = _data_signature()

    if not force and _cache["jobs"] is not None and _cache["signature"] == signature:
        return _cache["jobs"]

    all_jobs = []
    for path in sorted(
        glob.glob(os.path.join(DATA_DIR, "**", "*.json.gz"), recursive=True)
        + glob.glob(os.path.join(DATA_DIR, "**", "*_jobs.json"), recursive=True)
    ):
        all_jobs.extend(_load_one_file(path))

    _cache["jobs"] = all_jobs
    _cache["signature"] = signature
    return all_jobs


# ============================================================
# HELPERS
# ============================================================

def _split_csv(value):
    if not value:
        return []
    return [v.strip().lower() for v in value.split(",") if v.strip()]


def _normalize_days(date_value):
    """Job "date" fields are: an int (days ago), a Workday '30+'-style
    string, or None (unknown). Turn all of that into a plain int for
    filtering/sorting, treating unknown as "very old" so it never
    wins a recency sort or slips under a `days=` cutoff by accident."""
    if date_value is None:
        return 10_000
    if isinstance(date_value, int):
        return date_value
    if isinstance(date_value, str):
        match = re.match(r"^\s*(\d+)", date_value)
        if match:
            return int(match.group(1))
    return 10_000


def _job_countries(job):
    return [
        (loc.get("country") or "").lower()
        for loc in (job.get("locations") or [])
        if loc.get("country")
    ]


def _matches_filters(job, filters):
    if filters["company"]:
        company = (job.get("company") or "").lower()
        if not any(c in company for c in filters["company"]):
            return False

    if filters["exp"]:
        if (job.get("experience_level") or "").lower() not in filters["exp"]:
            return False

    if filters["country"]:
        countries = _job_countries(job)
        if not any(any(c in jc for jc in countries) for c in filters["country"]):
            return False

    if filters["remote"] is not None:
        if bool(job.get("remote")) != filters["remote"]:
            return False

    if filters["days"] is not None:
        if _normalize_days(job.get("date")) > filters["days"]:
            return False

    return True


def _relevance_score(job, query_tokens, query_lower):
    """How close a job is to the free-text query. Cheap and
    stdlib-only (difflib), not a real search engine, but good enough
    to surface the closest matches first."""
    title = (job.get("title") or "").lower()
    company = (job.get("company") or "").lower()

    title_ratio = difflib.SequenceMatcher(None, query_lower, title).ratio()

    title_tokens = set(re.findall(r"[a-z0-9]+", title))
    overlap = len(query_tokens & title_tokens)
    overlap_score = overlap / len(query_tokens) if query_tokens else 0.0

    company_bonus = 0.15 if company and company in query_lower else 0.0
    substring_bonus = 0.2 if query_lower and query_lower in title else 0.0

    return (0.5 * title_ratio) + (0.5 * overlap_score) + company_bonus + substring_bonus


def _parse_amount(raw, default=DEFAULT_AMOUNT):
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(1, min(value, MAX_AMOUNT))


def _parse_int(raw):
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _parse_bool(raw):
    if raw is None or raw == "":
        return None
    return raw.strip().lower() in ("1", "true", "yes", "y")


# ============================================================
# CORE: filter + rank + paginate
# ============================================================

def get_jobs(params):
    """params: dict of single string values (already flattened from
    parse_qs). Returns (response_dict)."""

    q_raw = (params.get("q") or "").strip()
    if not q_raw:
        raise ValueError("Missing required parameter 'q' - a search query is required.")

    exp_raw = params.get("exp") or params.get("exp_lvl") or params.get("experience") or params.get("level")

    filters = {
        "q": q_raw,
        "company": _split_csv(params.get("company")),
        "exp": [e for e in _split_csv(exp_raw) if e in VALID_EXP_LEVELS],
        "country": _split_csv(params.get("country")),
        "remote": _parse_bool(params.get("remote")),
        "days": _parse_int(params.get("days")),
    }

    amount = _parse_amount(params.get("amount") or params.get("limit"))
    offset = max(0, _parse_int(params.get("offset")) or 0)

    jobs = load_jobs()
    matched = [job for job in jobs if _matches_filters(job, filters)]

    if filters["q"]:
        query_lower = filters["q"].lower()
        query_tokens = set(re.findall(r"[a-z0-9]+", query_lower))
        matched.sort(
            key=lambda job: (
                -_relevance_score(job, query_tokens, query_lower),
                _normalize_days(job.get("date")),
            )
        )
    else:
        matched.sort(key=lambda job: _normalize_days(job.get("date")))

    page = matched[offset: offset + amount]

    return {
        "filters": {
            "q": filters["q"],
            "company": filters["company"] or None,
            "exp": filters["exp"] or None,
            "country": filters["country"] or None,
            "remote": filters["remote"],
            "days": filters["days"],
            "amount": amount,
            "offset": offset,
        },
        "total_matched": len(matched),
        "count": len(page),
        "jobs": page,
    }


# ============================================================
# VERCEL HANDLER
# ============================================================

class handler(BaseHTTPRequestHandler):

    def _send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        try:
            parsed = urlparse(self.path)
            raw_params = parse_qs(parsed.query)
            params = {k: v[0] for k, v in raw_params.items() if v}

            response = get_jobs(params)
            self._send_json(200, response)

        except ValueError as e:
            self._send_json(400, {"error": str(e)})

        except Exception as e:  # noqa: BLE001 - always want JSON back, never a raw 500 page
            self._send_json(500, {"error": str(e)})
