import requests
import json
import time
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock


# ============================================================
# CONFIG
# ============================================================

TENANTS_FILE = "data/tenants.json"
BACKUP_FILE = "data/tenants.json.bak"
BROKEN_FILE = "data/broken_tenants.json"

TIMEOUT = 10
RETRIES = 2            # extra attempts before declaring a tenant dead
RETRY_DELAY = 1         # seconds between retries
MAX_WORKERS = 200        # tenants checked in parallel

print_lock = Lock()


# ============================================================
# SESSION
# ============================================================

session = requests.Session()

session.headers.update({
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json",
    "Content-Type": "application/json",
})

# Give the session enough pooled connections for parallel workers
adapter = requests.adapters.HTTPAdapter(
    pool_connections=MAX_WORKERS,
    pool_maxsize=MAX_WORKERS,
)
session.mount("https://", adapter)
session.mount("http://", adapter)


# ============================================================
# LOAD TENANTS
# ============================================================

def load_tenants():
    """
    Reads data/tenants.json, expects a list of strings like:
    "tenant|wd|site" e.g. "nvidia|wd5|nvidiaexternalcareersite"
    Returns the raw list of strings (kept as-is so we can write
    back the exact same format).
    """

    with open(TENANTS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def parse_entry(entry):

    parts = entry.split("|")

    if len(parts) != 3:
        return None

    tenant, wd, site = parts

    return {
        "tenant": tenant.strip(),
        "wd": wd.strip(),
        "site": site.strip(),
    }


# ============================================================
# BUILD URL
# ============================================================

def build_url(tenant, wd, site):
    return (
        f"https://{tenant}.{wd}.myworkdayjobs.com"
        f"/wday/cxs/{tenant}/{site}/jobs"
    )


# ============================================================
# CHECK ONE TENANT
# ============================================================

def check_tenant(tenant, wd, site):
    """
    Returns (is_alive: bool, reason: str)
    A tenant is considered alive only if Workday returns HTTP 200
    with valid JSON that actually looks like a job search response.
    """

    url = build_url(tenant, wd, site)

    payload = {
        "limit": 1,
        "offset": 0,
        "searchText": ""
    }

    last_reason = "unknown error"

    for attempt in range(1, RETRIES + 2):  # 1 initial + RETRIES

        try:

            response = session.post(
                url,
                json=payload,
                timeout=TIMEOUT
            )

            if response.status_code == 404:
                return False, "HTTP 404 (tenant/site not found)"

            if response.status_code != 200:
                last_reason = f"HTTP {response.status_code}"

            else:

                try:
                    data = response.json()
                except ValueError:
                    last_reason = "invalid JSON response"
                    data = None

                if data is not None:

                    # A healthy Workday jobs endpoint always
                    # includes these keys, even with 0 results.
                    if "jobPostings" in data and "total" in data:
                        return True, "OK"

                    last_reason = "unexpected response shape"

        except requests.exceptions.Timeout:
            last_reason = "timeout"

        except requests.exceptions.ConnectionError:
            last_reason = "connection error (DNS/refused)"

        except requests.RequestException as e:
            last_reason = f"request error: {e}"

        if attempt <= RETRIES:
            time.sleep(RETRY_DELAY)

    return False, last_reason


# ============================================================
# MAIN
# ============================================================

def main():

    raw_entries = load_tenants()

    print("=" * 60)
    print(f"HEALTH CHECK: {len(raw_entries)} tenants (parallel, {MAX_WORKERS} workers)")
    print("=" * 60)

    results = {}  # entry -> (is_alive, reason) or None for malformed

    def worker(entry):

        parsed = parse_entry(entry)

        if not parsed:
            return entry, None, "malformed entry"

        tenant, wd, site = parsed["tenant"], parsed["wd"], parsed["site"]

        is_alive, reason = check_tenant(tenant, wd, site)

        with print_lock:
            if is_alive:
                print(f"[OK]     {tenant:<20} ({wd}/{site})")
            else:
                print(f"[BROKEN] {tenant:<20} ({wd}/{site}) -> {reason}")

        return entry, is_alive, reason

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:

        futures = [executor.submit(worker, entry) for entry in raw_entries]

        for future in as_completed(futures):

            entry, is_alive, reason = future.result()
            results[entry] = (is_alive, reason)

    # ============================================================
    # SPLIT RESULTS (preserving original order)
    # ============================================================

    alive_entries = []
    broken_entries = []

    for entry in raw_entries:

        is_alive, reason = results[entry]

        if is_alive:
            alive_entries.append(entry)
        else:
            broken_entries.append({"entry": entry, "reason": reason})

    # ============================================================
    # BACK UP ORIGINAL FILE BEFORE OVERWRITING
    # ============================================================

    shutil.copy(TENANTS_FILE, BACKUP_FILE)

    # ============================================================
    # WRITE CLEANED TENANTS LIST
    # ============================================================

    with open(TENANTS_FILE, "w", encoding="utf-8") as f:
        json.dump(alive_entries, f, indent=2, ensure_ascii=False)

    # ============================================================
    # WRITE BROKEN TENANTS LOG
    # ============================================================

    with open(BROKEN_FILE, "w", encoding="utf-8") as f:
        json.dump(broken_entries, f, indent=2, ensure_ascii=False)

    # ============================================================
    # SUMMARY
    # ============================================================

    print()
    print("=" * 60)
    print("HEALTH CHECK FINISHED")
    print("=" * 60)
    print(f"Alive         : {len(alive_entries)}")
    print(f"Broken/removed: {len(broken_entries)}")
    print(f"Backup saved  : {BACKUP_FILE}")
    print(f"Updated file  : {TENANTS_FILE}")
    print(f"Broken log    : {BROKEN_FILE}")
    print("=" * 60)


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":
    main()