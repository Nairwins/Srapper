import requests
import json
import time
import os
import re
import glob
import gzip
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from rich.live import Live
from rich.table import Table
from rich.console import Console


# ============================================================
# CONFIG
# ============================================================

TENANTS_FILE = "data/tenants.json"
PAGE_SIZE = 20
OUTPUT_DIR = "output"
MAX_WORKERS = 8

# Once this many jobs have accumulated across finished tenants,
# merge them into one gzip-compressed chunk.
CHUNK_JOB_CAP = 50_000


# ============================================================
# TERMINAL STATUS
# ============================================================

console = Console()


class StatusDisplay:

    def __init__(self, total_tenants):
        self.total_tenants = total_tenants
        self.lock = threading.Lock()

        # Only currently running workers are stored here.
        self.tenants = {}

        self.completed = 0
        self.failed = 0
        self.total_jobs = 0

        self._live = Live(
            self._render(),
            console=console,
            refresh_per_second=4,
        )

    # --------------------------------------------------------
    # START / STOP DISPLAY
    # --------------------------------------------------------

    def start(self):
        self._live.start()

    def stop(self):
        with self.lock:
            table = self._render()

        self._live.update(
            table,
            refresh=True
        )

        self._live.stop()
        print()

    # --------------------------------------------------------
    # UPDATE ACTIVE TENANT
    # --------------------------------------------------------

    def update(
        self,
        tenant_key,
        tenant,
        wd,
        site,
        collected=0,
        total=None,
        offset=0,
        state="RUNNING",
        error=None,
    ):
        with self.lock:

            self.tenants[tenant_key] = {
                "tenant": tenant,
                "wd": wd,
                "site": site,
                "collected": collected,
                "total": total,
                "offset": offset,
                "state": state,
                "error": error,
            }

            table = self._render()

        self._live.update(
            table,
            refresh=True
        )

    # --------------------------------------------------------
    # FINISH TENANT
    # --------------------------------------------------------

    def finish(
        self,
        tenant_key,
        count,
        failed=False,
    ):
        with self.lock:

            # Remove from active worker display.
            self.tenants.pop(
                tenant_key,
                None
            )

            self.completed += 1

            if failed:
                self.failed += 1

            self.total_jobs += count

            table = self._render()

        self._live.update(
            table,
            refresh=True
        )

    # --------------------------------------------------------
    # BUILD TABLE
    # --------------------------------------------------------

    def _render(self):

        table = Table(
            title="WORKDAY SCRAPER",
            caption=(
                f"Workers: {MAX_WORKERS}    "
                f"Active: {len(self.tenants)}/{MAX_WORKERS}    "
                f"Completed: {self.completed}/{self.total_tenants}    "
                f"Failed: {self.failed}    "
                f"Jobs: {self.total_jobs}"
            ),
            expand=True,
        )

        table.add_column(
            "Tenant",
            style="cyan",
            no_wrap=True,
            width=20
        )

        table.add_column(
            "Progress",
            width=45
        )

        table.add_column(
            "Offset",
            justify="right"
        )

        items = list(
            self.tenants.values()
        )

        for item in items:

            tenant = item["tenant"]
            collected = item["collected"]
            total = item["total"]

            # ------------------------------------------------
            # PROGRESS BAR
            # ------------------------------------------------

            if total:

                percentage = min(
                    100,
                    int(
                        (collected / total) * 100
                    )
                )

                bar_width = 30

                filled = int(
                    bar_width * percentage / 100
                )

                bar = (
                    "█" * filled
                    +
                    "░" * (
                        bar_width - filled
                    )
                )

                progress = (
                    f"{bar} "
                    f"{collected:,}/{total:,}"
                )

            else:

                progress = (
                    f"{'░' * 30} "
                    f"{collected:,}/?"
                )

            table.add_row(
                tenant,
                progress,
                str(item["offset"]),
            )

        if not items:

            table.add_row(
                "—",
                "no active workers",
                "—"
            )

        return table


