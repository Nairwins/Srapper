import glob
import gzip
import json
import os
import re
import threading
import time

from concurrent.futures import ThreadPoolExecutor, as_completed

from rich.console import Console
from rich.live import Live
from rich.table import Table

from scrappy import workdscrap
from scrappy import greenscrap
from scrappy import ashscrap
from scrappy import leverscrap
from scrappy import bambooscrap
from scrappy import workascrap
from scrappy import recruitscrap
from scrappy import personioscrap


# ============================================================
# CONFIG - change this to pick which scraper(s) run
# ============================================================

# Options: "workday", "greenhouse", "ashby", "lever", "bamboohr", "workable", "recruitee", "personio", or "all"
SCRAPER = "all"
SOURCE = "default"  # "default" = module's own tenants file, or path to a custom tenants JSON file

MAX_WORKERS = 20
OUTPUT_DIR = "output"

# Once this many jobs have accumulated across finished tenants,
# merge them into one gzip-compressed chunk.
CHUNK_JOB_CAP = 50_000
CHUNKING_ENABLED = True

# Be a decent citizen between pages/tenants.
REQUEST_DELAY = 0.2


# ============================================================
# TERMINAL STATUS (generic - works for any scraper module)
# ============================================================

console = Console()


class StatusDisplay:

    def __init__(self, total_tenants, title, job_limit=None):
        self.total_tenants = total_tenants
        self.title = title
        self.job_limit = job_limit
        self.lock = threading.Lock()

        # Only currently running workers are stored here.
        self.tenants = {}

        self.completed = 0
        self.failed = 0
        self.empty = 0
        self.total_jobs = 0

        self._live = Live(
            self._render(),
            console=console,
            refresh_per_second=4,
        )

    def start(self):
        self._live.start()

    def stop(self):
        with self.lock:
            table = self._render()

        self._live.update(table, refresh=True)
        self._live.stop()
        print()

    def update(self, key, label, collected=0, total=None, offset=0, state="RUNNING"):
        with self.lock:
            self.tenants[key] = {
                "label": label,
                "collected": collected,
                "total": total,
                "offset": offset,
                "state": state,
            }
            table = self._render()

        self._live.update(table)

    def finish(self, key, count, failed=False, empty=False):
        with self.lock:
            self.tenants.pop(key, None)
            self.completed += 1

            if failed:
                self.failed += 1

            if empty:
                self.empty += 1

            self.total_jobs += count
            table = self._render()

        self._live.update(table)

    def _render(self):
        table = Table(
            title=self.title,
            caption=(
                f"Workers: {MAX_WORKERS}    "
                f"Active: {len(self.tenants)}/{MAX_WORKERS}    "
                f"Completed: {self.completed}/{self.total_tenants}    "
                f"Failed: {self.failed}    "
                f"Empty: {self.empty}    "
                f"Jobs: {self.total_jobs}"
            ),
            expand=True,
        )

        table.add_column("Tenant", style="cyan", no_wrap=True, width=20)
        table.add_column("Progress", width=45)
        table.add_column("Offset", justify="right")

        items = list(self.tenants.values())

        for item in items:
            collected = item["collected"]
            total = item["total"]

            if total:
                display_total = min(total, self.job_limit) if self.job_limit else total
                percentage = min(100, int((collected / display_total) * 100))
                bar_width = 30
                filled = int(bar_width * percentage / 100)
                bar = "█" * filled + "░" * (bar_width - filled)
                progress = f"{bar} {collected:,}/{display_total:,}"
            else:
                progress = f"{'░' * 30} {collected:,}/?"

            table.add_row(item["label"], progress, str(item["offset"]))

        if not items:
            table.add_row("—", "no active workers", "—")

        return table


# ============================================================
# SAVE JSON
# ============================================================

def save_jobs(jobs, output_file):
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(jobs, f, indent=4, ensure_ascii=False)


def output_path(module, tenant_info, output_dir=OUTPUT_DIR):
    """Return a collision-free file path for one configured ATS site."""
    label = tenant_info["label"]
    wd = tenant_info.get("wd", "")
    site = tenant_info.get("site", "")
    safe_site = re.sub(r"[^A-Za-z0-9._-]+", "_", site)
    return os.path.join(output_dir, f"{module.NAME}_{label}_{wd}_{safe_site}_jobs.json")


