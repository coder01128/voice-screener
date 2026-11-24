"""Text-only screening calls: the interviewer talks to a persona, no voice.

Usage:
    uv run python simulate.py --persona thandi
    uv run python simulate.py --all

Uses Gemini only (no Deepgram, no ElevenLabs). The interviewer is built by the
same code as the voice bot. Each run saves profiles/<timestamp>-sim-<persona>/
with transcript, profile and role matches, checks them against the persona's
ground truth and config/personas/expected.json, and prints how many Gemini
requests it used (also appended to logs/usage.csv). Run it once per change, not in a loop: the free tier is the
budget.
"""

import argparse
import asyncio
import sys
import time
from datetime import datetime

from dotenv import load_dotenv
from loguru import logger

from pipecat.processors.aggregators.llm_context import LLMContext

from screener.call_end import end_interview_schema
from screener.config import PROJECT_ROOT, ConfigError, load_voice_config, require_env
from screener.ground_truth import check_against_expected, check_against_ground_truth, load_expected, load_ground_truth
from screener.interview import load_interview_config
from screener.llm import Reply, RequestCounter, interviewer_llm, persona_llm, reply
from screener.matching import describe_matches, load_matching_config, load_roles, match_call
from screener.personas import build_persona_prompt, list_personas, load_card, load_simulation_config
from screener.postcall import with_error_retries
from screener.profile import describe_profile, load_schema, profile_call
from screener.simulation import run_simulated_call
from screener.transcript import INTERVIEWER, now_iso
from screener.usage import append_usage_row, usage_row

SIM_LOG_PATH = PROJECT_ROOT / "logs" / "simulate.log"


def setup_logging():
    logger.remove()
    logger.add(sys.stderr, level="WARNING")
    logger.add(SIM_LOG_PATH, level="DEBUG", rotation="5 MB", retention=5, encoding="utf-8")


