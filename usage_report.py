"""Prints this month's usage totals against the free-tier limits.

Usage:
    uv run python usage_report.py
    uv run python usage_report.py --month 2026-11

Reads logs/usage.csv (one row per call) and the limits in config/usage.json.
Sends nothing and spends nothing.
"""

import argparse
import re
import sys
from datetime import date

from loguru import logger

logger.remove()  # keep the output to the report (importing Pipecat logs a banner)

from screener.config import ConfigError  # noqa: E402
from screener.usage import USAGE_LOG_PATH, describe_totals, load_usage_limits, read_usage  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Print usage totals against the free-tier limits.")
    parser.add_argument("--month", help="YYYY-MM (default: this month)")
    args = parser.parse_args()

    today = date.today()
    month = args.month or f"{today:%Y-%m}"
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        print("ERROR: --month must look like 2026-11")
        return 1
    try:
        limits = load_usage_limits()
    except ConfigError as e:
        print(f"ERROR: {e}")
        return 1

    rows = read_usage()
    if not rows:
        print(f"No usage logged yet ({USAGE_LOG_PATH} is missing or empty). It fills in after each call.")
        return 0
    print("\n".join(describe_totals(rows, limits, month, today.isoformat())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
