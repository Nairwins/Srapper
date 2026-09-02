"""
Health check for Ashby tenants.

Reads the same tenants file ashscrap.py uses (data/tenants.json by
default), checks every company's job board, and removes only the ones
that are confirmed dead (board genuinely doesn't exist - a real 404,
or an unrecognized response shape). Anything ambiguous (rate limiting,
network errors, other HTTP errors) is left alone.

Usage:
    python health/ashby_health.py [path/to/tenants.json]

If no path is given, it uses ashscrap.TENANTS_FILE ("data/tenants.json",
relative to wherever you run this from).
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scrappy import ashscrap
from health._common import run_health_check


if __name__ == "__main__":
    tenants_file = sys.argv[1] if len(sys.argv) > 1 else None
    run_health_check(ashscrap, is_workday=False, tenants_file=tenants_file)
