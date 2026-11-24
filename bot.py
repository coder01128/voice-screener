"""Voice Screener bot: a short candidate screening call.

SmallWebRTC in -> Deepgram STT -> Gemini -> ElevenLabs TTS -> SmallWebRTC out,
with Silero VAD on the user aggregator. Barge-in: the candidate's turn starts
on VAD speech only (screener.turns), which interrupts the bot.

The interview script comes from config/interview.json via screener.interview,
the same code simulate.py uses. The bot ends the call itself, in code
(screener.call_end), either when the LLM calls the end-interview tool after the
last topic or when the call-length limit is reached. The transcript is saved to
profiles/<call id>/ when the call ends, however it ends, and the candidate
profile is built from it (screener.profile, same code as simulate.py).

Run with:  uv run bot.py -t webrtc   then open http://localhost:7860/client
"""

import sys
import time
from datetime import datetime

from dotenv import load_dotenv
from loguru import logger

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import TTSSpeakFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    AssistantTurnStoppedMessage,
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
    UserTurnStoppedMessage,
)
from pipecat.runner.types import RunnerArguments
from pipecat.runner.utils import create_transport
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.elevenlabs.tts import ElevenLabsTTSService
from pipecat.transports.base_transport import BaseTransport, TransportParams
from pipecat.workers.runner import WorkerRunner

from screener.call_end import CallEnder, CallTimeLimit, WrapUpMuteStrategy, end_interview_tool
from screener.config import PROJECT_ROOT, ConfigError, load_voice_config, require_env
from screener.interview import build_opening_line, load_interview_config
from screener.latency import LatencyLog
from screener.llm import interviewer_llm
from screener.profile import profile_call
from screener.transcript import CANDIDATE, INTERVIEWER, TranscriptRecorder, now_iso
from screener.turns import candidate_turn_strategies

load_dotenv(PROJECT_ROOT / ".env", override=True)

BOT_LOG_PATH = PROJECT_ROOT / "logs" / "bot.log"


def add_file_log() -> int:
    return logger.add(BOT_LOG_PATH, level="DEBUG", rotation="5 MB", retention=5, encoding="utf-8")


# Captures preflight. The dev runner's main() then calls logger.remove(), which
# drops this sink, so bot() adds its own for each call.
add_file_log()


