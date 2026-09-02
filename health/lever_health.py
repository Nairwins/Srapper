"""
Health check for Lever tenants.

Reads the same tenants file leverscrap.py uses (data/tenants.json by
default), checks every company's job board, and removes only the ones
that are confirmed dead (board genuinely doesn't exist, or the response
isn't the JSON array a healthy board returns). Anything ambiguous (rate
limiting, network errors, other HTTP errors) is left alone.

Usage:
    python health/lever_health.py [path/to/tenants.json]

If no path is given, it uses leverscrap.TENANTS_FILE
("data/tenants.json", relative to wherever you run this from).
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scrappy import leverscrap
from health._common import run_health_check


if __name__ == "__main__":
    tenants_file = sys.argv[1] if len(sys.argv) > 1 else None
    run_health_check(leverscrap, is_workday=False, tenants_file=tenants_file)
