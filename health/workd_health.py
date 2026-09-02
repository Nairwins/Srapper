"""
Health check for Workday tenants.

Reads the same tenants file workdscrap.py uses (data/tenants.json by
default - entries here are "tenant|wd|site" strings), checks every
company's job feed, and removes only the ones that are confirmed dead
(a real HTTP 404 - the tenant/wd/site combo genuinely doesn't exist).
Anything ambiguous (rate limiting/429, other HTTP errors, network
errors, bad JSON) is left alone, since workdscrap.py doesn't classify
those the way the other scrapers do.

Usage:
    python health/workd_health.py [path/to/tenants.json]

If no path is given, it uses workdscrap.TENANTS_FILE
("data/tenants.json", relative to wherever you run this from).
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scrappy import workdscrap
from health._common import run_health_check


if __name__ == "__main__":
    tenants_file = sys.argv[1] if len(sys.argv) > 1 else None
    run_health_check(workdscrap, is_workday=True, tenants_file=tenants_file)
