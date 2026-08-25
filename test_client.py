"""
Test client for the Jobs API.

Run the API first (locally or hosted), then run this script:
    python test_client.py --url http://localhost:8000

It hits every endpoint and prints results plus basic pass/fail checks,
so you can confirm the API is actually working end to end.
"""

import argparse
import sys

import requests


def check(label: str, condition: bool):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}")
    return condition


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://localhost:8000", help="Base URL of the running API")
    args = parser.parse_args()

    base = args.url.rstrip("/")
    all_passed = True

    # ------------------------------------------------------------
    # 1. Health check
    # ------------------------------------------------------------
    print("\n== /health ==")
    r = requests.get(f"{base}/health", timeout=10)
    print(r.json())
    all_passed &= check("health endpoint returns 200", r.status_code == 200)
    all_passed &= check("jobs_loaded > 0", r.json().get("jobs_loaded", 0) > 0)

    # ------------------------------------------------------------
    # 2. Companies list
    # ------------------------------------------------------------
    print("\n== /companies ==")
    r = requests.get(f"{base}/companies", timeout=10)
    companies_data = r.json()
    print(f"{companies_data['count']} companies: {companies_data['companies']}")
    all_passed &= check("companies endpoint returns 200", r.status_code == 200)
    all_passed &= check("at least one company found", companies_data["count"] > 0)

    # ------------------------------------------------------------
    # 3. Basic /jobs, no filters
    # ------------------------------------------------------------
    print("\n== /jobs (no filters) ==")
    r = requests.get(f"{base}/jobs", timeout=10)
    data = r.json()
    print(f"total={data['total']} count={data['count']}")
    all_passed &= check("jobs endpoint returns 200", r.status_code == 200)
    all_passed &= check("returns some jobs", data["count"] > 0)

    # ------------------------------------------------------------
    # 4. Filter by company (use whatever the first discovered company is)
    # ------------------------------------------------------------
    if companies_data["companies"]:
        target_company = companies_data["companies"][0]
        print(f"\n== /jobs?company={target_company} ==")
        r = requests.get(f"{base}/jobs", params={"company": target_company}, timeout=10)
        data = r.json()
        print(f"total={data['total']} count={data['count']}")
        all_ok = all(target_company.lower() in j["company"].lower() for j in data["jobs"])
        all_passed &= check(f"all returned jobs belong to '{target_company}'", all_ok)

    # ------------------------------------------------------------
    # 5. Filter by title search text
    # ------------------------------------------------------------
    print("\n== /jobs?q=engineer ==")
    r = requests.get(f"{base}/jobs", params={"q": "engineer"}, timeout=10)
    data = r.json()
    print(f"total={data['total']} count={data['count']}")
    all_ok = all("engineer" in j["title"].lower() for j in data["jobs"])
    all_passed &= check("all returned jobs have 'engineer' in title", all_ok)

    # ------------------------------------------------------------
    # 6. Filter by remote
    # ------------------------------------------------------------
    print("\n== /jobs?remote=true ==")
    r = requests.get(f"{base}/jobs", params={"remote": "true"}, timeout=10)
    data = r.json()
    print(f"total={data['total']} count={data['count']}")
    all_ok = all(j.get("remote") is True for j in data["jobs"])
    all_passed &= check("all returned jobs are remote", all_ok)

    # ------------------------------------------------------------
    # 7. Pagination
    # ------------------------------------------------------------
    print("\n== /jobs?limit=5&offset=0 then offset=5 ==")
    r1 = requests.get(f"{base}/jobs", params={"limit": 5, "offset": 0}, timeout=10).json()
    r2 = requests.get(f"{base}/jobs", params={"limit": 5, "offset": 5}, timeout=10).json()
    ids1 = [j["url"] for j in r1["jobs"]]
    ids2 = [j["url"] for j in r2["jobs"]]
    all_passed &= check("page 1 and page 2 don't overlap", not set(ids1) & set(ids2))

    # ------------------------------------------------------------
    # Result
    # ------------------------------------------------------------
    print("\n" + "=" * 40)
    if all_passed:
        print("ALL CHECKS PASSED")
    else:
        print("SOME CHECKS FAILED — see above")
        sys.exit(1)


if __name__ == "__main__":
    main()