# ============================================================
# SESSION
# ============================================================

session = requests.Session()

session.headers.update({
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json",
    "Content-Type": "application/json",
})

adapter = requests.adapters.HTTPAdapter(
    pool_connections=MAX_WORKERS,
    pool_maxsize=MAX_WORKERS,
)

session.mount(
    "https://",
    adapter
)

session.mount(
    "http://",
    adapter
)


# ============================================================
# LOAD TENANTS
# ============================================================

def load_tenants():

    with open(
        TENANTS_FILE,
        "r",
        encoding="utf-8"
    ) as f:

        raw_list = json.load(f)

    tenants = []

    for entry in raw_list:

        parts = entry.split("|")

        if len(parts) != 3:

            print(
                f"SKIPPING malformed entry: {entry}"
            )

            continue

        tenant, wd, site = parts

        tenants.append({
            "tenant": tenant.strip(),
            "wd": wd.strip(),
            "site": site.strip(),
        })

    return tenants


# ============================================================
# BUILD URL
# ============================================================

def build_url(
    tenant,
    wd,
    site
):

    return (
        f"https://{tenant}.{wd}.myworkdayjobs.com"
        f"/wday/cxs/{tenant}/{site}/jobs"
    )


# ============================================================
# FETCH ONE PAGE
# ============================================================

def fetch_page(
    url,
    offset
):

    payload = {
        "limit": PAGE_SIZE,
        "offset": offset,
        "searchText": ""
    }

    try:

        response = session.post(
            url,
            json=payload,
            timeout=30
        )

        if response.status_code != 200:

            return (
                None,
                f"HTTP {response.status_code}"
            )

        return (
            response.json(),
            None
        )

    except requests.RequestException as e:

        return (
            None,
            f"REQUEST ERROR: {e}"
        )

    except ValueError:

        return (
            None,
            "INVALID JSON RESPONSE"
        )


# ============================================================
# PARSE "POSTED ON" TEXT INTO DAYS AGO
# ============================================================

DAYS_AGO_RE = re.compile(
    r"(\d+)(\+)?\s*Day",
    re.IGNORECASE
)


def parse_posted_days_ago(posted_on):

    if not posted_on:
        return None

    text = posted_on.strip().lower()

    if "today" in text:
        return 0

    if "yesterday" in text:
        return 1

    match = DAYS_AGO_RE.search(
        posted_on
    )

    if match:

        number = match.group(1)

        has_plus = (
            match.group(2) is not None
        )

        return (
            f"{number}+"
            if has_plus
            else int(number)
        )

    return None


# ============================================================
# SAVE JSON
# ============================================================

def save_jobs(
    jobs,
    output_file
):

    with open(
        output_file,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            jobs,
            f,
            indent=4,
            ensure_ascii=False
        )


# ============================================================
# CHUNKED MERGE + GZIP
# ============================================================

