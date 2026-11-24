"""A text-only screening call between the interviewer and a persona.

The interviewer is the voice bot's: same system prompt, opening line, end tool
and Gemini service (screener.llm.interviewer_llm). Endings mirror the voice
call: the end tool adds the closing line; the turn limit (standing in for the
5-minute limit) adds the time-limit goodbye. The transcript goes through the
same TranscriptRecorder, so it is identical in format to a voice transcript.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from screener.call_end import END_REASON_COMPLETE, END_REASON_TIME_LIMIT
from screener.interview import InterviewConfig, build_opening_line
from screener.llm import Reply
from screener.personas import SimulationConfig
from screener.transcript import CANDIDATE, INTERVIEWER, TranscriptRecorder, now_iso

END_REASON_NO_INTERVIEWER_REPLY = "interviewer_no_reply"
END_REASON_NO_PERSONA_REPLY = "persona_no_reply"

# Each takes the conversation from its own side ("assistant" = itself).
InterviewerTurn = Callable[[list[dict]], Awaitable[Reply]]
PersonaTurn = Callable[[list[dict]], Awaitable[str]]


@dataclass
class SimulatedCall:
    transcript: TranscriptRecorder
    end_reason: str
    interviewer_turns: int


async def run_simulated_call(
    interview: InterviewConfig,
    sim: SimulationConfig,
    interviewer_turn: InterviewerTurn,
    persona_turn: PersonaTurn,
    on_turn: Callable[[str, str], None] = lambda speaker, text: None,
) -> SimulatedCall:
    transcript = TranscriptRecorder()
    interviewer_view: list[dict] = []
    persona_view: list[dict] = []

    def interviewer_says(text: str):
        transcript.add(INTERVIEWER, text, now_iso())
        interviewer_view.append({"role": "assistant", "content": text})
        persona_view.append({"role": "user", "content": text})
        on_turn(INTERVIEWER, text)

    def candidate_says(text: str):
        transcript.add(CANDIDATE, text, now_iso())
        interviewer_view.append({"role": "user", "content": text})
        persona_view.append({"role": "assistant", "content": text})
        on_turn(CANDIDATE, text)

    interviewer_says(build_opening_line(interview))
    turns = 1

    while True:
        answer = (await persona_turn(list(persona_view))).strip()
        if not answer:
            return SimulatedCall(transcript, END_REASON_NO_PERSONA_REPLY, turns)
        candidate_says(answer)

        if turns >= sim.max_interviewer_turns:
            interviewer_says(interview.prompt.time_limit_goodbye)
            return SimulatedCall(transcript, END_REASON_TIME_LIMIT, turns + 1)

        r = await interviewer_turn(list(interviewer_view))
        if r.text:
            interviewer_says(r.text)
            turns += 1
        if interview.prompt.end_tool_name in r.function_calls:
            interviewer_says(interview.prompt.closing_line)
            return SimulatedCall(transcript, END_REASON_COMPLETE, turns + 1)
        if not r.text:
            return SimulatedCall(transcript, END_REASON_NO_INTERVIEWER_REPLY, turns)