# ============================================================
# CHUNKED MERGE + GZIP (generic - operates on *_jobs.json files
# regardless of which scraper produced them)
# ============================================================

def _next_chunk_index(output_dir=OUTPUT_DIR):
    existing_chunks = glob.glob(os.path.join(output_dir, "chunk_*.json.gz"))

    indexes = []
    for path in existing_chunks:
        match = re.search(r"chunk_(\d+)\.json\.gz$", os.path.basename(path))
        if match:
            indexes.append(int(match.group(1)))

    return max(indexes) + 1 if indexes else 1


def flush_chunk(files, chunk_index, output_dir=OUTPUT_DIR):
    if not files:
        return 0

    all_jobs = []
    readable_files = []
    bad_files = []

    for path in files:
        try:
            with open(path, "r", encoding="utf-8") as f:
                jobs = json.load(f)

            if isinstance(jobs, list):
                all_jobs.extend(jobs)
                readable_files.append(path)
            else:
                bad_files.append((path, "not a JSON list"))

        except (OSError, ValueError) as e:
            bad_files.append((path, str(e)))

    if not all_jobs:
        for path in readable_files:
            try:
                os.remove(path)
            except OSError:
                pass
        return 0

    chunk_path = os.path.join(output_dir, f"chunk_{chunk_index:04d}.json.gz")

    with gzip.open(chunk_path, "wt", encoding="utf-8") as f:
        json.dump(all_jobs, f, ensure_ascii=False, separators=(",", ":"))

    raw_size = sum(os.path.getsize(path) for path in readable_files if os.path.exists(path))
    compressed_size = os.path.getsize(chunk_path)
    savings = (100 * (1 - compressed_size / raw_size)) if raw_size else 0

    print(
        f"[chunk {chunk_index}] {len(readable_files)} tenant file(s), "
        f"{len(all_jobs):,} jobs -> {chunk_path} "
        f"({raw_size:,} -> {compressed_size:,} bytes, {savings:.1f}% smaller)"
    )

    if bad_files:
        print(f"  WARNING: skipped {len(bad_files)} unreadable file(s):")
        for path, reason in bad_files:
            print(f"    {path}: {reason}")

    for path in readable_files:
        try:
            os.remove(path)
        except OSError:
            pass

    return len(all_jobs)


def merge_company_files(module, tenants, output_dir=OUTPUT_DIR):
    """Merge per-site files into one deduplicated file per company label."""
    grouped = {}
    for tenant_info in tenants:
        path = output_path(module, tenant_info, output_dir)
        grouped.setdefault(tenant_info["label"], []).append(path)

    merged_files = []
    for label, paths in grouped.items():
        jobs_by_url = {}
        merged_path = os.path.join(output_dir, f"{module.NAME}_{label}_jobs.json")
        for path in paths:
            if not os.path.exists(path):
                # Expected for any tenant that ended up with 0 jobs -
                # scrape_tenant() deliberately removes/never-creates its
                # file in that case. Not worth a warning.
                continue

            try:
                with open(path, "r", encoding="utf-8") as f:
                    jobs = json.load(f)
            except (OSError, ValueError) as e:
                print(f"  WARNING: skipped {path}: {e}")
                continue

            if not isinstance(jobs, list):
                print(f"  WARNING: skipped {path}: not a JSON list")
                continue

            for job in jobs:
                key = job.get("url") if isinstance(job, dict) else None
                if key is None:
                    key = json.dumps(job, sort_keys=True, ensure_ascii=False)
                jobs_by_url.setdefault(key, job)

        if not jobs_by_url:
            try:
                os.remove(merged_path)
            except OSError:
                pass
            continue

        save_jobs(list(jobs_by_url.values()), merged_path)
        merged_files.append(merged_path)

        for path in paths:
            try:
                os.remove(path)
            except OSError:
                pass

    return merged_files


