"""
Shared logic for the health-check scripts in this folder.

Not a scraper itself - just the code every {platform}_health.py file in
here calls into, so the actual checking logic lives in one place instead
of being copy-pasted 7 times.

WHAT COUNTS AS "DEAD" (removed from the tenants file):
    Only a tenant whose board/account genuinely doesn't exist any more -
    a real 404 / "no board found" / unrecognized response shape. That's
    the scraper's own `error_type == "not_found"` (or, for Workday,
    which doesn't have an error_type field, a plain HTTP 404).

WHAT IS IGNORED (tenant is kept, even though the check "failed"):
    Anything that could just be a temporary hiccup and NOT proof the
    company/link is wrong - rate limiting (429), other HTTP errors,
    network errors, timeouts, unexpected-but-not-404 failures. These
    get printed so you can see them, but the tenant stays in the file.
    Each scraper already retries these a couple of times internally
    before giving up, so what you see here is already past that.
"""

import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed


def _default_key_extractor(raw_entry):
    """Recreate the same 'key' that module.load_tenants() would produce
    for one raw entry from the JSON file, WITHOUT needing the parsed
    tenant list - used so we can filter the original raw JSON (which
    might be plain strings or dicts) instead of the parsed dicts."""
    if isinstance(raw_entry, str):
        token = raw_entry.strip()
        return token or None
    if isinstance(raw_entry, dict):
        token = raw_entry.get("token") or raw_entry.get("slug") or raw_entry.get("name")
        return token.strip() if token else None
    return None


def _workday_key_extractor(raw_entry):
    """Workday tenants are 'tenant|wd|site' strings - same key shape
    workdscrap.load_tenants() builds internally."""
    if not isinstance(raw_entry, str):
        return None
    parts = raw_entry.split("|")
    if len(parts) != 3:
        return None
    tenant, wd, site = (p.strip() for p in parts)
    return f"{tenant}|{wd}|{site}"


def _dedupe_and_reorder(raw_list, key_extractor):
    """Remove duplicate tenant entries (same key - keep the first
    occurrence) and sort what's left alphabetically by key, so the
    tenants file stays tidy and every health-check run walks tenants
    in a stable, predictable order.

    Entries whose key can't be determined (malformed - load_tenants()
    already flags these on its own) are left as-is, in their original
    relative order, appended after every keyed entry - there's nothing
    to dedupe/sort them by."""
    seen_keys = set()
    keyed = []
    unkeyed = []

    for raw_entry in raw_list:
        key = key_extractor(raw_entry)

        if key is None:
            unkeyed.append(raw_entry)
            continue

        if key in seen_keys:
            continue

        seen_keys.add(key)
        keyed.append((key, raw_entry))

    keyed.sort(key=lambda pair: pair[0].lower())

    return [raw_entry for _key, raw_entry in keyed] + unkeyed


def check_tenant(module, tenant_info, is_workday=False, rate_limit_is_dead=False):
    """
    Run one fetch against a single tenant and decide what to do with it.

    Returns (keep, detail):
        keep=True   -> tenant is fine, OR the failure was ambiguous
                       (blocking/rate limit/network/etc.) - leave it in
        keep=False  -> confirmed dead link - remove it
        detail      -> None on success, else a short reason string
    """
    result = module.fetch_jobs(tenant_info, 0)
    error = result.get("error")

    if error is None:
        return True, None

    if is_workday:
        # workdscrap.py doesn't classify errors into an error_type the
        # way the other scrapers do - it just gives back a string like
        # "HTTP 404", "HTTP 429", "request error: ...", or "invalid
        # JSON response". Only a real 404 means the tenant/site/wd
        # combo genuinely doesn't exist; everything else is ambiguous
        # (could be blocking, a network blip, a temporary bad response)
        # and gets ignored.
        if isinstance(error, str) and error.strip().startswith("HTTP 404"):
            return False, error
        return True, error

    error_type = result.get("error_type")
    if error_type == "rate_limited" and rate_limit_is_dead:
        return False, error

    if error_type == "not_found":
        return False, error

    # rate_limited / network_error / http_error / anything else -> ignore
    return True, error


