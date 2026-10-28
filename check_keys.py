"""Checks each .env value against its service without spending credits.

One read-only call per service: Deepgram lists projects, Gemini looks up the
configured model (metadata only, no generation), ElevenLabs looks up the voice.
Prints OK or FAIL per line. Never prints a key or the voice ID.

Run with:  uv run check_keys.py
"""

import json
import sys
import urllib.error
import urllib.request

from dotenv import load_dotenv
from google import genai
from google.genai import errors as genai_errors

from screener.config import PROJECT_ROOT, ConfigError, load_voice_config, require_env

TIMEOUT_SECS = 15


def _redact(text: str, secrets: list[str]) -> str:
    for s in secrets:
        text = text.replace(s, "<redacted>")
    return text


def _http_get(url: str, headers: dict[str, str]) -> tuple[int, str]:
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECS) as resp:
            return resp.status, ""
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            detail = json.loads(body)
            body = json.dumps(detail.get("detail", detail))
        except json.JSONDecodeError:
            pass
        return e.code, body[:300]


def check_deepgram(env: dict[str, str]) -> tuple[bool, str]:
    status, body = _http_get(
        "https://api.deepgram.com/v1/projects",
        {"Authorization": f"Token {env['DEEPGRAM_API_KEY']}"},
    )
    if status == 200:
        return True, "key accepted"
    return False, f"HTTP {status} {body}"


def check_gemini(env: dict[str, str], model: str) -> tuple[bool, str]:
    client = genai.Client(api_key=env["GOOGLE_API_KEY"])
    try:
        client.models.get(model=model)
        return True, f"key accepted, model '{model}' available"
    except genai_errors.APIError as e:
        return False, f"{e.code} {e.status}: {e.message}"


def check_elevenlabs(env: dict[str, str]) -> tuple[bool, str]:
    status, body = _http_get(
        f"https://api.elevenlabs.io/v1/voices/{env['ELEVENLABS_VOICE_ID']}",
        {"xi-api-key": env["ELEVENLABS_API_KEY"]},
    )
    if status == 200:
        return True, "key accepted, voice ID found on this account"
    if status == 401:
        return False, f"API key rejected or missing Voices permission: HTTP 401 {body}"
    return False, f"voice ID not usable with this key: HTTP {status} {body}"


def main() -> int:
    load_dotenv(PROJECT_ROOT / ".env", override=True)
    try:
        config = load_voice_config()
        env = require_env()
    except ConfigError as e:
        print(f"FAIL  config: {e}")
        return 1

    secrets = list(env.values())
    checks = [
        ("Deepgram  ", lambda: check_deepgram(env)),
        ("Gemini    ", lambda: check_gemini(env, config.llm_model)),
        ("ElevenLabs", lambda: check_elevenlabs(env)),
    ]
    all_ok = True
    for name, run in checks:
        try:
            ok, msg = run()
        except Exception as e:  # network errors etc.
            ok, msg = False, f"{type(e).__name__}: {e}"
        all_ok &= ok
        print(f"{'OK  ' if ok else 'FAIL'}  {name}  {_redact(msg, secrets)}")

    print("Gemini requests used: 0 generation (1 model lookup)")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
