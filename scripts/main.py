"""
Jobs API
========

Serves job listings scraped by scrap.py through a filterable JSON API.

On startup, it loads every chunk_*.json.gz (and any loose *.json files
containing a list of jobs) found in DATA_DIR into memory once. Every
request then filters that in-memory list — no re-reading files, no
database needed for this data size.

Run locally:
    uvicorn main:app --reload --port 8000

Env vars:
    DATA_DIR   Folder containing chunk_*.json.gz / *.json files (default: "output")
"""

import gzip
import json
import os
from glob import glob
from typing import Optional

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware

DATA_DIR = os.environ.get("DATA_DIR", "output")

app = FastAPI(title="Jobs API", version="1.0")

# Allow browser-based clients (e.g. a hosted version of your index.html)
# to call this API from any origin. Tighten this to your real domain
# once you know it.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory store, populated once on startup.
JOBS: list[dict] = []


def _load_json_list(path: str) -> list[dict]:
    """Read a plain .json file containing a list of job dicts."""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, list) else []


def _load_gzip_json_list(path: str) -> list[dict]:
    """Read a gzip-compressed .json.gz file containing a list of job dicts."""
    with gzip.open(path, "rt", encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, list) else []


def load_all_jobs() -> list[dict]:
    jobs: list[dict] = []

    chunk_paths = sorted(glob(os.path.join(DATA_DIR, "chunk_*.json.gz")))
    loose_paths = sorted(glob(os.path.join(DATA_DIR, "*.json")))

    for path in chunk_paths:
        try:
            jobs.extend(_load_gzip_json_list(path))
        except (OSError, ValueError, gzip.BadGzipFile) as e:
            print(f"WARNING: could not read {path}: {e}")

    for path in loose_paths:
        try:
            jobs.extend(_load_json_list(path))
        except (OSError, ValueError) as e:
            print(f"WARNING: could not read {path}: {e}")

    return jobs


@app.on_event("startup")
def startup_load_data():
    global JOBS
    JOBS = load_all_jobs()
    print(f"Loaded {len(JOBS):,} jobs from '{DATA_DIR}'")


@app.get("/health")
def health():
    return {"status": "ok", "jobs_loaded": len(JOBS)}


@app.get("/companies")
def list_companies():
    """Distinct company names currently in memory, useful for building filter UIs."""
    companies = sorted({job.get("company", "") for job in JOBS if job.get("company")})
    return {"count": len(companies), "companies": companies}


@app.get("/jobs")
def get_jobs(
    q: Optional[str] = Query(None, description="Search text, matched against job title"),
    company: Optional[str] = Query(None, description="Filter by company (substring, case-insensitive)"),
    location: Optional[str] = Query(None, description="Filter by location (substring, case-insensitive)"),
    remote: Optional[bool] = Query(None, description="Filter to remote-only (true) or non-remote (false)"),
    max_days: Optional[int] = Query(None, description="Only jobs posted within this many days"),
    limit: int = Query(50, ge=1, le=500, description="Max results to return"),
    offset: int = Query(0, ge=0, description="Pagination offset"),
):
    results = JOBS

    if q:
        needle = q.lower()
        results = [j for j in results if needle in (j.get("title") or "").lower()]

    if company:
        needle = company.lower()
        results = [j for j in results if needle in (j.get("company") or "").lower()]

    if location:
        needle = location.lower()
        results = [
            j for j in results
            if any(needle in loc.lower() for loc in (j.get("locations") or []))
        ]

    if remote is not None:
        results = [j for j in results if bool(j.get("remote", False)) == remote]

    if max_days is not None:
        results = [
            j for j in results
            if j.get("date") is not None and j.get("date") <= max_days
        ]

    total = len(results)
    page = results[offset: offset + limit]

    return {
        "total": total,
        "count": len(page),
        "limit": limit,
        "offset": offset,
        "jobs": page,
    }


@app.get("/")
def root():
    return {
        "message": "Jobs API is running.",
        "jobs_loaded": len(JOBS),
        "endpoints": ["/jobs", "/companies", "/health"],
    }
