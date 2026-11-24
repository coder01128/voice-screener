"""Per-call usage log (logs/usage.csv) and this month's totals.

One row per call, voice or simulated, appended when the call's post-call work
is done:

    date, call_id, mode, call_seconds, deepgram_audio_seconds,
    elevenlabs_characters, gemini_requests, median_latency_secs

Deepgram audio seconds and ElevenLabs characters are what Pipecat's own
services report (STTUsageMetricsData / TTSUsageMetricsData, collected by
ServiceMetricsObserver), not estimates. Gemini requests are counted where they
are sent (screener.llm), retries included. Median latency is the median of the
per-turn values written to logs/latency.log.

The free-tier limits the totals are compared against live in
config/usage.json, with where each figure came from.
"""

import csv
import statistics
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from pipecat.observers.service_metrics_observer import (
    ServiceMetricsObserver,
    ServiceUsageKind,
    ServiceUsageRecord,
)

from screener.config import PROJECT_ROOT, ConfigError, read_json

USAGE_LOG_PATH = PROJECT_ROOT / "logs" / "usage.csv"
USAGE_CONFIG_PATH = PROJECT_ROOT / "config" / "usage.json"

COLUMNS = (
    "date",
    "call_id",
    "mode",
    "call_seconds",
    "deepgram_audio_seconds",
    "elevenlabs_characters",
    "gemini_requests",
    "median_latency_secs",
)


class UsageMeter:
    """Sums what Deepgram and ElevenLabs report during one voice call."""

    def __init__(self):
        self.deepgram_audio_seconds = 0.0
        self.elevenlabs_characters = 0
        self.observer = ServiceMetricsObserver()

        @self.observer.event_handler("on_service_usage")
        async def on_service_usage(_observer, record: ServiceUsageRecord):
            self.add(record)

    def add(self, record: ServiceUsageRecord):
        if record.kind == ServiceUsageKind.STT and record.audio_seconds:
            self.deepgram_audio_seconds += record.audio_seconds
        elif record.kind == ServiceUsageKind.TTS and record.characters:
            self.elevenlabs_characters += record.characters


def median_or_blank(values: list[float]) -> float | str:
    return round(statistics.median(values), 3) if values else ""


def local_date(started_at_iso: str | None) -> str:
    """The call's start date in this computer's time zone (transcripts store UTC)."""
    if started_at_iso:
        try:
            return datetime.fromisoformat(started_at_iso).astimezone().date().isoformat()
        except ValueError:
            pass
    return date.today().isoformat()


def usage_row(
    *,
    call_id: str,
    mode: str,
    started_at: str | None,
    call_seconds: float | None,
    deepgram_audio_seconds: float,
    elevenlabs_characters: int,
    gemini_requests: int,
    latencies_secs: list[float],
) -> dict:
    return {
        "date": local_date(started_at),
        "call_id": call_id,
        "mode": mode,
        "call_seconds": "" if call_seconds is None else round(call_seconds, 1),
        "deepgram_audio_seconds": round(deepgram_audio_seconds, 1),
        "elevenlabs_characters": elevenlabs_characters,
        "gemini_requests": gemini_requests,
        "median_latency_secs": median_or_blank(latencies_secs),
    }


def append_usage_row(row: dict, path: Path = USAGE_LOG_PATH):
    if set(row) != set(COLUMNS):
        raise ValueError(f"usage row must have exactly these columns: {', '.join(COLUMNS)}")
    path.parent.mkdir(parents=True, exist_ok=True)
    new_file = not path.exists() or path.stat().st_size == 0
    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        if new_file:
            writer.writeheader()
        writer.writerow(row)


def read_usage(path: Path = USAGE_LOG_PATH) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


# --- limits and totals ---------------------------------------------------------


@dataclass(frozen=True)
class UsageLimits:
    deepgram_free_credit_usd: float
    deepgram_usd_per_minute: float
    zar_per_usd: float
    elevenlabs_credits_per_month: int
    elevenlabs_credits_per_character: float
    gemini_requests_per_minute: int
    gemini_requests_per_day: int
    gemini_daily_confirmed: bool


def _number(data: dict, dotted: str, source: str) -> float:
    node = data
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            raise ConfigError(f"{source}: missing '{dotted}'")
        node = node[part]
    if isinstance(node, bool) or not isinstance(node, (int, float)) or node <= 0:
        raise ConfigError(f"{source}: '{dotted}' must be a number above 0")
    return node


