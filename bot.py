"""Voice Screener bot (Ticket 01: the voice loop).

SmallWebRTC in -> Deepgram STT -> Gemini -> ElevenLabs TTS -> SmallWebRTC out,
with Silero VAD on the user aggregator. Barge-in uses Pipecat's default user
turn strategies, which interrupt the bot when the user starts speaking.

Run with:  uv run bot.py -t webrtc   then open http://localhost:7860/client
"""

import sys
from datetime import datetime

from dotenv import load_dotenv
from loguru import logger

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import LLMRunFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.runner.types import RunnerArguments
from pipecat.runner.utils import create_transport
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.elevenlabs.tts import ElevenLabsTTSService
from pipecat.services.google.llm import GoogleLLMService
from pipecat.transports.base_transport import BaseTransport, TransportParams
from pipecat.workers.runner import WorkerRunner

from screener.config import PROJECT_ROOT, ConfigError, load_voice_config, require_env
from screener.latency import LatencyLog

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
    env = require_env()
    session_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    logger.info(
        f"Session {session_id}: stt={config.stt_model} llm={config.llm_model} tts={config.tts_model}"
    )

    stt = DeepgramSTTService(
        api_key=env["DEEPGRAM_API_KEY"],
        settings=DeepgramSTTService.Settings(model=config.stt_model),
    )

    llm = GoogleLLMService(
        api_key=env["GOOGLE_API_KEY"],
        settings=GoogleLLMService.Settings(
            model=config.llm_model,
            system_instruction=config.system_prompt,
        ),
    )

    tts = ElevenLabsTTSService(
        api_key=env["ELEVENLABS_API_KEY"],
        settings=ElevenLabsTTSService.Settings(
            model=config.tts_model,
            voice=env["ELEVENLABS_VOICE_ID"],
        ),
    )

    context = LLMContext()
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(vad_analyzer=SileroVADAnalyzer()),
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

    latency_log = LatencyLog(session_id)

    worker = PipelineWorker(
        pipeline,
        params=PipelineParams(enable_metrics=True, enable_usage_metrics=True),
        observers=[latency_log.observer],
        idle_timeout_secs=runner_args.pipeline_idle_timeout_secs,
    )

    runner = WorkerRunner(handle_sigint=runner_args.handle_sigint)

    @worker.rtvi.event_handler("on_client_ready")
    async def on_client_ready(_rtvi):
        context.add_message({"role": "developer", "content": config.greeting_prompt})
        await worker.queue_frames([LLMRunFrame()])

    @transport.event_handler("on_client_connected")
    async def on_client_connected(_transport, _client):
        logger.info(f"Session {session_id}: client connected")

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(_transport, _client):
        logger.info(f"Session {session_id}: client disconnected, ending call")
        await runner.cancel()

    await runner.add_workers(worker)
    await runner.run()
    logger.info(f"Session {session_id}: ended cleanly")


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
        require_env()
    except ConfigError as e:
        logger.error(str(e))
        sys.exit(1)
    logger.info(
        f"Preflight OK: config loaded (stt={config.stt_model}, llm={config.llm_model}, "
        f"tts={config.tts_model}); all 4 .env values present"
    )


if __name__ == "__main__":
    from pipecat.runner.run import main

    preflight()
    main()
