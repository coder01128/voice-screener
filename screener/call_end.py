"""Ending the call from the bot side, enforced in code.

Both ways a call ends on the bot's side use CallEnder.end():
- normal finish: the LLM calls the end-interview tool once every topic is
  covered; code then speaks the closing line from config and ends the call.
- time limit: CallTimeLimit fires after max_call_seconds; code cuts off the
  bot, speaks the time-limit goodbye from config and ends the call.

The goodbye is queued ahead of an EndWorkerFrame, not an EndFrame. An EndFrame
entering at the pipeline source reaches the SmallWebRTC input transport first,
and its stop() disconnects the peer at once, cutting off audio still being
synthesised. An EndWorkerFrame travels downstream instead: the TTS service
queues it behind the goodbye's audio, the output transport releases it only
after that audio has played, and when it reaches the end of the pipeline the
worker sends the EndFrame that shuts everything down.
"""

import asyncio

from loguru import logger

from pipecat.adapters.llm.schemas.function_schema import FunctionSchema
from pipecat.frames.frames import (
    EndWorkerFrame,
    Frame,
    FunctionCallResultProperties,
    InterruptionFrame,
    TTSSpeakFrame,
)
from pipecat.services.llm_service import FunctionCallParams
from pipecat.turns.user_mute.base_user_mute_strategy import BaseUserMuteStrategy

END_REASON_COMPLETE = "interview_complete"
END_REASON_TIME_LIMIT = "time_limit"


class WrapUpMuteStrategy(BaseUserMuteStrategy):
    """Mutes the candidate once the call is wrapping up, so nothing interrupts the goodbye.

    A muted user aggregator also drops InterruptionFrames. So when the wrap-up
    starts by interrupting the bot, the mute is armed and only switches on as
    that interruption passes through (or, as a fallback, the goodbye does).
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.active = False
        self._armed_on: Frame | None = None

    def activate(self):
        self.active = True

    def arm(self, goodbye: Frame):
        self._armed_on = goodbye

    async def process_frame(self, frame: Frame) -> bool:
        if self._armed_on is not None and (
            isinstance(frame, InterruptionFrame) or frame is self._armed_on
        ):
            self.active = True
            self._armed_on = None
        return self.active


class CallEnder:
    """Created before the pipeline worker (the end tool needs it), then attached to it."""

    def __init__(self, mute: WrapUpMuteStrategy):
        self._mute = mute
        self._worker = None
        self.reason: str | None = None

    def attach(self, worker):
        self._worker = worker

    @property
    def ending(self) -> bool:
        return self.reason is not None

    async def end(self, reason: str, goodbye_text: str, interrupt: bool):
        """Speak goodbye_text in full, then end the call. Only the first call has any effect."""
        if self.ending:
            return
        if self._worker is None:
            raise RuntimeError("CallEnder.end() called before attach(worker)")
        self.reason = reason
        logger.info(f"Bot is ending the call ({reason})")

        goodbye = TTSSpeakFrame(goodbye_text)
        goodbye.interruptible = False
        frames: list[Frame] = []
        if interrupt:
            frames.append(InterruptionFrame())
            self._mute.arm(goodbye)
        else:
            self._mute.activate()
        frames += [goodbye, EndWorkerFrame(reason=reason)]
        await self._worker.queue_frames(frames)


class CallTimeLimit:
    def __init__(self, ender: CallEnder, max_seconds: int, goodbye_text: str):
        self._ender = ender
        self._max_seconds = max_seconds
        self._goodbye_text = goodbye_text
        self._task: asyncio.Task | None = None

    def start(self):
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="call-time-limit")

    def cancel(self):
        if self._task and not self._task.done():
            self._task.cancel()

    async def _run(self):
        await asyncio.sleep(self._max_seconds)
        logger.info(f"Call time limit of {self._max_seconds}s reached")
        await self._ender.end(END_REASON_TIME_LIMIT, self._goodbye_text, interrupt=True)


def end_interview_tool(name: str, description: str, ender: CallEnder, closing_line: str) -> FunctionSchema:
    """The tool the LLM calls when every topic is covered. Code, not the LLM, says goodbye."""

    async def handle_end_interview(params: FunctionCallParams):
        # Resolve the call first so the LLM isn't left waiting, and don't let it reply.
        await params.result_callback(
            {"status": "ending call"},
            properties=FunctionCallResultProperties(run_llm=False),
        )
        await ender.end(END_REASON_COMPLETE, closing_line, interrupt=False)

    return FunctionSchema(
        name=name,
        description=description,
        properties={},
        required=[],
        handler=handle_end_interview,
    )
