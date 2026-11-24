"""Gemini services, built in one place for the voice bot and simulate.py.

interviewer_llm() is the only way the interviewer's Gemini service is made, so
a simulated call sends Gemini exactly what a voice call does: same model,
system prompt, thinking level, retry settings and request conversion.

reply() collects one response outside a pipeline. It goes through Pipecat's
own request path (GoogleLLMService._stream_response, which includes the
slow-start retry), so simulate.py doesn't rebuild requests by hand.
tests/offline_checks.py exercises this path, so a Pipecat change that breaks
it fails there first.

Every Gemini request made here waits for a slot from a per-model pacer, so a
process never exceeds the free tier's requests-per-minute cap (it waits
instead of getting a 429). The wait happens before Pipecat's slow-start retry
timer starts, so pacing never triggers a retry.
"""

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field

from google import genai
from google.genai import types
from loguru import logger

from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.services.google.llm import GoogleLLMService

from screener.config import VoiceConfig
from screener.interview import InterviewConfig, build_interviewer_prompt


class RateLimiter:
    """At most `per_window` requests in any rolling `window_secs`."""

    def __init__(self, per_window: int, window_secs: float = 60.0):
        self.per_window = per_window
        self.window_secs = window_secs
        self._sent: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self):
        async with self._lock:
            while True:
                now = time.monotonic()
                while self._sent and now - self._sent[0] >= self.window_secs:
                    self._sent.popleft()
                if len(self._sent) < self.per_window:
                    self._sent.append(now)
                    return
                wait = self.window_secs - (now - self._sent[0]) + 0.05
                logger.warning(
                    f"Pausing {wait:.0f}s to stay under {self.per_window} Gemini requests per minute (free tier)"
                )
                await asyncio.sleep(wait)


_limiters: dict[str, RateLimiter] = {}


def limiter_for(model: str, per_minute: int) -> RateLimiter:
    """One shared pacer per model: Google counts the cap per project per model."""
    if model not in _limiters:
        _limiters[model] = RateLimiter(per_minute)
    return _limiters[model]


def _paced(llm: GoogleLLMService, limiter: RateLimiter) -> GoogleLLMService:
    original = llm._stream_content

    async def paced(context):
        await limiter.acquire()
        return await original(context)

    llm._stream_content = paced
    return llm


def interviewer_llm(voice: VoiceConfig, interview: InterviewConfig, api_key: str) -> GoogleLLMService:
    llm = GoogleLLMService(
        api_key=api_key,
        settings=GoogleLLMService.Settings(
            model=voice.llm_model,
            system_instruction=build_interviewer_prompt(interview),
        ),
        retry_on_timeout=voice.llm_retry_if_slow_to_start,
        retry_timeout_secs=voice.llm_first_reply_timeout_secs,
    )
    return _paced(llm, limiter_for(voice.llm_model, voice.llm_max_requests_per_minute))


def persona_llm(model: str, system_prompt: str, api_key: str, max_per_minute: int) -> GoogleLLMService:
    # Not latency-critical, so no slow-start retry: it would only spend requests.
    llm = GoogleLLMService(
        api_key=api_key,
        settings=GoogleLLMService.Settings(model=model, system_instruction=system_prompt),
    )
    return _paced(llm, limiter_for(model, max_per_minute))


class JsonGenerator:
    """Post-call structured output: one Gemini request returning JSON for a schema.

    Not part of a voice pipeline, so it calls google-genai directly with
    response_json_schema. No temperature: Google lists it as deprecated and
    ignored for Gemini 3.5 Flash-Lite.
    """

    def __init__(self, api_key: str, model: str, thinking_level: str, max_per_minute: int):
        self._client = genai.Client(api_key=api_key)
        self._model = model
        self._thinking_level = thinking_level
        self._limiter = limiter_for(model, max_per_minute)
        self.requests = 0

    async def __call__(self, system: str, user: str, response_schema: dict) -> str:
        await self._limiter.acquire()
        self.requests += 1
        response = await self._client.aio.models.generate_content(
            model=self._model,
            contents=user,
            config=types.GenerateContentConfig(
                system_instruction=system,
                response_mime_type="application/json",
                response_json_schema=response_schema,
                thinking_config=types.ThinkingConfig(thinking_level=self._thinking_level),
            ),
        )
        return response.text or ""


@dataclass
class Reply:
    text: str = ""
    function_calls: list[str] = field(default_factory=list)


class RequestCounter:
    """Counts Gemini requests sent by one service, retries included."""

    def __init__(self, llm: GoogleLLMService):
        self.count = 0
        original = llm._stream_content

        async def counted(context):
            self.count += 1
            return await original(context)

        llm._stream_content = counted


async def reply(llm: GoogleLLMService, context: LLMContext) -> Reply:
    out = Reply()
    parts_text: list[str] = []
    async for chunk in llm._stream_response(context):
        for candidate in chunk.candidates or []:
            if not candidate.content:
                continue
            for part in candidate.content.parts or []:
                if getattr(part, "thought", False):
                    continue
                if part.function_call and part.function_call.name:
                    out.function_calls.append(part.function_call.name)
                elif part.text:
                    parts_text.append(part.text)
    out.text = "".join(parts_text).strip()
    return out
