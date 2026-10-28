"""Writes one line per user turn to logs/latency.log.

Latency is measured by Pipecat's UserBotLatencyObserver: from when the user
actually stopped speaking (VAD stop time minus its silence window) to when the
bot started speaking.
"""

from datetime import datetime
from pathlib import Path

from pipecat.observers.user_bot_latency_observer import (
    LatencyBreakdown,
    MeasuredFrom,
    UserBotLatencyObserver,
)

from screener.config import PROJECT_ROOT

LATENCY_LOG_PATH = PROJECT_ROOT / "logs" / "latency.log"


class LatencyLog:
    def __init__(self, session_id: str, path: Path = LATENCY_LOG_PATH):
        self._session_id = session_id
        self._path = path
        self._turn = 0
        self._pending_secs: float | None = None
        self.observer = UserBotLatencyObserver()

        # on_latency_measured fires only for user turns (not the greeting), and
        # always just before on_latency_breakdown for the same turn.
        @self.observer.event_handler("on_latency_measured")
        async def on_latency_measured(_observer, latency_secs: float):
            self._pending_secs = latency_secs

        @self.observer.event_handler("on_latency_breakdown")
        async def on_latency_breakdown(_observer, breakdown: LatencyBreakdown):
            if self._pending_secs is None:
                return
            self._turn += 1
            self._write(self._pending_secs, breakdown)
            self._pending_secs = None

    def _write(self, latency_secs: float, breakdown: LatencyBreakdown):
        parts = (
            "; ".join(f"{c.label} {c.duration_secs:.3f}s" for c in breakdown.contributions)
            if breakdown.measured_from is MeasuredFrom.USER_SILENCE
            else ""
        )
        line = (
            f"{datetime.now().astimezone().isoformat(timespec='milliseconds')}"
            f"  session={self._session_id}"
            f"  turn={self._turn}"
            f"  latency_ms={round(latency_secs * 1000)}"
            f"  breakdown=[{parts}]\n"
        )
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as f:
            f.write(line)