def flush_chunk(
    files,
    chunk_index,
    output_dir=OUTPUT_DIR
):

    if not files:
        return 0

    all_jobs = []
    readable_files = []
    bad_files = []

    # --------------------------------------------------------
    # READ FINISHED TENANT FILES
    # --------------------------------------------------------

    for path in files:

        try:

            with open(
                path,
                "r",
                encoding="utf-8"
            ) as f:

                jobs = json.load(f)

            if isinstance(jobs, list):

                all_jobs.extend(jobs)
                readable_files.append(path)

            else:

                bad_files.append(
                    (
                        path,
                        "not a JSON list"
                    )
                )

        except (
            OSError,
            ValueError
        ) as e:

            bad_files.append(
                (
                    path,
                    str(e)
                )
            )

    # --------------------------------------------------------
    # NOTHING VALID TO WRITE
    # --------------------------------------------------------

    if not all_jobs:

        for path in readable_files:

            try:
                os.remove(path)
            except OSError:
                pass

        return 0

    # --------------------------------------------------------
    # CHUNK PATH
    # --------------------------------------------------------

    chunk_path = os.path.join(
        output_dir,
        f"chunk_{chunk_index:04d}.json.gz"
    )

    # --------------------------------------------------------
    # WRITE COMPRESSED JSON
    # --------------------------------------------------------

    with gzip.open(
        chunk_path,
        "wt",
        encoding="utf-8"
    ) as f:

        json.dump(
            all_jobs,
            f,
            ensure_ascii=False,
            separators=(",", ":"),
        )

    # --------------------------------------------------------
    # SIZE INFORMATION
    # --------------------------------------------------------

    raw_size = sum(
        os.path.getsize(path)
        for path in readable_files
        if os.path.exists(path)
    )

    compressed_size = os.path.getsize(
        chunk_path
    )

    savings = (
        100 * (
            1 - compressed_size / raw_size
        )
        if raw_size
        else 0
    )

    print(
        f"[chunk {chunk_index}] "
        f"{len(readable_files)} tenant file(s), "
        f"{len(all_jobs):,} jobs -> "
        f"{chunk_path} "
        f"({raw_size:,} -> "
        f"{compressed_size:,} bytes, "
        f"{savings:.1f}% smaller)"
    )

    # --------------------------------------------------------
    # WARN ABOUT BAD FILES
    # --------------------------------------------------------

    if bad_files:

        print(
            f"  WARNING: skipped "
            f"{len(bad_files)} unreadable file(s):"
        )

        for path, reason in bad_files:

            print(
                f"    {path}: {reason}"
            )

    # --------------------------------------------------------
    # DELETE RAW FILES THAT WERE SUCCESSFULLY READ
    # --------------------------------------------------------

    for path in readable_files:

        try:
            os.remove(path)
        except OSError:
            pass

    return len(all_jobs)


# ============================================================
# MERGE LEFTOVER TENANT FILES
# ============================================================

def merge_leftover_files(
    output_dir=OUTPUT_DIR,
    chunk_job_cap=CHUNK_JOB_CAP
):

    pattern = os.path.join(
        output_dir,
        "*_jobs.json"
    )

    leftover_files = sorted(
        glob.glob(pattern)
    )

    if not leftover_files:

        print(
            "No leftover '*_jobs.json' files found."
        )

        return

    # --------------------------------------------------------
    # CONTINUE CHUNK NUMBERING
    # --------------------------------------------------------

    existing_chunks = glob.glob(
        os.path.join(
            output_dir,
            "chunk_*.json.gz"
        )
    )

    if existing_chunks:

        indexes = []

        for path in existing_chunks:

            match = re.search(
                r"chunk_(\d+)\.json\.gz$",
                os.path.basename(path)
            )

            if match:
                indexes.append(
                    int(match.group(1))
                )

        next_index = (
            max(indexes) + 1
            if indexes
            else 1
        )

    else:

        next_index = 1

    # --------------------------------------------------------
    # ACCUMULATE FILES
    # --------------------------------------------------------

    pending_files = []
    pending_jobs = 0

    for path in leftover_files:

        try:

            with open(
                path,
                "r",
                encoding="utf-8"
            ) as f:

                jobs = json.load(f)

            count = (
                len(jobs)
                if isinstance(jobs, list)
                else 0
            )

        except (
            OSError,
            ValueError
        ):

            count = 0

        pending_files.append(path)
        pending_jobs += count

        if pending_jobs >= chunk_job_cap:

            flush_chunk(
                pending_files,
                next_index,
                output_dir
            )

            next_index += 1
            pending_files = []
            pending_jobs = 0

    # --------------------------------------------------------
    # FLUSH REMAINING
    # --------------------------------------------------------

    if pending_files:

        flush_chunk(
            pending_files,
            next_index,
            output_dir
        )


# ============================================================
# SCRAPE ONE TENANT
# ============================================================

