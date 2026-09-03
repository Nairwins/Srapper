"""
Health check for Personio tenants.

Reads the same tenants file personioscrap.py uses (data/tenants.json
by default), checks every company's job board, and removes only the
ones that are confirmed dead (subdomain genuinely doesn't exist, i.e.
a 404 on the XML feed). Anything ambiguous (rate limiting, network
errors, other HTTP errors, malformed XML) is left alone.

Usage:
    python health/personio_health.py [path/to/tenants.json]

If no path is given, it uses personioscrap.TENANTS_FILE
("data/tenants.json", relative to wherever you run this from).
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scrappy import personioscrap
from health._common import run_health_check


if __name__ == "__main__":
    tenants_file = sys.argv[1] if len(sys.argv) > 1 else None
    run_health_check(
        personioscrap,
        is_workday=False,
        tenants_file=tenants_file,
        workers=10,
        rate_limit_is_dead=True,
    )