async def simulate_one(persona_id: str, api_key: str) -> tuple[int, bool, bool]:
    """Runs one simulated call, its profile and checks.

    Returns (Gemini requests used, ran to the end, every check passed).
    """
    voice = load_voice_config()
    interview = load_interview_config()
    sim = load_simulation_config()
    card = load_card(persona_id)  # the card section only; never ground_truth

    interviewer = interviewer_llm(voice, interview, api_key)
    persona = persona_llm(sim.persona_model, build_persona_prompt(card, sim), api_key,
                          voice.llm_max_requests_per_minute)
    interviewer_requests = RequestCounter(interviewer)
    persona_requests = RequestCounter(persona)
    end_tool = end_interview_schema(interview.prompt.end_tool_name, interview.prompt.end_tool_description)

    retries, wait = voice.llm_post_call_error_retries, voice.llm_post_call_retry_wait_secs

    async def interviewer_turn(messages: list[dict]) -> Reply:
        return await with_error_retries(
            "Interviewer", lambda: reply(interviewer, LLMContext(messages=messages, tools=[end_tool])), retries, wait)

    async def persona_turn(messages: list[dict]) -> str:
        r = await with_error_retries("Persona", lambda: reply(persona, LLMContext(messages=messages)), retries, wait)
        return r.text

    def show(speaker: str, text: str):
        who = interview.agent_name.upper() if speaker == INTERVIEWER else card["name"].upper()
        print(f"\n{who}: {text}", flush=True)

    def requests_used() -> int:
        return interviewer_requests.count + persona_requests.count

    call_id = f"{datetime.now():%Y%m%d-%H%M%S}-sim-{persona_id}"
    print(f"\n=== Simulated call {call_id} (persona: {card['name']}) ===", flush=True)
    started_at, started = now_iso(), time.monotonic()

    def log_usage(gemini_requests: int):
        # No voice here, so no Deepgram audio, no ElevenLabs characters and no reply latency.
        append_usage_row(usage_row(
            call_id=call_id, mode="simulated", started_at=started_at,
            call_seconds=time.monotonic() - started, deepgram_audio_seconds=0.0,
            elevenlabs_characters=0, gemini_requests=gemini_requests, latencies_secs=[],
        ))

    try:
        result = await run_simulated_call(interview, sim, interviewer_turn, persona_turn, on_turn=show)
    except Exception as e:
        print(f"\nFAILED: {type(e).__name__}: {e}")
        print(f"Gemini requests used: {requests_used()} "
              f"(interviewer {interviewer_requests.count}, persona {persona_requests.count})")
        log_usage(requests_used())
        return requests_used(), False, False

    out_dir = result.transcript.save(
        call_id,
        {
            "call_id": call_id,
            "mode": "simulated",
            "persona": persona_id,
            "started_at": started_at,
            "ended_at": now_iso(),
            "duration_secs": round(time.monotonic() - started, 1),
            "end_reason": result.end_reason,
            "max_interviewer_turns": sim.max_interviewer_turns,
            "gemini_requests": {
                "interviewer": interviewer_requests.count,
                "persona": persona_requests.count,
                "total": requests_used(),
            },
        },
    )
    print(f"\n--- {card['name']}: {len(result.transcript.turns)} turns, ended: {result.end_reason}")
    print(f"Saved: {out_dir}")

    schema = load_schema()
    profile_result, profile_requests = await profile_call(out_dir, api_key, schema=schema)
    print()
    print("\n".join(describe_profile(profile_result, schema)))

    checks_ok = profile_result["status"] == "valid"
    if checks_ok:
        checks = check_against_ground_truth(profile_result["profile"], load_ground_truth(persona_id, schema))
        print(f"\nGround-truth check ({card['name']}):")
        for c in checks:
            print(f"  {'PASS' if c.passed else 'FAIL'}  {c.field}: {c.reason}")
        checks_ok = all(c.passed for c in checks) and not profile_result["evidence_errors"]
        print(f"  => {'all fields pass' if checks_ok else 'some checks failed'}")

    roles, matching_cfg = load_roles(), load_matching_config()
    matches, match_requests = await match_call(out_dir, api_key, roles=roles)
    print()
    print("\n".join(describe_matches(matches)))
    matches_ok = matches["status"] == "valid"
    if matches_ok:
        expected = load_expected(persona_id, {r.id for r in roles}, set(matching_cfg.outcomes.values()))
        print(f"\nExpected-outcome check ({card['name']}):")
        outcome_checks = check_against_expected(matches, expected)
        for c in outcome_checks:
            print(f"  {'PASS' if c.passed else 'FAIL'}  {c.field}: {c.reason}")
        matches_ok = all(c.passed for c in outcome_checks)
        print(f"  => {'all outcomes as expected' if matches_ok else 'some outcomes differ'}")

    total = requests_used() + profile_requests + match_requests
    print(f"\nGemini requests used: {total} (interviewer {interviewer_requests.count}, "
          f"persona {persona_requests.count}, profile {profile_requests}, matching {match_requests})")
    log_usage(total)
    return total, True, checks_ok and matches_ok


async def main() -> int:
    parser = argparse.ArgumentParser(description="Run text-only simulated screening calls.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--persona", help=f"one of: {', '.join(list_personas())}")
    group.add_argument("--all", action="store_true", help="run every persona once")
    args = parser.parse_args()

    setup_logging()
    load_dotenv(PROJECT_ROOT / ".env", override=True)
    try:
        api_key = require_env(("GOOGLE_API_KEY",))["GOOGLE_API_KEY"]
        persona_ids = list_personas() if args.all else [args.persona]
        for pid in persona_ids:
            load_card(pid)  # fail fast on a bad name before spending any request
    except ConfigError as e:
        print(f"ERROR: {e}")
        return 1

    total = 0
    outcomes: dict[str, str] = {}
    for pid in persona_ids:
        used, ran, checks_ok = await simulate_one(pid, api_key)
        total += used
        outcomes[pid] = "all checks pass" if checks_ok else ("checks failed" if ran else "run failed")
        if not ran:
            print("Stopping here so a failing run doesn't keep spending requests.")
            break
    if len(persona_ids) > 1:
        print("\n=== Summary ===")
        for pid, outcome in outcomes.items():
            print(f"  {pid}: {outcome}")
        print(f"=== Total Gemini requests this run: {total} ===")
    return 0 if outcomes and all(o == "all checks pass" for o in outcomes.values()) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