def run_health_check(module, is_workday=False, tenants_file=None, delay=0.3,
                     workers=20, rate_limit_is_dead=False):
    """
    Check every tenant in `module`'s tenants file, print a line per
    tenant, then rewrite the file with confirmed-dead tenants removed.
    Everything else (including anything that errored ambiguously) is
    left in place untouched.

    Tenants are checked concurrently using a thread pool (`workers`
    threads at a time - each request is a blocking network call, so
    threads are enough to get real speedup without needing async).
    Set workers=1 to fall back to old-style one-at-a-time checking
    (still honors `delay` in that case).
    """
    path = tenants_file or module.TENANTS_FILE

    with open(path, "r", encoding="utf-8") as f:
        raw_list = json.load(f)

    key_extractor = _workday_key_extractor if is_workday else _default_key_extractor

    # Dedupe + reorder BEFORE any checking happens, so duplicate
    # entries never get checked (and potentially reported/removed)
    # twice, and every run walks tenants in the same stable order.
    deduped_raw_list = _dedupe_and_reorder(raw_list, key_extractor)

    if deduped_raw_list != raw_list:
        dupes_removed = len(raw_list) - len(deduped_raw_list)

        with open(path, "w", encoding="utf-8") as f:
            json.dump(deduped_raw_list, f, indent=2, ensure_ascii=False)

        if dupes_removed:
            print(f"[{module.NAME}] removed {dupes_removed} duplicate tenant(s) "
                  f"and reordered {path}\n")
        else:
            print(f"[{module.NAME}] reordered {path} (no duplicates found)\n")

        raw_list = deduped_raw_list

    tenants = module.load_tenants(path)

    total = len(tenants)
    print(f"[{module.NAME}] checking {total} tenant(s) from {path} "
          f"({workers} worker{'s' if workers != 1 else ''})\n")

    removed = []  # list of (key, reason)

    if workers <= 1:
        # Original sequential path, unchanged behavior (including delay
        # between requests - useful if a platform starts rate-limiting
        # under concurrency and you need to dial it back down).
        for i, tenant_info in enumerate(tenants, start=1):
            label = tenant_info.get("label") or tenant_info.get("key")
            keep, detail = check_tenant(
                module,
                tenant_info,
                is_workday=is_workday,
                rate_limit_is_dead=rate_limit_is_dead,
            )

            if keep:
                status = "ok" if detail is None else f"ok (ignored: {detail})"
            else:
                status = f"DEAD - {detail}"
                removed.append((tenant_info["key"], detail))

            print(f"  [{i}/{total}] {label:<30} {status}")

            if i < total:
                time.sleep(delay)
    else:
        # Concurrent path. No delay/sleep between requests - the point
        # of running N workers is to have N requests in flight, so a
        # throttling sleep between submissions would defeat that. If a
        # platform starts returning 429s under this load, those are
        # ambiguous failures anyway (see module docstring) and the
        # tenant is left alone, not removed.
        completed = 0
        with ThreadPoolExecutor(max_workers=workers) as pool:
            future_to_tenant = {
                pool.submit(
                    check_tenant,
                    module,
                    tenant_info,
                    is_workday=is_workday,
                    rate_limit_is_dead=rate_limit_is_dead,
                ): tenant_info
                for tenant_info in tenants
            }

            for future in as_completed(future_to_tenant):
                tenant_info = future_to_tenant[future]
                label = tenant_info.get("label") or tenant_info.get("key")
                completed += 1

                try:
                    keep, detail = future.result()
                except Exception as e:
                    # A tenant whose check itself blew up (bug, unhandled
                    # exception in the scraper, etc.) is ambiguous, not
                    # confirmed-dead - keep it, same spirit as the other
                    # "ignore and leave in place" cases above.
                    keep, detail = True, f"health check raised {e!r}"

                if keep:
                    status = "ok" if detail is None else f"ok (ignored: {detail})"
                else:
                    status = f"DEAD - {detail}"
                    removed.append((tenant_info["key"], detail))

                print(f"  [{completed}/{total}] {label:<30} {status}")

    removed_keys = {key for key, _ in removed}

    kept_raw = []
    for raw_entry in raw_list:
        key = key_extractor(raw_entry)
        if key is not None and key in removed_keys:
            continue
        kept_raw.append(raw_entry)

    print()
    if removed:
        print(f"Removing {len(removed)} dead tenant(s):")
        for key, detail in removed:
            print(f"  - {key}: {detail}")
    else:
        print("No dead tenants found - nothing removed.")

    if len(kept_raw) != len(raw_list):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(kept_raw, f, indent=2, ensure_ascii=False)
        print(f"\nUpdated {path}: {len(raw_list)} -> {len(kept_raw)} entries.")
    else:
        print(f"\n{path} unchanged ({len(kept_raw)} entries).")

    return kept_raw, removed