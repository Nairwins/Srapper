"""
Quick test script for the /api/jobs endpoint.

Usage:
    pip install requests
    python test_api.py
    python test_api.py --q "backend engineer" --exp mid,senior --days 30
    python test_api.py --url http://localhost:3000 --company netflix
    python test_api.py --country usa,germany --remote true --amount 5

Everything routes to main.py (see vercel.json), so any path on the
deployed domain works - the default just hits the root.

Run `vercel dev` locally first if you want to hit localhost instead of
the deployed URL.
"""

import argparse
import json
import sys

import requests

DEFAULT_URL = "https://srapper-bay.vercel.app"


def main():
    parser = argparse.ArgumentParser(description="Test the /api/jobs endpoint")
    parser.add_argument("--url", default=DEFAULT_URL, help="Full endpoint URL")
    parser.add_argument("--q", help="Free-text search query")
    parser.add_argument("--company", help="Comma-separated company filter")
    parser.add_argument("--exp", help="Comma-separated: intern,junior,mid,senior")
    parser.add_argument("--country", help="Comma-separated country filter")
    parser.add_argument("--remote", choices=["true", "false"], help="Filter by remote status")
    parser.add_argument("--days", type=int, help="Only jobs posted within N days")
    parser.add_argument("--amount", type=int, default=20, help="Max jobs to return (default 20, capped at 100)")
    parser.add_argument("--offset", type=int, help="Skip this many matches before taking --amount")
    parser.add_argument("--raw", action="store_true", help="Print raw JSON instead of a summary table")

    args = parser.parse_args()

    params = {
        "q": args.q,
        "company": args.company,
        "exp": args.exp,
        "country": args.country,
        "remote": args.remote,
        "days": args.days,
        "amount": args.amount,
        "offset": args.offset,
    }
    params = {k: v for k, v in params.items() if v is not None}

    print(f"GET {args.url}")
    print(f"params: {params}\n")

    try:
        resp = requests.get(args.url, params=params, timeout=30)
    except requests.RequestException as e:
        print(f"Request failed: {e}")
        sys.exit(1)

    print(f"status: {resp.status_code}")

    try:
        data = resp.json()
    except ValueError:
        print("Response wasn't JSON:")
        print(resp.text[:2000])
        sys.exit(1)

    if resp.status_code != 200:
        print(json.dumps(data, indent=2))
        sys.exit(1)

    if args.raw:
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return

    print(f"filters applied: {json.dumps(data.get('filters'), ensure_ascii=False)}")
    print(f"total matched:   {data.get('total_matched')}")
    print(f"returned:        {data.get('count')}\n")

    for i, job in enumerate(data.get("jobs", []), start=1):
        title = job.get("title") or "?"
        company = job.get("company") or "?"
        level = job.get("experience_level") or "?"
        remote = "remote" if job.get("remote") else "on-site"
        date = job.get("date")
        locations = ", ".join(
            (loc.get("city") + ", " if loc.get("city") else "") + (loc.get("country") or "")
            for loc in (job.get("locations") or [])
        ) or "-"

        print(f"{i:>2}. {title}  @  {company}")
        print(f"    {level} | {remote} | {date}d ago | {locations}")
        print(f"    {job.get('url')}")

    if not data.get("jobs"):
        print("(no jobs matched - check your filters, or that output/ actually has data deployed)")


if __name__ == "__main__":
    main()
