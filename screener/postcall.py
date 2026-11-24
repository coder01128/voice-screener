"""Retry policy for Gemini requests outside the live voice pipeline: the
post-call steps (profile, matching) and simulated conversation turns.

Two different failures, two different rules:
- the model replied but the reply is not valid JSON for the schema: retry
  once, straight away (the spec's rule); a second bad reply means invalid;
- the request itself errored (e.g. 503 "model is experiencing high demand",
  a network drop): wait and retry, up to `error_retries` times, doubling the
  wait each time. These say nothing about the reply, so they don't use up the
  one validation retry.
"""

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from loguru import logger

MAX_INVALID_REPLIES = 2  # the first reply plus one retry


async def with_error_retries(
    label: str,
    call: Callable[[], Awaitable],
    error_retries: int,
    error_wait_secs: float,
):
    """Runs call(), retrying a request that errors with a doubling wait. Re-raises after the last try."""
    request_errors = 0
    while True:
        try:
            return await call()
        except Exception as e:
            request_errors += 1
            logger.warning(f"{label} request failed ({request_errors}): {type(e).__name__}: {str(e)[:160]}")
            if request_errors > error_retries:
                raise
            wait = error_wait_secs * 2 ** (request_errors - 1)
            if wait:
                logger.warning(f"{label}: waiting {wait:.0f}s before retrying")
                await asyncio.sleep(wait)


@dataclass
class Outcome:
    data: object = None
    raw_output: str | None = None
    errors: list[str] = field(default_factory=list)
    attempts: int = 0

    @property
    def valid(self) -> bool:
        return self.data is not None


async def request_valid_json(
    label: str,
    generate: Callable[[str, str, dict], Awaitable[str]],
    system: str,
    user: str,
    schema: dict,
    validate: Callable[[object], list[str]],
    error_retries: int,
    error_wait_secs: float,
) -> Outcome:
    out = Outcome()
    invalid_replies = request_errors = 0
    while True:
        out.attempts += 1
        try:
            raw = await generate(system, user, schema)
        except Exception as e:
            request_errors += 1
            out.errors = [f"request failed: {type(e).__name__}: {e}"]
            logger.warning(f"{label} request {out.attempts} failed: {type(e).__name__}: {str(e)[:160]}")
            if request_errors > error_retries:
                return out
            wait = error_wait_secs * 2 ** (request_errors - 1)
            if wait:
                logger.warning(f"{label}: waiting {wait:.0f}s before retrying")
                await asyncio.sleep(wait)
            continue

        out.raw_output = raw
        try:
            data = json.loads(raw)
            problems = validate(data)
        except json.JSONDecodeError as e:
            problems = [f"reply is not valid JSON: {e}"]
        if not problems:
            out.data, out.errors, out.raw_output = data, [], None
            return out
        invalid_replies += 1
        out.errors = problems
        if invalid_replies >= MAX_INVALID_REPLIES:
            return out