def chunk_files(files, chunk_index, output_dir=OUTPUT_DIR, chunk_job_cap=CHUNK_JOB_CAP):
    """Chunk whole company files, allowing an oversized company chunk."""
    pending_files = []
    pending_jobs = 0

    for path in files:
        try:
            with open(path, "r", encoding="utf-8") as f:
                job_count = len(json.load(f))
        except (OSError, ValueError):
            continue

        if pending_files and pending_jobs + job_count > chunk_job_cap:
            flush_chunk(pending_files, chunk_index, output_dir)
            chunk_index += 1
            pending_files = []
            pending_jobs = 0

        pending_files.append(path)
        pending_jobs += job_count

    if pending_files:
        flush_chunk(pending_files, chunk_index, output_dir)


def merge_leftover_files(output_dir=OUTPUT_DIR, chunk_job_cap=CHUNK_JOB_CAP):
    """Sweep up any *_jobs.json files not yet folded into a chunk.
    Handy as a final pass after running scrapers, or standalone."""
    if not CHUNKING_ENABLED:
        print("Chunking disabled (CHUNKING_ENABLED=False) - leaving loose '*_jobs.json' files as-is.")
        return

    leftover_files = sorted(glob.glob(os.path.join(output_dir, "*_jobs.json")))

    if not leftover_files:
        print("No leftover '*_jobs.json' files found.")
        return

    next_index = _next_chunk_index(output_dir)
    pending_files = []
    pending_jobs = 0

    for path in leftover_files:
        try:
            with open(path, "r", encoding="utf-8") as f:
                jobs = json.load(f)
            count = len(jobs) if isinstance(jobs, list) else 0
        except (OSError, ValueError):
            count = 0

        pending_files.append(path)
        pending_jobs += count

        if pending_jobs >= chunk_job_cap:
            flush_chunk(pending_files, next_index, output_dir)
            next_index += 1
            pending_files = []
            pending_jobs = 0

    if pending_files:
        flush_chunk(pending_files, next_index, output_dir)


# ============================================================
# SCRAPE ONE TENANT (generic - drives any module's fetch_jobs loop)
# ============================================================

def scrape_tenant(module, tenant_info, display, output_dir=OUTPUT_DIR):
    key = tenant_info["key"]
    label = tenant_info["label"]

    output_file = output_path(module, tenant_info, output_dir)

    display.update(key, label, collected=0, total=None, offset=0, state="RUNNING")

    all_jobs = []
    offset = 0
    known_total = None  # cache once known - a later page may omit/repeat it

    while True:
        result = module.fetch_jobs(tenant_info, offset)

        if result.get("error"):
            if all_jobs:
                save_jobs(all_jobs, output_file)
            display.finish(key, len(all_jobs), failed=True)
            return len(all_jobs)

        all_jobs.extend(result.get("jobs") or [])
        if all_jobs:
            save_jobs(all_jobs, output_file)

        if result.get("total"):
            known_total = result.get("total")

        display.update(
            key, label,
            collected=len(all_jobs),
            total=known_total,
            offset=offset,
            state="RUNNING",
        )

        if result.get("done"):
            break

        offset = result.get("next_offset", offset + len(result.get("jobs") or []))
        time.sleep(REQUEST_DELAY)

    if not all_jobs:
        # Nothing collected - either a genuinely empty board or a failure
        # with zero partial results. Don't leave a (possibly stale, from
        # a previous run) output file lying around for this tenant.
        try:
            if os.path.exists(output_file):
                os.remove(output_file)
        except OSError:
            pass

    display.finish(key, len(all_jobs), empty=(len(all_jobs) == 0))
    return len(all_jobs)


# ============================================================
# RUN ONE SCRAPER MODULE END-TO-END
# ============================================================

