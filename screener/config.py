"""Loads config/voice.json and checks required environment variables.

Model names live in config/voice.json, interview prompt text in
config/interview.json (see screener.interview), keys and the voice ID in .env.
Nothing here ever logs a value from the environment.
"""

import json
import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
VOICE_CONFIG_PATH = PROJECT_ROOT / "config" / "voice.json"

REQUIRED_ENV = (
    "DEEPGRAM_API_KEY",
    "GOOGLE_API_KEY",
    "ELEVENLABS_API_KEY",
    "ELEVENLABS_VOICE_ID",
)


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class VoiceConfig:
    stt_model: str
    llm_model: str
    tts_model: str
    # If Gemini hasn't sent its first token within the timeout, re-send the
    # request once (Pipecat's retry_on_timeout). Free-tier Gemini occasionally
    # stalls for 14-16s before starting a reply (seen in Ticket 02 testing).
    llm_retry_if_slow_to_start: bool
    llm_first_reply_timeout_secs: float
    # Every Gemini request this process sends to a model waits for a slot
    # under this cap. Free tier for our Flash-Lite model: 15 per minute per
    # project per model (from Google's 429 response, Nov 2026).
    llm_max_requests_per_minute: int


def read_json(path: Path) -> dict:
    # utf-8-sig also accepts a leading byte-order mark, which Notepad and
    # Windows PowerShell can add when saving a file as UTF-8.
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as e:
        raise ConfigError(f"Config file not found: {path}") from e
    except json.JSONDecodeError as e:
        raise ConfigError(f"{path.name} is not valid JSON: {e}") from e


def require_str(data: dict, dotted: str, source: str) -> str:
    node = data
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            raise ConfigError(f"{source}: missing '{dotted}'")
        node = node[part]
    if not isinstance(node, str) or not node.strip():
        raise ConfigError(f"{source}: '{dotted}' must be a non-empty string")
    return node.strip()


def load_voice_config(path: Path = VOICE_CONFIG_PATH) -> VoiceConfig:
    data = read_json(path)
    llm = data.get("llm", {})

    retry = llm.get("retry_if_slow_to_start")
    if not isinstance(retry, bool):
        raise ConfigError(f"{path.name}: 'llm.retry_if_slow_to_start' must be true or false")
    timeout = llm.get("first_reply_timeout_secs")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise ConfigError(f"{path.name}: 'llm.first_reply_timeout_secs' must be a positive number")
    rpm = llm.get("max_requests_per_minute")
    if isinstance(rpm, bool) or not isinstance(rpm, int) or rpm <= 0:
        raise ConfigError(f"{path.name}: 'llm.max_requests_per_minute' must be a positive whole number")

    return VoiceConfig(
        stt_model=require_str(data, "stt.model", path.name),
        llm_model=require_str(data, "llm.model", path.name),
        tts_model=require_str(data, "tts.model", path.name),
        llm_retry_if_slow_to_start=retry,
        llm_first_reply_timeout_secs=float(timeout),
        llm_max_requests_per_minute=rpm,
    )


def require_env(names: tuple[str, ...] = REQUIRED_ENV) -> dict[str, str]:
    """Return the required env vars, or raise naming the missing ones (never the values)."""
    unknown = [name for name in names if name not in REQUIRED_ENV]
    if unknown:
        raise ConfigError(f"Not a known .env name: {', '.join(unknown)}")
    missing = [name for name in names if not os.getenv(name, "").strip()]
    if missing:
        raise ConfigError(
            "Missing or empty in .env: " + ", ".join(missing) + ". Open .env in Notepad and paste the value after the '='."
        )
    return {name: os.environ[name].strip() for name in names}
