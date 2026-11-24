"""The review page's local API: lists calls, reads one call, saves decisions.

Serves review/review.html and JSON under /api. Reads only what the pipeline
saved under profiles/; the one thing it writes is a recommendation's status
and decided_at in matches.json (approved, rejected, or back to pending).
Nothing is sent anywhere. Bound to 127.0.0.1 by review_server.py.

The page gets the profile field names and titles from /api/schema
(config/profile.schema.json), so it never hard-codes a field name.
"""

import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse

from screener.config import PROJECT_ROOT, ConfigError, read_json, require_str
from screener.matching import load_matching_config
from screener.profile import load_extraction_config, load_schema
from screener.transcript import PROFILES_DIR, now_iso

REVIEW_CONFIG_PATH = PROJECT_ROOT / "config" / "review.json"
PAGE_PATH = PROJECT_ROOT / "review" / "review.html"
CALL_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


@dataclass(frozen=True)
class ReviewConfig:
    port: int
    voice_page_url: str
    approved: str
    rejected: str


def load_review_config(path: Path = REVIEW_CONFIG_PATH) -> ReviewConfig:
    source = path.name
    data = read_json(path)
    port = data.get("port")
    if isinstance(port, bool) or not isinstance(port, int) or not 1024 <= port <= 65535:
        raise ConfigError(f"{source}: 'port' must be a whole number from 1024 to 65535")
    return ReviewConfig(
        port=port,
        voice_page_url=require_str(data, "voice_page_url", source),
        approved=require_str(data, "decisions.approve", source),
        rejected=require_str(data, "decisions.reject", source),
    )


def _read(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _write_atomically(path: Path, data: dict):
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".matches-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def create_app(profiles_dir: Path = PROFILES_DIR) -> FastAPI:
    app = FastAPI(title="Voice Screener review", docs_url=None, redoc_url=None, openapi_url=None)
    review_cfg = load_review_config()
    matching_cfg = load_matching_config()
    pending = matching_cfg.pending_status
    allowed_decisions = {review_cfg.approved, review_cfg.rejected, pending}
    not_discussed = load_extraction_config().not_discussed

    def call_dir(call_id: str) -> Path:
        if not CALL_ID_PATTERN.match(call_id):
            raise HTTPException(400, "Not a valid call id")
        path = profiles_dir / call_id
        if not (path / "transcript.json").is_file():
            raise HTTPException(404, "No such call")
        return path

    def summary(path: Path) -> dict:
        call = _read(path / "call.json") or {}
        profile = _read(path / "profile.json")
        matches = _read(path / "matches.json")
        return {
            "call_id": path.name,
            "started_at": call.get("started_at"),
            "mode": call.get("mode"),
            "persona": call.get("persona"),
            "end_reason": call.get("end_reason"),
            "duration_secs": call.get("duration_secs"),
            "profile_status": profile.get("status") if profile else None,
            "matches_status": matches.get("status") if matches else None,
            "outcomes": [
                {"role_id": r["role_id"], "title": r["title"],
                 "outcome": r["recommendation"]["outcome"], "status": r["recommendation"]["status"]}
                for r in (matches or {}).get("roles", [])
            ],
        }

    @app.get("/")
    def page():
        return FileResponse(PAGE_PATH, media_type="text/html")

    @app.get("/api/config")
    def config():
        return {
            "voice_page_url": review_cfg.voice_page_url,
            "decisions": {"approved": review_cfg.approved, "rejected": review_cfg.rejected, "pending": pending},
            "outcomes": matching_cfg.outcomes,
            "ratings": matching_cfg.ratings,
            "not_discussed": not_discussed,
        }

    @app.get("/api/schema")
    def schema():
        return load_schema()

    @app.get("/api/calls")
    def calls():
        found = [summary(p) for p in profiles_dir.iterdir()
                 if p.is_dir() and CALL_ID_PATTERN.match(p.name) and (p / "transcript.json").is_file()] \
            if profiles_dir.exists() else []
        found.sort(key=lambda c: (c["started_at"] or "", c["call_id"]), reverse=True)
        return found

    @app.get("/api/calls/{call_id}")
    def call(call_id: str):
        path = call_dir(call_id)
        return {
            "info": summary(path),
            "transcript": _read(path / "transcript.json") or [],
            "profile": _read(path / "profile.json"),
            "matches": _read(path / "matches.json"),
        }

    @app.post("/api/calls/{call_id}/decision")
    def decide(call_id: str, role_id: str = Body(...), decision: str = Body(...)):
        path = call_dir(call_id)
        if decision not in allowed_decisions:
            raise HTTPException(400, f"Decision must be one of: {', '.join(sorted(allowed_decisions))}")
        matches = _read(path / "matches.json")
        if not matches or matches.get("status") != "valid":
            raise HTTPException(409, "This call has no valid matches to decide on")
        role = next((r for r in matches["roles"] if r["role_id"] == role_id), None)
        if role is None:
            raise HTTPException(404, "No such role in this call")
        role["recommendation"]["status"] = decision
        role["recommendation"]["decided_at"] = None if decision == pending else now_iso()
        _write_atomically(path / "matches.json", matches)
        return role["recommendation"]

    return app
