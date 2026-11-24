"""Checks a profile against a persona's ground_truth. The checker is the only
reader of ground_truth; the persona-playing model never sees it.

ground_truth uses the schema's field names (validated against
config/profile.schema.json). Each entry:
    {"fact": "what the profile should capture",
     "all_of": ["regex", ...],    every pattern must match the field's text
     "none_of": ["regex", ...]}   no pattern may match it
Patterns are case-insensitive; "a|b" means either. A field's text is every
string in it (value, skill names and examples, questions), not the evidence.
"""

import re
from dataclasses import dataclass

from screener.config import ConfigError, read_json
from screener.personas import PERSONAS_DIR
from screener.profile import EVIDENCE


@dataclass(frozen=True)
class FieldCheck:
    field: str
    passed: bool
    reason: str


def load_ground_truth(persona_id: str, schema: dict) -> dict:
    path = PERSONAS_DIR / f"{persona_id}.json"
    truth = read_json(path).get("ground_truth")
    if not isinstance(truth, dict) or not truth:
        raise ConfigError(f"{path.name}: no 'ground_truth' section")
    unknown = [name for name in truth if name not in schema["properties"]]
    if unknown:
        raise ConfigError(f"{path.name}: ground_truth fields not in profile.schema.json: {', '.join(unknown)}")
    for name, rule in truth.items():
        if not isinstance(rule, dict) or not isinstance(rule.get("fact"), str):
            raise ConfigError(f"{path.name}: ground_truth.{name} needs a 'fact'")
        for key in ("all_of", "none_of"):
            for pattern in rule.get(key, []):
                try:
                    re.compile(pattern)
                except re.error as e:
                    raise ConfigError(f"{path.name}: ground_truth.{name}.{key} bad pattern {pattern!r}: {e}") from e
    return truth


EXPECTED_PATH = PERSONAS_DIR / "expected.json"


def load_expected(persona_id: str, role_ids: set[str], outcome_labels: set[str]) -> dict:
    """The persona's expected outcomes, checked against roles.json and the outcome vocabulary."""
    expected = read_json(EXPECTED_PATH).get(persona_id)
    if not isinstance(expected, dict) or not expected:
        raise ConfigError(f"{EXPECTED_PATH.name}: no expected outcomes for '{persona_id}'")
    unknown_roles = [r for r in expected if r not in role_ids]
    if unknown_roles:
        raise ConfigError(f"{EXPECTED_PATH.name}: role ids not in roles.json: {', '.join(unknown_roles)}")
    bad = [f"{r}={e.get('outcome')!r}" for r, e in expected.items() if e.get("outcome") not in outcome_labels]
    if bad:
        raise ConfigError(f"{EXPECTED_PATH.name}: outcomes not in matching.json: {', '.join(bad)}")
    return expected


def check_against_expected(matches: dict, expected: dict) -> list[FieldCheck]:
    by_role = {r["role_id"]: r for r in matches.get("roles", [])}
    checks = []
    for role_id, want in expected.items():
        rec = by_role[role_id]["recommendation"] if role_id in by_role else None
        if rec is None:
            checks.append(FieldCheck(role_id, False, "role missing from matches"))
            continue
        problems = []
        if rec["outcome"] != want["outcome"]:
            problems.append(f"got {rec['outcome']!r}, expected {want['outcome']!r}")
        mention = want.get("follow_up_must_mention")
        if mention and not any(mention.lower() in q.lower() for q in rec["follow_up_questions"]):
            problems.append(f"no follow-up question mentions {mention!r}")
        detail = f"{rec['outcome']}" + (f"; asks: {' / '.join(rec['follow_up_questions'])}" if rec["follow_up_questions"] else "")
        checks.append(FieldCheck(role_id, not problems, "; ".join(problems) if problems else detail))
    return checks


def field_text(profile: dict, name: str) -> str:
    def strings(node) -> list[str]:
        if isinstance(node, str):
            return [node]
        if isinstance(node, dict):
            return [s for key, v in node.items() if key != EVIDENCE for s in strings(v)]
        if isinstance(node, list):
            return [s for v in node for s in strings(v)]
        return []

    return " | ".join(strings(profile.get(name)))


def check_against_ground_truth(profile: dict, truth: dict) -> list[FieldCheck]:
    checks = []
    for name, rule in truth.items():
        text = field_text(profile, name)
        missing = [p for p in rule.get("all_of", []) if not re.search(p, text, re.IGNORECASE)]
        forbidden = [p for p in rule.get("none_of", []) if re.search(p, text, re.IGNORECASE)]
        if missing or forbidden:
            parts = []
            if missing:
                parts.append("missing " + ", ".join(repr(p) for p in missing))
            if forbidden:
                parts.append("should not contain " + ", ".join(repr(p) for p in forbidden))
            checks.append(FieldCheck(name, False, f"{'; '.join(parts)}. Expected: {rule['fact']}. Got: {text!r}"))
        else:
            checks.append(FieldCheck(name, True, rule["fact"]))
    return checks