async def run_bot(transport: BaseTransport, runner_args: RunnerArguments):
    # Re-read per call so config edits apply without restarting the server.
    config = load_voice_config()
    interview = load_interview_config()
    env = require_env()
    call_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    logger.info(
        f"Session {call_id}: stt={config.stt_model} llm={config.llm_model} tts={config.tts_model} "
        f"limit={interview.max_call_seconds}s llm_retry="
        f"{f'{config.llm_first_reply_timeout_secs}s' if config.llm_retry_if_slow_to_start else 'off'}"
    )

    stt = DeepgramSTTService(
        api_key=env["DEEPGRAM_API_KEY"],
        settings=DeepgramSTTService.Settings(model=config.stt_model),
    )

    llm = interviewer_llm(config, interview, env["GOOGLE_API_KEY"])

    tts = ElevenLabsTTSService(
        api_key=env["ELEVENLABS_API_KEY"],
        settings=ElevenLabsTTSService.Settings(
            model=config.tts_model,
            voice=env["ELEVENLABS_VOICE_ID"],
        ),
    )

    wrap_up_mute = WrapUpMuteStrategy()
    ender = CallEnder(wrap_up_mute)
    end_tool = end_interview_tool(
        interview.prompt.end_tool_name,
        interview.prompt.end_tool_description,
        ender,
        interview.prompt.closing_line,
    )
    context = LLMContext(tools=[end_tool])
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            vad_analyzer=SileroVADAnalyzer(),
            user_turn_strategies=candidate_turn_strategies(),
            user_mute_strategies=[wrap_up_mute],
        ),
    )

    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            user_aggregator,
            llm,
            tts,
            transport.output(),
            assistant_aggregator,
        ]
    )

    latency_log = LatencyLog(call_id)

    worker = PipelineWorker(
        pipeline,
        params=PipelineParams(enable_metrics=True, enable_usage_metrics=True),
        observers=[latency_log.observer],
        idle_timeout_secs=runner_args.pipeline_idle_timeout_secs,
    )

    ender.attach(worker)
    runner = WorkerRunner(handle_sigint=runner_args.handle_sigint)
    transcript = TranscriptRecorder()
    time_limit = CallTimeLimit(ender, interview.max_call_seconds, interview.prompt.time_limit_goodbye)
    call = {"started_at": None, "started_monotonic": None, "out_dir": None}

    @user_aggregator.event_handler("on_user_turn_stopped")
    async def on_user_turn_stopped(_aggregator, _strategy, message: UserTurnStoppedMessage):
        transcript.add(CANDIDATE, message.content, message.timestamp)

    @assistant_aggregator.event_handler("on_assistant_turn_stopped")
    async def on_assistant_turn_stopped(_aggregator, message: AssistantTurnStoppedMessage):
        transcript.add(INTERVIEWER, message.content, message.timestamp)

    @worker.rtvi.event_handler("on_client_ready")
    async def on_client_ready(_rtvi):
        call["started_at"] = now_iso()
        call["started_monotonic"] = time.monotonic()
        time_limit.start()
        await worker.queue_frames([TTSSpeakFrame(build_opening_line(interview))])

    @transport.event_handler("on_client_connected")
    async def on_client_connected(_transport, _client):
        logger.info(f"Session {call_id}: client connected")

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(_transport, _client):
        if ender.ending:
            # The bot hung up; the client leaving is the result, not the cause.
            logger.info(f"Session {call_id}: client disconnected after bot ended the call")
            return
        logger.info(f"Session {call_id}: client disconnected, ending call")
        await runner.cancel()

    @worker.event_handler("on_pipeline_finished")
    async def on_pipeline_finished(_worker, _frame):
        time_limit.cancel()
        duration = (
            round(time.monotonic() - call["started_monotonic"], 1)
            if call["started_monotonic"] is not None
            else None
        )
        out_dir = transcript.save(
            call_id,
            {
                "call_id": call_id,
                "mode": "voice",
                "started_at": call["started_at"],
                "ended_at": now_iso(),
                "duration_secs": duration,
                "end_reason": ender.reason or "candidate_disconnected",
                "max_call_seconds": interview.max_call_seconds,
            },
        )
        call["out_dir"] = out_dir
        logger.info(f"Session {call_id}: transcript saved to {out_dir} ({len(transcript.turns)} turns)")

    await runner.add_workers(worker)
    await runner.run()
    logger.info(f"Session {call_id}: ended cleanly")

    if call.get("out_dir") and transcript.turns:
        try:
            result, requests = await profile_call(call["out_dir"], env["GOOGLE_API_KEY"])
            logger.info(
                f"Session {call_id}: profile {result['status']} after {result['attempts']} attempt(s), "
                f"{len(result['evidence_errors'])} evidence error(s), {requests} Gemini request(s)"
            )
        except Exception as e:
            logger.error(f"Session {call_id}: profile step failed: {type(e).__name__}: {e}")


async def bot(runner_args: RunnerArguments):
    transport_params = {
        "webrtc": lambda: TransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
        ),
    }
    log_sink = add_file_log()
    try:
        transport = await create_transport(runner_args, transport_params)
        await run_bot(transport, runner_args)
    finally:
        logger.remove(log_sink)


def preflight():
    """Fail fast at start-up, before the browser connects, naming what is wrong."""
    try:
        config = load_voice_config()
        interview = load_interview_config()
        require_env()
    except ConfigError as e:
        logger.error(str(e))
        sys.exit(1)
    logger.info(
        f"Preflight OK: config loaded (stt={config.stt_model}, llm={config.llm_model}, "
        f"tts={config.tts_model}); interview has {len(interview.topics)} topics, "
        f"limit {interview.max_call_seconds}s; all 4 .env values present"
    )


if __name__ == "__main__":
    from pipecat.runner.run import main

    preflight()
    main()
