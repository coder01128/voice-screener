"""Text-only screening calls: the interviewer talks to a persona, no voice.

Usage:
    uv run python simulate.py --persona thandi
    uv run python simulate.py --all

Uses Gemini only (no Deepgram, no ElevenLabs). The interviewer is built by the
same code as the voice bot. Each run saves profiles/<timestamp>-sim-<persona>/
and prints how many Gemini requests it used. Run it once per change, not in a
loop: the free tier is the budget.
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
from screener.interview import load_interview_config
from screener.llm import Reply, RequestCounter, interviewer_llm, persona_llm, reply
from screener.personas import build_persona_prompt, list_personas, load_card, load_simulation_config
from screener.simulation import run_simulated_call
from screener.transcript import INTERVIEWER, now_iso

SIM_LOG_PATH = PROJECT_ROOT / "logs" / "simulate.log"


def setup_logging():
    logger.remove()
    logger.add(sys.stderr, level="WARNING")
    logger.add(SIM_LOG_PATH, level="DEBUG", rotation="5 MB", retention=5, encoding="utf-8")


async def simulate_one(persona_id: str, api_key: str) -> tuple[int, bool]:
    """Runs one simulated call. Returns (Gemini requests used, succeeded)."""
    voice = load_voice_config()
    interview = load_interview_config()
    sim = load_simulation_config()
    card = load_card(persona_id)  # the card section only; never ground_truth

    interviewer = interviewer_llm(voice, interview, api_key)
    persona = persona_llm(sim.persona_model, build_persona_prompt(card, sim), api_key)
    interviewer_requests = RequestCounter(interviewer)
    persona_requests = RequestCounter(persona)
    end_tool = end_interview_schema(interview.prompt.end_tool_name, interview.prompt.end_tool_description)

    async def interviewer_turn(messages: list[dict]) -> Reply:
        return await reply(interviewer, LLMContext(messages=messages, tools=[end_tool]))

    async def persona_turn(messages: list[dict]) -> str:
        return (await reply(persona, LLMContext(messages=messages))).text

    def show(speaker: str, text: str):
        who = interview.agent_name.upper() if speaker == INTERVIEWER else card["name"].upper()
        print(f"\n{who}: {text}", flush=True)

    def requests_used() -> int:
        return interviewer_requests.count + persona_requests.count

    call_id = f"{datetime.now():%Y%m%d-%H%M%S}-sim-{persona_id}"
    print(f"\n=== Simulated call {call_id} (persona: {card['name']}) ===", flush=True)
    started_at, started = now_iso(), time.monotonic()
    try:
        result = await run_simulated_call(interview, sim, interviewer_turn, persona_turn, on_turn=show)
    except Exception as e:
        print(f"\nFAILED: {type(e).__name__}: {e}")
        print(f"Gemini requests used: {requests_used()} "
              f"(interviewer {interviewer_requests.count}, persona {persona_requests.count})")
        return requests_used(), False

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
    print(f"Gemini requests used: {requests_used()} "
          f"(interviewer {interviewer_requests.count}, persona {persona_requests.count})")
    return requests_used(), True


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
    for pid in persona_ids:
        used, ok = await simulate_one(pid, api_key)
        total += used
        if not ok:
            print("Stopping here so a failing run doesn't keep spending requests.")
            break
    if len(persona_ids) > 1:
        print(f"\n=== Total Gemini requests this run: {total} ===")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
