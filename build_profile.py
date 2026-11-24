"""Builds (or rebuilds) the candidate profile for saved calls.

Usage:
    uv run python build_profile.py 20261030-155617
    uv run python build_profile.py --latest
    uv run python build_profile.py 20261030-155617 --schema path\\to\\other.schema.json

Takes call folder names under profiles/ (or full paths). Works the same for
voice and simulated calls: it reads transcript.json only. Overwrites the
folder's profile.json and prints the Gemini request count. --schema points at
a different schema file, e.g. a deliberately broken copy to test the invalid
path; the real config/profile.schema.json is never modified.
"""

import argparse
import asyncio
import sys
from pathlib import Path

from dotenv import load_dotenv
from loguru import logger

from screener.config import PROJECT_ROOT, ConfigError, require_env
from screener.profile import describe_profile, load_schema, profile_call
from screener.transcript import PROFILES_DIR


def resolve(name: str) -> Path:
    path = Path(name)
    if not path.is_absolute():
        path = PROFILES_DIR / name
    if not (path / "transcript.json").exists():
        raise ConfigError(f"No transcript.json in {path}")
    return path


async def main() -> int:
    parser = argparse.ArgumentParser(description="Build candidate profiles from saved call transcripts.")
    parser.add_argument("calls", nargs="*", help="call folder names under profiles/")
    parser.add_argument("--latest", action="store_true", help="the most recent call folder")
    parser.add_argument("--schema", help="use this schema file instead of config/profile.schema.json")
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
        schema = load_schema(Path(args.schema)) if args.schema else load_schema()
    except ConfigError as e:
        print(f"ERROR: {e}")
        return 1

    total, all_valid = 0, True
    for call_dir in call_dirs:
        print(f"\n=== {call_dir.name} ===")
        result, requests = await profile_call(call_dir, api_key, schema=schema)
        total += requests
        all_valid &= result["status"] == "valid"
        print("\n".join(describe_profile(result, schema)))
        print(f"Saved: {call_dir / 'profile.json'}")
    print(f"\nGemini requests used: {total}")
    return 0 if all_valid else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