def load_usage_limits(path: Path = USAGE_CONFIG_PATH) -> UsageLimits:
    source = path.name
    data = read_json(path)
    confirmed = data.get("gemini", {}).get("requests_per_day_confirmed")
    if not isinstance(confirmed, bool):
        raise ConfigError(f"{source}: 'gemini.requests_per_day_confirmed' must be true or false")
    return UsageLimits(
        deepgram_free_credit_usd=_number(data, "deepgram.free_credit_usd", source),
        deepgram_usd_per_minute=_number(data, "deepgram.usd_per_streaming_minute", source),
        zar_per_usd=_number(data, "zar_per_usd", source),
        elevenlabs_credits_per_month=int(_number(data, "elevenlabs.credits_per_month", source)),
        elevenlabs_credits_per_character=_number(data, "elevenlabs.credits_per_character", source),
        gemini_requests_per_minute=int(_number(data, "gemini.requests_per_minute", source)),
        gemini_requests_per_day=int(_number(data, "gemini.requests_per_day", source)),
        gemini_daily_confirmed=confirmed,
    )


def _f(value: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


@dataclass
class Totals:
    calls_by_mode: dict
    call_seconds: float
    deepgram_audio_seconds: float
    elevenlabs_characters: int
    gemini_requests: int
    median_latency_secs: float | None


def totals(rows: list[dict]) -> Totals:
    by_mode: dict[str, int] = {}
    for r in rows:
        by_mode[r.get("mode") or "unknown"] = by_mode.get(r.get("mode") or "unknown", 0) + 1
    medians = [_f(r["median_latency_secs"]) for r in rows if r.get("median_latency_secs")]
    return Totals(
        calls_by_mode=dict(sorted(by_mode.items())),
        call_seconds=sum(_f(r.get("call_seconds")) for r in rows),
        deepgram_audio_seconds=sum(_f(r.get("deepgram_audio_seconds")) for r in rows),
        elevenlabs_characters=int(sum(_f(r.get("elevenlabs_characters")) for r in rows)),
        gemini_requests=int(sum(_f(r.get("gemini_requests")) for r in rows)),
        median_latency_secs=round(statistics.median(medians), 2) if medians else None,
    )


def _pct(used: float, limit: float) -> str:
    return f"{used / limit * 100:.1f}%"


def describe_totals(rows: list[dict], limits: UsageLimits, month: str, today: str) -> list[str]:
    """month is 'YYYY-MM', today is 'YYYY-MM-DD' (both local dates)."""
    month_rows = [r for r in rows if (r.get("date") or "").startswith(month)]
    t = totals(month_rows)
    all_time = totals(rows)
    today_requests = totals([r for r in rows if r.get("date") == today]).gemini_requests
    month_name = datetime.strptime(month, "%Y-%m").strftime("%B %Y")

    calls = ", ".join(f"{n} {mode}" for mode, n in t.calls_by_mode.items()) or "none"
    dg_minutes = t.deepgram_audio_seconds / 60
    dg_usd = dg_minutes * limits.deepgram_usd_per_minute
    dg_all_usd = all_time.deepgram_audio_seconds / 60 * limits.deepgram_usd_per_minute
    el_credits = t.elevenlabs_characters * limits.elevenlabs_credits_per_character

    def zar(usd: float) -> str:
        return f"R{usd * limits.zar_per_usd:,.2f}"

    day_note = "" if limits.gemini_daily_confirmed else " (unconfirmed figure: check AI Studio)"

    return [
        f"Usage for {month_name} (from logs/usage.csv)",
        f"  Calls logged: {sum(t.calls_by_mode.values())} ({calls}), {t.call_seconds / 60:.1f} min in total",
        "",
        "Deepgram (one-off sign-up credit, not monthly)",
        f"  This month: {dg_minutes:.1f} min of audio, about ${dg_usd:.2f} ({zar(dg_usd)}) "
        f"at ${limits.deepgram_usd_per_minute}/min",
        f"  All months logged: about ${dg_all_usd:.2f} ({zar(dg_all_usd)}) of the "
        f"${limits.deepgram_free_credit_usd:g} ({zar(limits.deepgram_free_credit_usd)}) credit "
        f"({_pct(dg_all_usd, limits.deepgram_free_credit_usd)})",
        "",
        "ElevenLabs (monthly credits)",
        f"  This month: {t.elevenlabs_characters:,} characters, up to {el_credits:,.0f} of "
        f"{limits.elevenlabs_credits_per_month:,} credits ({_pct(el_credits, limits.elevenlabs_credits_per_month)})",
        "",
        "Gemini (free tier, limits per day and per minute)",
        f"  This month: {t.gemini_requests:,} requests",
        f"  Today: {today_requests} of about {limits.gemini_requests_per_day} per day{day_note}",
        f"  Per-minute cap: {limits.gemini_requests_per_minute}; the bot paces itself under it",
        "",
        "Reply latency (voice calls)",
        f"  Median of this month's per-call medians: {t.median_latency_secs} s" if t.median_latency_secs is not None
        else "  No voice calls logged this month",
        "",
        "Not in this log: build_profile.py and match_roles.py re-runs, check_keys.py.",
        "Each service's own dashboard shows the exact balance.",
    ]
