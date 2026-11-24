"""Local review page for screening calls.

Usage:
    uv run python review_server.py

Then open the address it prints (default http://127.0.0.1:7870). Lists every
call under profiles/, shows its profile, role matches and transcript, and
saves Approve / Reject decisions into that call's matches.json. Listens on
this computer only (127.0.0.1); nothing is sent anywhere. Stop it with Ctrl+C.
"""

import sys

import uvicorn

from screener.config import ConfigError
from screener.review import create_app, load_review_config


def main() -> int:
    try:
        cfg = load_review_config()
        app = create_app()
    except ConfigError as e:
        print(f"ERROR: {e}")
        return 1
    print(f"Review page: http://127.0.0.1:{cfg.port}  (Ctrl+C to stop)", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=cfg.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
