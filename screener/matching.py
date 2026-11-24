"""Matches a call against the roles in config/roles.json and saves matches.json.

Gemini only rates requirements (met / partly met / not met / unknown), with
evidence turns and a one-line reason. The recommendation is computed here, in
code, by the spec's rule:
    any must-have not met  -> not a fit
    any must-have unknown  -> follow up, with that requirement's question
    otherwise              -> advance
A rating other than unknown that cites no turn of the candidate's counts as
unknown for the rule (and is flagged): the model can't move a candidate either
way on a claim it can't point to.

The reply schema is generated from roles.json, so Gemini must rate every
requirement of every role, using only the allowed ratings. Like the profile
step, this reads transcript.json only, so voice and simulated calls are
matched by the same code. Every recommendation starts as pending; a person
approves or rejects it (Ticket 05).
"""

import json
from dataclasses import dataclass
from pathlib import Path

from jsonschema import Draft202012Validator

from screener.config import PROJECT_ROOT, ConfigError, read_json, require_str
from screener.postcall import request_valid_json
from screener.profile import Generate, format_transcript, load_extraction_config
from screener.transcript import CANDIDATE, now_iso

ROLES_PATH = PROJECT_ROOT / "config" / "roles.json"
MATCHING_CONFIG_PATH = PROJECT_ROOT / "config" / "matching.json"
RATING_KEYS = ("met", "partly_met", "not_met", "unknown")
OUTCOME_KEYS = ("advance", "follow_up", "not_a_fit")


@dataclass(frozen=True)
class Requirement:
    id: str
    text: str
    follow_up_question: str
    must_have: bool


@dataclass(frozen=True)
class Role:
    id: str
    title: str
    summary: str
    requirements: tuple[Requirement, ...]


@dataclass(frozen=True)
class MatchingConfig:
    model: str
    thinking_level: str
    ratings: dict[str, str]   # key -> label, e.g. "not_met" -> "not met"
    outcomes: dict[str, str]  # key -> label, e.g. "follow_up" -> "follow up"
    pending_status: str
    system_template: str
    role_template: str
    requirement_line_template: str


def load_matching_config(path: Path = MATCHING_CONFIG_PATH) -> MatchingConfig:
    source = path.name
    data = read_json(path)
    ratings, outcomes = data.get("ratings", {}), data.get("outcomes", {})
    if sorted(ratings) != sorted(RATING_KEYS) or not all(isinstance(v, str) and v for v in ratings.values()):
        raise ConfigError(f"{source}: 'ratings' must give a label for each of {', '.join(RATING_KEYS)}")
    if sorted(outcomes) != sorted(OUTCOME_KEYS) or not all(isinstance(v, str) and v for v in outcomes.values()):
        raise ConfigError(f"{source}: 'outcomes' must give a label for each of {', '.join(OUTCOME_KEYS)}")
    lines = data.get("system_template")
    if not isinstance(lines, list) or not all(isinstance(line, str) for line in lines):
        raise ConfigError(f"{source}: 'system_template' must be a list of strings")
    return MatchingConfig(
        model=require_str(data, "model", source),
        thinking_level=require_str(data, "thinking_level", source),
        ratings=dict(ratings),
        outcomes=dict(outcomes),
        pending_status=require_str(data, "pending_status", source),
        system_template="\n".join(lines),
        role_template=require_str(data, "role_template", source),
        requirement_line_template=require_str(data, "requirement_line_template", source),
    )


def _load_requirements(raw: list, must_have: bool, where: str) -> list[Requirement]:
    if not isinstance(raw, list):
        raise ConfigError(f"{where} must be a list")
    reqs = []
    for i, r in enumerate(raw):
        src = f"{where}[{i}]"
        reqs.append(Requirement(
            id=require_str(r, "id", src),
            text=require_str(r, "text", src),
            follow_up_question=require_str(r, "follow_up_question", src),
            must_have=must_have,
        ))
    return reqs