def scrape_tenant(
    tenant,
    wd,
    site,
    display
):

    tenant_key = (
        f"{tenant}|{wd}|{site}"
    )

    url = build_url(
        tenant,
        wd,
        site
    )

    output_file = os.path.join(
        OUTPUT_DIR,
        f"{tenant}_jobs.json"
    )

    # --------------------------------------------------------
    # INITIAL STATUS
    # --------------------------------------------------------

    display.update(
        tenant_key,
        tenant,
        wd,
        site,
        collected=0,
        total=None,
        offset=0,
        state="RUNNING"
    )

    all_jobs = []

    offset = 0
    total_jobs = None

    while True:

        data, error = fetch_page(
            url,
            offset
        )

        # ----------------------------------------------------
        # REQUEST FAILED
        # ----------------------------------------------------

        if data is None:

            display.finish(
                tenant_key,
                len(all_jobs),
                failed=True
            )

            return len(all_jobs)

        # ----------------------------------------------------
        # GET JOBS
        # ----------------------------------------------------

        jobs = data.get(
            "jobPostings",
            []
        )

        # ----------------------------------------------------
        # NO MORE JOBS
        # ----------------------------------------------------

        if not jobs:
            break

        # ----------------------------------------------------
        # TOTAL JOB COUNT
        # ----------------------------------------------------

        if total_jobs is None:

            total_jobs = data.get(
                "total"
            )

        # ----------------------------------------------------
        # PROCESS JOBS
        # ----------------------------------------------------

        for job in jobs:

            external_path = job.get(
                "externalPath"
            )

            # Full Workday job URL.
            job_url = (
                f"https://{tenant}.{wd}.myworkdayjobs.com"
                f"/{site}"
                f"{external_path}"
                if external_path
                else None
            )

            locations = job.get(
                "locationsText",
                job.get(
                    "bulletFields",
                    [None]
                )[0]
            )

            # Infer remote from location text.
            remote = bool(
                locations
                and
                "remote" in locations.lower()
            )

            # Parse fuzzy Workday date.
            posted_days = parse_posted_days_ago(
                job.get("postedOn")
            )

            # ------------------------------------------------
            # COMPACT JOB OBJECT
            # ------------------------------------------------

            clean_job = {
                "company": tenant,
                "title": job.get("title"),
                "locations": locations,
                "url": job_url,
            }

            # Only store date when it exists.
            if posted_days is not None:

                clean_job["date"] = posted_days

            # Only store remote when true.
            if remote:

                clean_job["remote"] = True

            all_jobs.append(
                clean_job
            )

        # ----------------------------------------------------
        # SAVE PROGRESS
        # ----------------------------------------------------

        save_jobs(
            all_jobs,
            output_file
        )

        # ----------------------------------------------------
        # UPDATE ACTIVE WORKER
        # ----------------------------------------------------

        display.update(
            tenant_key,
            tenant,
            wd,
            site,
            collected=len(all_jobs),
            total=total_jobs,
            offset=offset,
            state="RUNNING"
        )

        # ----------------------------------------------------
        # STOP WHEN EVERYTHING IS COLLECTED
        # ----------------------------------------------------

        if (
            total_jobs
            and
            len(all_jobs) >= total_jobs
        ):

            break

        # ----------------------------------------------------
        # NEXT PAGE
        # ----------------------------------------------------

        offset += PAGE_SIZE

        time.sleep(0.5)

    # --------------------------------------------------------
    # FINISHED
    # --------------------------------------------------------

    display.finish(
        tenant_key,
        len(all_jobs),
        failed=False
    )

    return len(all_jobs)


# ============================================================
# MAIN
# ============================================================