def run_scraper(module, tenants_file=None, output_dir=OUTPUT_DIR):
    """Run a scraper module (workdscrap or greenscrap) to completion:
    load its tenants, fan them out across a worker pool, show the live
    dashboard, chunk finished output, and print a summary."""

    # Prefer an explicit override, then the module's own declared tenants
    # file (e.g. data/ashby.json), and only fall back to the shared
    # data/tenants.json if the module hasn't been given its own file yet.
    # Loading every platform from the same shared file is what causes
    # each scraper to attempt companies that don't even belong to it.
    path = tenants_file or getattr(module, "TENANTS_FILE", None) or "data/tenants.json"

    if not os.path.exists(path):
        print(f"Tenants file not found: {path}")
        return

    tenants = module.load_tenants(path)

    if not tenants:
        print(f"No valid tenants found in {path}")
        return

    os.makedirs(output_dir, exist_ok=True)

    print(f"Loaded {len(tenants)} companies from {path}")
    print(f"Writing output to '{output_dir}/'\n")

    display = StatusDisplay(
        len(tenants),
        title=module.NAME,
        job_limit=getattr(module, "WORKDAY_JOB_LIMIT", None) or getattr(module, "JOB_LIMIT", None),
    )
    display.start()

    summary = {}

    try:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {
                executor.submit(scrape_tenant, module, tenant_info, display, output_dir): tenant_info
                for tenant_info in tenants
            }

            for future in as_completed(futures):
                tenant_info = futures[future]
                key = tenant_info["key"]
                label = tenant_info["label"]

                try:
                    count = future.result()
                except Exception:
                    display.finish(key, 0, failed=True)
                    count = 0

                summary[label] = summary.get(label, 0) + count

            merged_files = merge_company_files(module, tenants, output_dir)
            if CHUNKING_ENABLED:
                chunk_files(
                    merged_files,
                    _next_chunk_index(output_dir),
                    output_dir,
                )
    finally:
        display.stop()

    _print_summary(module.NAME, summary, output_dir)


def _print_summary(name, summary, output_dir):
    print()
    print("=" * 90)
    print(f"{name} FINISHED")
    print("=" * 90)

    print("=" * 90)
    print(f"Total jobs: {sum(summary.values())}")
    print(f"Companies:  {len(summary)}")
    print("=" * 90)

    chunk_count = len(glob.glob(os.path.join(output_dir, "chunk_*.json.gz")))
    loose_count = len(glob.glob(os.path.join(output_dir, "*_jobs.json")))

    print(f"Chunks:      {chunk_count}")
    print(f"Loose files: {loose_count}")
    print("=" * 90)


# ============================================================
# MAIN
# ============================================================

def main():
    valid_scrapers = ("workday", "greenhouse", "ashby", "lever", "bamboohr", "workable", "recruitee", "personio", "all")
    if SCRAPER not in valid_scrapers:
        raise SystemExit(
            f"Invalid SCRAPER value: '{SCRAPER}'. "
            f"Must be one of {valid_scrapers}."
        )

    tenants_file = None if SOURCE == "default" else SOURCE

    if SCRAPER in ("ashby", "all"):
        print("=" * 90)
        print("RUNNING ASHBY SCRAPER")
        print("=" * 90)
        run_scraper(ashscrap, tenants_file=tenants_file)
        print()

    if SCRAPER in ("greenhouse", "all"):
            print("=" * 90)
            print("RUNNING GREENHOUSE SCRAPER")
            print("=" * 90)
            run_scraper(greenscrap, tenants_file=tenants_file)
            print()

    if SCRAPER in ("lever", "all"):
        print("=" * 90)
        print("RUNNING LEVER SCRAPER")
        print("=" * 90)
        run_scraper(leverscrap, tenants_file=tenants_file)
        print()

    if SCRAPER in ("bamboohr", "all"):
        print("=" * 90)
        print("RUNNING BAMBOOHR SCRAPER")
        print("=" * 90)
        run_scraper(bambooscrap, tenants_file=tenants_file)
        print()

    if SCRAPER in ("workable", "all"):
        print("=" * 90)
        print("RUNNING WORKABLE SCRAPER")
        print("=" * 90)
        run_scraper(workascrap, tenants_file=tenants_file)
        print()

    if SCRAPER in ("recruitee", "all"):
        print("=" * 90)
        print("RUNNING RECRUITEE SCRAPER")
        print("=" * 90)
        run_scraper(recruitscrap, tenants_file=tenants_file)
        print()

    if SCRAPER in ("personio", "all"):
        print("=" * 90)
        print("RUNNING PERSONIO SCRAPER")
        print("=" * 90)
        run_scraper(personioscrap, tenants_file=tenants_file)
        print()

    
    if SCRAPER in ("workday", "all"):
        print("=" * 90)
        print("RUNNING WORKDAY SCRAPER")
        print("=" * 90)
        run_scraper(workdscrap, tenants_file=tenants_file)
        print()


if __name__ == "__main__":
    main()