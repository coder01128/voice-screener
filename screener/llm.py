"""Gemini services, built in one place for the voice bot and simulate.py.

interviewer_llm() is the only way the interviewer's Gemini service is made, so
a simulated call sends Gemini exactly what a voice call does: same model,
system prompt, thinking level, retry settings and request conversion.

reply() collects one response outside a pipeline. It goes through Pipecat's
own request path (GoogleLLMService._stream_response, which includes the
slow-start retry), so simulate.py doesn't rebuild requests by hand.
tests/offline_checks.py exercises this path, so a Pipecat change that breaks
it fails there first.
"""

from dataclasses import dataclass, field

from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.services.google.llm import GoogleLLMService

from screener.config import VoiceConfig
from screener.interview import InterviewConfig, build_interviewer_prompt


def interviewer_llm(voice: VoiceConfig, interview: InterviewConfig, api_key: str) -> GoogleLLMService:
    return GoogleLLMService(
        api_key=api_key,
        settings=GoogleLLMService.Settings(
            model=voice.llm_model,
            system_instruction=build_interviewer_prompt(interview),
        ),
        retry_on_timeout=voice.llm_retry_if_slow_to_start,
        retry_timeout_secs=voice.llm_first_reply_timeout_secs,
    )


def persona_llm(model: str, system_prompt: str, api_key: str) -> GoogleLLMService:
    # Not latency-critical, so no slow-start retry: it would only spend requests.
    return GoogleLLMService(
        api_key=api_key,
        settings=GoogleLLMService.Settings(model=model, system_instruction=system_prompt),
    )


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