def main():

    os.makedirs(
        OUTPUT_DIR,
        exist_ok=True
    )

    tenants = load_tenants()

    if not tenants:

        print(
            f"No valid tenants found in "
            f"{TENANTS_FILE}"
        )

        return

    # ========================================================
    # START DASHBOARD
    # ========================================================

    display = StatusDisplay(
        len(tenants)
    )

    display.start()

    summary = {}

    # ========================================================
    # PARALLEL SCRAPING
    # ========================================================

    try:

        with ThreadPoolExecutor(
            max_workers=MAX_WORKERS
        ) as executor:

            futures = {}

            for entry in tenants:

                tenant = entry["tenant"]
                wd = entry["wd"]
                site = entry["site"]

                future = executor.submit(
                    scrape_tenant,
                    tenant,
                    wd,
                    site,
                    display
                )

                futures[future] = (
                    tenant,
                    wd,
                    site
                )

            # ------------------------------------------------
            # CHUNKING STATE
            # ------------------------------------------------

            chunk_index = 1

            # Continue after existing chunks if present.
            existing_chunks = glob.glob(
                os.path.join(
                    OUTPUT_DIR,
                    "chunk_*.json.gz"
                )
            )

            if existing_chunks:

                indexes = []

                for path in existing_chunks:

                    match = re.search(
                        r"chunk_(\d+)\.json\.gz$",
                        os.path.basename(path)
                    )

                    if match:

                        indexes.append(
                            int(match.group(1))
                        )

                if indexes:

                    chunk_index = (
                        max(indexes) + 1
                    )

            pending_files = []
            pending_job_count = 0

            # ------------------------------------------------
            # PROCESS FINISHED TENANTS
            # ------------------------------------------------

            for future in as_completed(
                futures
            ):

                tenant, wd, site = (
                    futures[future]
                )

                key = (
                    f"{tenant}|{wd}|{site}"
                )

                try:

                    count = future.result()

                    summary[key] = count

                except Exception:

                    display.finish(
                        key,
                        0,
                        failed=True
                    )

                    summary[key] = 0
                    count = 0

                # ------------------------------------------------
                # QUEUE FINISHED TENANT FILE
                # ------------------------------------------------

                output_file = os.path.join(
                    OUTPUT_DIR,
                    f"{tenant}_jobs.json"
                )

                if (
                    count > 0
                    and
                    os.path.exists(output_file)
                ):

                    pending_files.append(
                        output_file
                    )

                    pending_job_count += count

                # ------------------------------------------------
                # CHUNK CAP REACHED
                # ------------------------------------------------

                if (
                    pending_job_count
                    >= CHUNK_JOB_CAP
                ):

                    flush_chunk(
                        pending_files,
                        chunk_index
                    )

                    chunk_index += 1

                    pending_files = []
                    pending_job_count = 0

            # ------------------------------------------------
            # FLUSH REMAINING TENANTS
            # ------------------------------------------------

            if pending_files:

                flush_chunk(
                    pending_files,
                    chunk_index
                )

    finally:

        display.stop()

    # ========================================================
    # FINAL SUMMARY
    # ========================================================

    print()
    print("=" * 90)
    print("ALL TENANTS FINISHED")
    print("=" * 90)

    for key, count in summary.items():

        tenant, wd, site = key.split(
            "|",
            2
        )

        print(
            f"{tenant:<20} "
            f"{site:<30} "
            f"{count:>6} jobs"
        )

    print("=" * 90)

    print(
        f"Total jobs: {sum(summary.values())}"
    )

    print(
        f"Tenants:    {len(summary)}"
    )

    print("=" * 90)

    # --------------------------------------------------------
    # OUTPUT INFO
    # --------------------------------------------------------

    chunk_count = len(
        glob.glob(
            os.path.join(
                OUTPUT_DIR,
                "chunk_*.json.gz"
            )
        )
    )

    loose_count = len(
        glob.glob(
            os.path.join(
                OUTPUT_DIR,
                "*_jobs.json"
            )
        )
    )

    print(
        f"Chunks:     {chunk_count}"
    )

    print(
        f"Loose files: {loose_count}"
    )

    print("=" * 90)


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":
    main()