"""Loads config/voice.json and checks required environment variables.

Model names and prompt text live in config/voice.json; keys and the voice ID
live in .env. Nothing here ever logs a value from the environment.
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
    system_prompt: str
    greeting_prompt: str


def _require_str(data: dict, dotted: str) -> str:
    node = data
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            raise ConfigError(f"{VOICE_CONFIG_PATH.name}: missing '{dotted}'")
        node = node[part]
    if not isinstance(node, str) or not node.strip():
        raise ConfigError(f"{VOICE_CONFIG_PATH.name}: '{dotted}' must be a non-empty string")
    return node.strip()


def load_voice_config(path: Path = VOICE_CONFIG_PATH) -> VoiceConfig:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise ConfigError(f"Config file not found: {path}") from e
    except json.JSONDecodeError as e:
        raise ConfigError(f"{path.name} is not valid JSON: {e}") from e

    return VoiceConfig(
        stt_model=_require_str(data, "stt.model"),
        llm_model=_require_str(data, "llm.model"),
        tts_model=_require_str(data, "tts.model"),
        system_prompt=_require_str(data, "prompts.system"),
        greeting_prompt=_require_str(data, "prompts.greeting"),
    )


def require_env() -> dict[str, str]:
    """Return the required env vars, or raise naming the missing ones (never the values)."""
    missing = [name for name in REQUIRED_ENV if not os.getenv(name, "").strip()]
    if missing:
        raise ConfigError(
            "Missing or empty in .env: " + ", ".join(missing) + ". Open .env in Notepad and paste the value after the '='."
        )
    return {name: os.environ[name].strip() for name in REQUIRED_ENV}