def load_roles(path: Path = ROLES_PATH) -> list[Role]:
    source = path.name
    raw_roles = read_json(path).get("roles")
    if not isinstance(raw_roles, list) or not raw_roles:
        raise ConfigError(f"{source}: 'roles' must be a non-empty list")
    roles = []
    for i, r in enumerate(raw_roles):
        where = f"{source} roles[{i}]"
        reqs = _load_requirements(r.get("must_haves"), True, f"{where}.must_haves")
        reqs += _load_requirements(r.get("nice_to_haves", []), False, f"{where}.nice_to_haves")
        if not any(q.must_have for q in reqs):
            raise ConfigError(f"{where}: needs at least one must-have")
        ids = [q.id for q in reqs]
        if len(set(ids)) != len(ids):
            raise ConfigError(f"{where}: requirement ids must be unique")
        roles.append(Role(
            id=require_str(r, "id", where),
            title=require_str(r, "title", where),
            summary=require_str(r, "summary", where),
            requirements=tuple(reqs),
        ))
    if len({r.id for r in roles}) != len(roles):
        raise ConfigError(f"{source}: role ids must be unique")
    return roles


def reply_schema(roles: list[Role], cfg: MatchingConfig) -> dict:
    """JSON Schema for Gemini's ratings, generated from roles.json."""
    rating = {
        "type": "object",
        "additionalProperties": False,
        "required": ["rating", "evidence", "reason"],
        "properties": {
            "rating": {"type": "string", "enum": [cfg.ratings[k] for k in RATING_KEYS]},
            "evidence": {"type": "array", "items": {"type": "integer"}},
            "reason": {"type": "string"},
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [r.id for r in roles],
        "properties": {
            r.id: {
                "type": "object",
                "additionalProperties": False,
                "required": [q.id for q in r.requirements],
                "properties": {q.id: rating for q in r.requirements},
            }
            for r in roles
        },
    }


def build_matching_prompt(roles: list[Role], cfg: MatchingConfig) -> str:
    def lines(must: bool, role: Role) -> str:
        return "\n".join(
            cfg.requirement_line_template.format(id=q.id, text=q.text)
            for q in role.requirements if q.must_have == must
        ) or cfg.requirement_line_template.format(id="-", text="-")

    role_text = "\n".join(
        cfg.role_template.format(id=r.id, title=r.title, summary=r.summary,
                                 must_haves=lines(True, r), nice_to_haves=lines(False, r))
        for r in roles
    )
    return cfg.system_template.format(roles=role_text, **cfg.ratings)


def recommend(role: Role, effective: dict[str, str], cfg: MatchingConfig) -> tuple[str, list[str]]:
    """The spec's rule, applied to must-haves. Returns (outcome label, follow-up questions)."""
    musts = [q for q in role.requirements if q.must_have]
    if any(effective[q.id] == cfg.ratings["not_met"] for q in musts):
        return cfg.outcomes["not_a_fit"], []
    unknown = [q for q in musts if effective[q.id] == cfg.ratings["unknown"]]
    if unknown:
        return cfg.outcomes["follow_up"], [q.follow_up_question for q in unknown]
    return cfg.outcomes["advance"], []


def _evidence_problems(rating: dict, speaker_by_turn: dict[int, str], unknown: str) -> list[str]:
    evidence = rating["evidence"]
    problems = [f"turn {n} does not exist in the transcript" for n in evidence if n not in speaker_by_turn]
    candidate_turns = [n for n in evidence if speaker_by_turn.get(n) == CANDIDATE]
    if rating["rating"] == unknown:
        if evidence:
            problems.append("rated unknown but cites turns")
    elif not candidate_turns:
        problems.append("rating cites no turn of the candidate's; treated as unknown")
    return problems


def score(ratings: dict, roles: list[Role], turns: list[dict], cfg: MatchingConfig) -> tuple[list[dict], list[dict]]:
    """Turns validated ratings into per-role results. Returns (roles, evidence_errors)."""
    speaker_by_turn = {t["turn"]: t["speaker"] for t in turns}
    unknown = cfg.ratings["unknown"]
    results, errors = [], []
    for role in roles:
        reqs, effective = [], {}
        for q in role.requirements:
            r = ratings[role.id][q.id]
            problems = _evidence_problems(r, speaker_by_turn, unknown)
            unsupported = r["rating"] != unknown and not any(
                speaker_by_turn.get(n) == CANDIDATE for n in r["evidence"])
            effective[q.id] = unknown if unsupported else r["rating"]
            errors += [{"role": role.id, "requirement": q.id, "problem": p} for p in problems]
            reqs.append({
                "id": q.id,
                "text": q.text,
                "must_have": q.must_have,
                "rating": r["rating"],
                "rating_used": effective[q.id],
                "evidence": r["evidence"],
                "reason": r["reason"],
            })
        outcome, questions = recommend(role, effective, cfg)
        results.append({
            "role_id": role.id,
            "title": role.title,
            "recommendation": {
                "outcome": outcome,
                "follow_up_questions": questions,
                "status": cfg.pending_status,
                "decided_at": None,
            },
            "requirements": reqs,
        })
    return results, errors


async def build_matches(
    call_dir: Path,
    generate: Generate,
    roles: list[Role] | None = None,
    cfg: MatchingConfig | None = None,
    error_retries: int = 0,
    error_wait_secs: float = 0.0,
) -> dict:
    """Rates, scores and saves matches.json in call_dir. Never raises for a bad model reply.

    Retry policy (screener.postcall): an invalid reply is retried once; a request
    that errors is retried up to error_retries times with a growing wait.
    """
    roles = roles if roles is not None else load_roles()
    cfg = cfg if cfg is not None else load_matching_config()
    turns = json.loads((call_dir / "transcript.json").read_text(encoding="utf-8"))
    schema = reply_schema(roles, cfg)
    validator = Draft202012Validator(schema)

    outcome = await request_valid_json(
        "Matching", generate,
        build_matching_prompt(roles, cfg), format_transcript(turns, load_extraction_config()), schema,
        validate=lambda data: [f"{'/'.join(str(p) for p in e.path) or '(top level)'}: {e.message}"
                               for e in validator.iter_errors(data)],
        error_retries=error_retries, error_wait_secs=error_wait_secs,
    )
    scored, evidence_errors = score(outcome.data, roles, turns, cfg) if outcome.valid else ([], [])
    result: dict = {
        "status": "valid" if outcome.valid else "invalid",
        "call_id": call_dir.name,
        "generated_at": now_iso(),
        "model": cfg.model,
        "attempts": outcome.attempts,
        "roles": scored,
        "evidence_errors": evidence_errors,
        "validation_errors": outcome.errors,
        "raw_output": outcome.raw_output,
    }
    (call_dir / "matches.json").write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    return result


async def match_call(call_dir: Path, api_key: str, roles: list[Role] | None = None) -> tuple[dict, int]:
    """The matching step as every caller runs it. Returns (saved result, Gemini requests used)."""
    from screener.config import load_voice_config
    from screener.llm import JsonGenerator

    cfg = load_matching_config()
    voice = load_voice_config()
    generate = JsonGenerator(api_key, cfg.model, cfg.thinking_level, voice.llm_max_requests_per_minute)
    result = await build_matches(call_dir, generate, roles=roles, cfg=cfg,
                                 error_retries=voice.llm_post_call_error_retries,
                                 error_wait_secs=voice.llm_post_call_retry_wait_secs)
    return result, generate.requests


def describe_matches(result: dict) -> list[str]:
    lines = [f"Matches: {result['status']} (attempts: {result['attempts']})"]
    if result["status"] != "valid":
        lines += [f"  problem: {e}" for e in result["validation_errors"]]
        return lines
    for role in result["roles"]:
        rec = role["recommendation"]
        lines.append(f"  {role['title']}: {rec['outcome'].upper()} ({rec['status']})")
        for q in role["requirements"]:
            kind = "must" if q["must_have"] else "nice"
            used = "" if q["rating_used"] == q["rating"] else f" -> counted as {q['rating_used']}"
            lines.append(f"    [{kind}] {q['id']}: {q['rating']}{used} [turns {q['evidence']}] {q['reason']}")
        for question in rec["follow_up_questions"]:
            lines.append(f"    Follow-up question: {question}")
    for e in result["evidence_errors"]:
        lines.append(f"  Evidence note: {e['role']}/{e['requirement']}: {e['problem']}")
    return lines
