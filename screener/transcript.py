"""Records a call transcript and saves it under profiles/<call id>/.

transcript.json is a plain array of {turn, speaker, text, started_at}, identical
for voice and simulated calls. Anything that differs between them (mode, end
reason, timings) goes in call.json beside it, so nothing reading the
transcript can branch on mode.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

from screener.config import PROJECT_ROOT

PROFILES_DIR = PROJECT_ROOT / "profiles"

INTERVIEWER = "interviewer"
CANDIDATE = "candidate"


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class TranscriptRecorder:
    def __init__(self):
        self._turns: list[dict] = []

    def add(self, speaker: str, text: str | None, started_at: str):
        """Append a turn. Empty text (e.g. a reply interrupted before any word) is skipped."""
        if speaker not in (INTERVIEWER, CANDIDATE):
            raise ValueError(f"unknown speaker: {speaker}")
        text = (text or "").strip()
        if not text:
            return
        self._turns.append(
            {
                "turn": len(self._turns) + 1,
                "speaker": speaker,
                "text": text,
                "started_at": started_at or now_iso(),
            }
        )

    @property
    def turns(self) -> list[dict]:
        return list(self._turns)

    def save(self, call_id: str, call_info: dict) -> Path:
        out_dir = PROFILES_DIR / call_id
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "transcript.json").write_text(
            json.dumps(self._turns, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        (out_dir / "call.json").write_text(
            json.dumps({**call_info, "turn_count": len(self._turns)}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return out_dir
