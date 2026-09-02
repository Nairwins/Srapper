"""
Health check for BambooHR tenants.

Reads the same tenants file bambooscrap.py uses (data/tenants.json by
default), checks every company's careers widget, and removes only the
ones that are confirmed dead (no widget for that tenant - a real 404,
or an unrecognized response shape). Anything ambiguous (rate limiting,
network errors, other HTTP errors) is left alone.

Usage:
    python health/bamboo_health.py [path/to/tenants.json]

If no path is given, it uses bambooscrap.TENANTS_FILE
("data/tenants.json", relative to wherever you run this from).
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scrappy import bambooscrap
from health._common import run_health_check


if __name__ == "__main__":
    tenants_file = sys.argv[1] if len(sys.argv) > 1 else None
    run_health_check(bambooscrap, is_workday=False, tenants_file=tenants_file)
