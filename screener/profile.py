"""Turns a saved call transcript into profiles/<call id>/profile.json.

config/profile.schema.json is the single source of truth for field names: the
extraction prompt lists fields by walking the schema, the same schema is sent
to Gemini as the response schema, and the reply is validated against it here.
No field name is written into this module.

build_profile() reads transcript.json only, never call.json, so voice and
simulated calls go through exactly the same code.

Rules enforced in code, after schema validation:
- every evidence turn number must exist in the transcript;
- a value other than the not-discussed marker must cite at least one turn;
- a not-discussed value must cite none;
- evidence that cites only the interviewer's turns is flagged.
Problems are listed under "evidence_errors"; they don't make the profile invalid.
"""

import copy
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from jsonschema import Draft202012Validator

from screener.config import PROJECT_ROOT, ConfigError, read_json, require_str
from screener.postcall import request_valid_json
from screener.transcript import CANDIDATE, now_iso

SCHEMA_PATH = PROJECT_ROOT / "config" / "profile.schema.json"
EXTRACTION_CONFIG_PATH = PROJECT_ROOT / "config" / "extraction.json"
EVIDENCE = "evidence"

# (system prompt, user text, response schema) -> raw model text
Generate = Callable[[str, str, dict], Awaitable[str]]


@dataclass(frozen=True)
class ExtractionConfig:
    model: str
    thinking_level: str
    not_discussed: str
    system_template: str
    field_line_template: str
    transcript_intro: str
    turn_line_template: str


def load_extraction_config(path: Path = EXTRACTION_CONFIG_PATH) -> ExtractionConfig:
    source = path.name
    data = read_json(path)
    lines = data.get("system_template")
    if not isinstance(lines, list) or not all(isinstance(line, str) for line in lines):
        raise ConfigError(f"{source}: 'system_template' must be a list of strings")
    return ExtractionConfig(
        model=require_str(data, "model", source),
        thinking_level=require_str(data, "thinking_level", source),
        not_discussed=require_str(data, "not_discussed_value", source),
        system_template="\n".join(lines),
        field_line_template=require_str(data, "field_line_template", source),
        transcript_intro=require_str(data, "transcript_intro", source),
        turn_line_template=require_str(data, "turn_line_template", source),
    )


def load_schema(path: Path = SCHEMA_PATH) -> dict:
    schema = read_json(path)
    try:
        Draft202012Validator.check_schema(schema)
    except Exception as e:
        raise ConfigError(f"{path.name} is not a valid JSON Schema: {e}") from e
    return schema


def schema_for_gemini(schema: dict) -> dict:
    """The same schema, minus `$schema`, which Gemini's response_json_schema doesn't list."""
    sent = copy.deepcopy(schema)
    sent.pop("$schema", None)
    return sent


def build_extraction_prompt(schema: dict, ext: ExtractionConfig) -> str:
    fields = "\n".join(
        ext.field_line_template.format(
            name=name, title=prop.get("title", name), description=prop.get("description", "")
        )
        for name, prop in schema["properties"].items()
    )
    return ext.system_template.format(fields=fields, not_discussed=ext.not_discussed)


def format_transcript(turns: list[dict], ext: ExtractionConfig) -> str:
    lines = [ext.turn_line_template.format(**t) for t in turns]
    return ext.transcript_intro + "\n" + "\n".join(lines)


def validate_profile(data, schema: dict) -> list[str]:
    errors = sorted(Draft202012Validator(schema).iter_errors(data), key=lambda e: list(e.path))
    return [f"{'/'.join(str(p) for p in e.path) or '(top level)'}: {e.message}" for e in errors]


def _evidence_items(profile: dict, schema: dict):
    """Yields (path, item, item_schema) for every evidence-bearing object, walking the schema."""
    for name, prop in schema["properties"].items():
        value = profile.get(name)
        if prop.get("type") == "object" and EVIDENCE in prop.get("properties", {}):
            yield name, value, prop
        elif prop.get("type") == "array" and EVIDENCE in prop.get("items", {}).get("properties", {}):
            for i, item in enumerate(value or []):
                yield f"{name}[{i}]", item, prop["items"]


