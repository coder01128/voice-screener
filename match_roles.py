"""Matches saved calls against the roles (or re-matches after editing roles.json).

Usage:
    uv run python match_roles.py 20261124-104012-sim-thandi
    uv run python match_roles.py --latest
    uv run python match_roles.py 20261124-104012-sim-thandi --roles path\\to\\other.roles.json

Takes call folder names under profiles/ (or full paths). Reads transcript.json
only, so voice and simulated calls are matched the same way. Overwrites the
folder's matches.json (every recommendation back to pending) and prints the
Gemini request count. --roles uses a different roles file, e.g. a copy with an
edited must-have; config/roles.json is never modified.
"""

import argparse
import asyncio
import sys
from pathlib import Path

from dotenv import load_dotenv
from loguru import logger

from screener.config import PROJECT_ROOT, ConfigError, require_env
from screener.matching import describe_matches, load_roles, match_call
from screener.transcript import PROFILES_DIR


def resolve(name: str) -> Path:
    path = Path(name)
    if not path.is_absolute():
        path = PROFILES_DIR / name
    if not (path / "transcript.json").exists():
        raise ConfigError(f"No transcript.json in {path}")
    return path


async def main() -> int:
    parser = argparse.ArgumentParser(description="Match saved calls against the roles.")
    parser.add_argument("calls", nargs="*", help="call folder names under profiles/")
    parser.add_argument("--latest", action="store_true", help="the most recent call folder")
    parser.add_argument("--roles", help="use this roles file instead of config/roles.json")
    args = parser.parse_args()

    logger.remove()
    logger.add(sys.stderr, level="WARNING")
    load_dotenv(PROJECT_ROOT / ".env", override=True)
    try:
        api_key = require_env(("GOOGLE_API_KEY",))["GOOGLE_API_KEY"]
        names = list(args.calls)
        if args.latest:
            folders = sorted(p.name for p in PROFILES_DIR.iterdir() if (p / "transcript.json").exists())
            names.append(folders[-1])
        if not names:
            parser.error("give a call folder name or --latest")
        call_dirs = [resolve(n) for n in names]
        roles = load_roles(Path(args.roles)) if args.roles else load_roles()
    except ConfigError as e:
        print(f"ERROR: {e}")
        return 1

    total, all_valid = 0, True
    for call_dir in call_dirs:
        print(f"\n=== {call_dir.name} ===")
        result, requests = await match_call(call_dir, api_key, roles=roles)
        total += requests
        all_valid &= result["status"] == "valid"
        print("\n".join(describe_matches(result)))
        print(f"Saved: {call_dir / 'matches.json'}")
    print(f"\nGemini requests used: {total}")
    return 0 if all_valid else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