def check_evidence(profile: dict, schema: dict, turns: list[dict], not_discussed: str) -> list[dict]:
    speaker_by_turn = {t["turn"]: t["speaker"] for t in turns}
    marker = not_discussed.strip().lower()
    problems: list[dict] = []

    for path, item, item_schema in _evidence_items(profile, schema):
        evidence = item.get(EVIDENCE, [])
        texts = [
            item[key] for key, sub in item_schema["properties"].items()
            if key != EVIDENCE and sub.get("type") == "string" and isinstance(item.get(key), str)
        ]
        makes_a_claim = any(t.strip().lower() != marker for t in texts)

        if makes_a_claim and not evidence:
            problems.append({"field": path, "turn": None, "problem": "states a fact but cites no turn"})
        if not makes_a_claim and evidence:
            problems.append({"field": path, "turn": None, "problem": "marked not discussed but cites turns"})
        missing = [n for n in evidence if n not in speaker_by_turn]
        for n in missing:
            problems.append({"field": path, "turn": n, "problem": "turn does not exist in the transcript"})
        existing = [n for n in evidence if n in speaker_by_turn]
        if existing and not any(speaker_by_turn[n] == CANDIDATE for n in existing):
            problems.append({"field": path, "turn": existing, "problem": "cites only the interviewer's turns"})
    return problems


async def build_profile(
    call_dir: Path,
    generate: Generate,
    schema: dict | None = None,
    ext: ExtractionConfig | None = None,
    error_retries: int = 0,
    error_wait_secs: float = 0.0,
) -> dict:
    """Extracts, validates and saves profile.json in call_dir. Never raises for a bad model reply.

    Retry policy (screener.postcall): an invalid reply is retried once; a request
    that errors is retried up to error_retries times with a growing wait.
    """
    schema = schema if schema is not None else load_schema()
    ext = ext if ext is not None else load_extraction_config()
    turns = json.loads((call_dir / "transcript.json").read_text(encoding="utf-8"))

    outcome = await request_valid_json(
        "Profile", generate,
        build_extraction_prompt(schema, ext), format_transcript(turns, ext), schema_for_gemini(schema),
        validate=lambda data: validate_profile(data, schema),
        error_retries=error_retries, error_wait_secs=error_wait_secs,
    )
    result: dict = {
        "status": "valid" if outcome.valid else "invalid",
        "call_id": call_dir.name,
        "generated_at": now_iso(),
        "model": ext.model,
        "attempts": outcome.attempts,
        "profile": outcome.data,
        "evidence_errors": check_evidence(outcome.data, schema, turns, ext.not_discussed) if outcome.valid else [],
        "validation_errors": outcome.errors,
        "raw_output": outcome.raw_output,
    }
    (call_dir / "profile.json").write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    return result


async def profile_call(call_dir: Path, api_key: str, schema: dict | None = None) -> tuple[dict, int]:
    """The profile step as every caller runs it (voice bot, simulate.py, build_profile.py).

    Returns the saved result and the number of Gemini requests it used.
    """
    from screener.config import load_voice_config
    from screener.llm import JsonGenerator  # local import keeps this module light for offline checks

    ext = load_extraction_config()
    voice = load_voice_config()
    generate = JsonGenerator(api_key, ext.model, ext.thinking_level, voice.llm_max_requests_per_minute)
    result = await build_profile(call_dir, generate, schema=schema, ext=ext,
                                 error_retries=voice.llm_post_call_error_retries,
                                 error_wait_secs=voice.llm_post_call_retry_wait_secs)
    return result, generate.requests


def describe_profile(result: dict, schema: dict) -> list[str]:
    """Human-readable lines for a saved profile result, walking the schema."""
    lines = [f"Profile: {result['status']} (attempts: {result['attempts']})"]
    if result["status"] != "valid":
        lines += [f"  problem: {e}" for e in result["validation_errors"]]
        if result["raw_output"] is not None:
            lines.append("  The model's raw reply is saved in profile.json for inspection.")
        else:
            lines.append("  No model reply to save: the request itself was refused (see problem above).")
        return lines
    profile = result["profile"]
    for name, prop in schema["properties"].items():
        title = prop.get("title", name)
        node = profile.get(name)
        if isinstance(node, list):
            if not node:
                lines.append(f"  {title}: (none)")
            for item in node:
                text = " | ".join(v for k, v in item.items() if k != EVIDENCE and isinstance(v, str))
                lines.append(f"  {title}: {text}  [turns {item.get(EVIDENCE, [])}]")
        elif isinstance(node, dict):
            text = " | ".join(v for k, v in node.items() if k != EVIDENCE and isinstance(v, str))
            lines.append(f"  {title}: {text}  [turns {node.get(EVIDENCE, [])}]")
    if result["evidence_errors"]:
        lines.append(f"  Evidence errors ({len(result['evidence_errors'])}):")
        lines += [f"    {e['field']}: {e['problem']} (turn {e['turn']})" for e in result["evidence_errors"]]
    else:
        lines.append("  Evidence: every cited turn exists and every claim cites one.")
    return lines
