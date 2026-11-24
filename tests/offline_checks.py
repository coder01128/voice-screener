"""Offline checks: no microphone, no network, no credits, no keys.

Run with:  uv run python tests/offline_checks.py
Prints PASS/FAIL per check and exits non-zero if any fail.
"""

import asyncio
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from loguru import logger  # noqa: E402

logger.remove()  # keep output to PASS/FAIL lines
logger.add(sys.stderr, level="WARNING")

from pipecat.frames.frames import (  # noqa: E402
    EndFrame,
    EndWorkerFrame,
    Frame,
    FunctionCallResultProperties,
    InputAudioRawFrame,
    InterruptionFrame,
    TranscriptionFrame,
    TTSSpeakFrame,
    UserStartedSpeakingFrame,
    VADUserStartedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline  # noqa: E402
from pipecat.pipeline.worker import PipelineWorker  # noqa: E402
from pipecat.processors.aggregators.llm_context import LLMContext  # noqa: E402
from pipecat.processors.aggregators.llm_response_universal import (  # noqa: E402
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor  # noqa: E402
from pipecat.workers.runner import WorkerRunner  # noqa: E402

from screener.call_end import (  # noqa: E402
    END_REASON_COMPLETE,
    END_REASON_TIME_LIMIT,
    CallEnder,
    CallTimeLimit,
    WrapUpMuteStrategy,
    end_interview_tool,
)
from screener.config import ConfigError  # noqa: E402
from screener.interview import (  # noqa: E402
    INTERVIEW_CONFIG_PATH,
    build_interviewer_prompt,
    build_opening_line,
    load_interview_config,
)
from screener.transcript import CANDIDATE, INTERVIEWER, PROFILES_DIR, TranscriptRecorder, now_iso  # noqa: E402
from screener.turns import candidate_turn_strategies  # noqa: E402

results: list[bool] = []


def check(name: str, ok: bool, detail: str = ""):
    results.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))


def section(title: str):
    print(f"\n== {title} ==")


class FakeWorker:
    def __init__(self, log: list | None = None):
        self.frames: list[Frame] = []
        self._log = log

    async def queue_frames(self, frames):
        if self._log is not None:
            self._log.append("frames queued")
        self.frames.extend(frames)


# ---------------------------------------------------------------- interview config
def check_interview_config():
    section("Interview config and prompt")
    cfg = load_interview_config()
    prompt = build_interviewer_prompt(cfg)
    opening = build_opening_line(cfg)
    check("interview.json loads", True, f"{len(cfg.topics)} topics, limit {cfg.max_call_seconds}s, agent {cfg.agent_name}")
    check("six topics in spec order", [t.id for t in cfg.topics] == [
        "current_role", "strongest_skills", "wants_next", "work_arrangement", "notice_period", "salary_expectation"])
    check("agent is Sam, never Jem", cfg.agent_name == "Sam" and "jem" not in (prompt + opening).lower())
    check("at most two follow-ups per topic", all(len(t.follow_ups) <= 2 for t in cfg.topics))
    check("salary topic optional, decline accepted", cfg.topics[-1].optional and "declines" in prompt)
    check("opening says AI, about three minutes, first question",
          "AI" in opening and "three minutes" in opening and cfg.topics[0].question in opening)
    check("prompt has no unfilled placeholders", "{" not in prompt and "}" not in prompt)
    for rule in ["one question at a time", "one or two short sentences", "never give advice", "never promise"]:
        check(f"prompt rule: {rule}", rule in prompt.lower())
    check("prompt tells LLM to call the end tool after the last topic",
          f"call the {cfg.prompt.end_tool_name} function" in prompt)
    check("closing line says a person will review the notes",
          "person from the team will review the notes" in cfg.prompt.closing_line)
    return cfg


def check_config_validation():
    section("Config validation")
    base = json.loads(INTERVIEW_CONFIG_PATH.read_text(encoding="utf-8"))
    bad_cases = {
        "three follow-ups": lambda d: d["topics"][0].__setitem__("follow_ups", ["a?", "b?", "c?"]),
        "agent named Jem": lambda d: d.__setitem__("agent_name", "Jem"),
        "unknown placeholder": lambda d: d["prompt"].__setitem__("opening_template", "Hi {nope}"),
        "zero time limit": lambda d: d.__setitem__("max_call_seconds", 0),
        "end tool name with spaces": lambda d: d["prompt"]["end_tool"].__setitem__("name", "end call"),
        "missing closing line": lambda d: d["prompt"].pop("closing_line"),
    }
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "interview.json"
        for name, mutate in bad_cases.items():
            d = json.loads(json.dumps(base))
            mutate(d)
            path.write_text(json.dumps(d), encoding="utf-8")
            try:
                load_interview_config(path)
                check(f"rejects {name}", False)
            except ConfigError:
                check(f"rejects {name}", True)

        d = json.loads(json.dumps(base))
        d["topics"][1]["question"] = "Which skill are you proudest of?"
        path.write_text(json.dumps(d), encoding="utf-8")
        check("edited question wording reaches the prompt, no code change",
              "Which skill are you proudest of?" in build_interviewer_prompt(load_interview_config(path)))


# ---------------------------------------------------------------- transcript
def check_transcript(opening: str):
    section("Transcript format")
    call_id = "_offline_check"
    rec = TranscriptRecorder()
    rec.add(INTERVIEWER, opening, "2026-10-28T08:00:00.000+00:00")
    rec.add(CANDIDATE, "I'm a junior React developer.", "2026-10-28T08:00:09.000+00:00")
    rec.add(INTERVIEWER, "", "2026-10-28T08:00:12.000+00:00")  # cut off before any word: skipped
    rec.add(INTERVIEWER, "Thanks. What are your strongest skills?", "2026-10-28T08:00:13.000+00:00")
    out = rec.save(call_id, {"mode": "voice"})
    try:
        saved = json.loads((out / "transcript.json").read_text(encoding="utf-8"))
        check("array of {turn, speaker, text, started_at}",
              isinstance(saved, list) and all(set(t) == {"turn", "speaker", "text", "started_at"} for t in saved))
        check("turns numbered 1..n, empty turn skipped", [t["turn"] for t in saved] == [1, 2, 3])
        check("mode is in call.json, not the transcript",
              json.loads((out / "call.json").read_text(encoding="utf-8"))["mode"] == "voice")
    finally:
        shutil.rmtree(PROFILES_DIR / call_id, ignore_errors=True)


# ---------------------------------------------------------------- bot hangs up
async def check_normal_finish_hangup(cfg):
    section("Normal finish: bot hangs up after the closing line")
    order: list[str] = []
    worker, mute = FakeWorker(order), WrapUpMuteStrategy()
    ender = CallEnder(mute)
    ender.attach(worker)
    tool = end_interview_tool(cfg.prompt.end_tool_name, cfg.prompt.end_tool_description, ender, cfg.prompt.closing_line)

    check("tool name and description come from config",
          tool.name == cfg.prompt.end_tool_name and tool.description == cfg.prompt.end_tool_description)

    captured = {}

    async def result_callback(result, *, properties=None):
        order.append("result_callback")
        captured["properties"] = properties

    class Params:
        pass

    params = Params()
    params.result_callback = result_callback
    await tool.handler(params)

    f = worker.frames
    check("LLM call resolved before any frame is queued", order == ["result_callback", "frames queued"])
    check("LLM told not to reply after the tool call",
          isinstance(captured.get("properties"), FunctionCallResultProperties) and captured["properties"].run_llm is False)
    check("frames: closing line, then end-after-audio (no interruption)", [type(x) for x in f] == [TTSSpeakFrame, EndWorkerFrame])
    check("closing line is config text and cannot be interrupted",
          f[0].text == cfg.prompt.closing_line and f[0].interruptible is False)
    check("candidate muted for the closing line", await mute.process_frame(Frame()) is True)
    check("end reason recorded", ender.reason == END_REASON_COMPLETE)

    limit = CallTimeLimit(ender, max_seconds=0, goodbye_text=cfg.prompt.time_limit_goodbye)
    limit.start()
    await asyncio.sleep(0.05)
    await tool.handler(params)
    check("a later time limit or second tool call does nothing", len(worker.frames) == 2 and ender.reason == END_REASON_COMPLETE)


async def check_time_limit_hangup(cfg):
    section("Time limit: bot cuts in, says goodbye, hangs up")
    worker, mute = FakeWorker(), WrapUpMuteStrategy()
    ender = CallEnder(mute)
    ender.attach(worker)
    limit = CallTimeLimit(ender, max_seconds=0, goodbye_text=cfg.prompt.time_limit_goodbye)
    check("candidate not muted before the limit", await mute.process_frame(Frame()) is False)
    limit.start()
    await asyncio.sleep(0.05)
    f = worker.frames
    check("frames: interruption, goodbye, end-after-audio",
          [type(x) for x in f] == [InterruptionFrame, TTSSpeakFrame, EndWorkerFrame])
    check("goodbye is config text and cannot be interrupted",
          f[1].text == cfg.prompt.time_limit_goodbye and f[1].interruptible is False)
    check("end reason recorded", ender.reason == END_REASON_TIME_LIMIT)

    worker2, mute2 = FakeWorker(), WrapUpMuteStrategy()
    ender2 = CallEnder(mute2)
    ender2.attach(worker2)
    limit2 = CallTimeLimit(ender2, max_seconds=60, goodbye_text="x")
    limit2.start()
    limit2.cancel()
    await asyncio.sleep(0.05)
    check("cancelled timer does nothing", worker2.frames == [] and ender2.reason is None)


class Capture(FrameProcessor):
    def __init__(self):
        super().__init__()
        self.seen: list[Frame] = []

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        self.seen.append(frame)
        await self.push_frame(frame, direction)


async def run_through_real_user_aggregator(interrupt: bool) -> tuple[list[Frame], WrapUpMuteStrategy, str | None]:
    """Sends the wrap-up frames through a real Pipecat user aggregator with the mute strategy."""
    mute = WrapUpMuteStrategy()
    user_agg, _ = LLMContextAggregatorPair(LLMContext(), user_params=LLMUserAggregatorParams(user_mute_strategies=[mute]))
    capture = Capture()
    worker = PipelineWorker(Pipeline([user_agg, capture]), enable_rtvi=False, idle_timeout_secs=None)
    ender = CallEnder(mute)
    ender.attach(worker)
    runner = WorkerRunner(handle_sigint=False)
    await runner.add_workers(worker)

    async def mic_audio():
        # Like a live call: silent mic frames keep arriving, so the aggregator
        # re-evaluates the mute between the wrap-up starting and its frames passing.
        silence = b"\x00" * 640
        while True:
            await worker.queue_frames([InputAudioRawFrame(audio=silence, sample_rate=16000, num_channels=1)])
            await asyncio.sleep(0.01)

    async def end_soon():
        await asyncio.sleep(0.3)
        await ender.end("check", "Goodbye.", interrupt=interrupt)

    mic = asyncio.create_task(mic_audio())
    asyncio.create_task(end_soon())
    try:
        await asyncio.wait_for(runner.run(), timeout=15)
    finally:
        mic.cancel()
    return [f for f in capture.seen if not isinstance(f, InputAudioRawFrame)], mute, ender.reason


async def check_real_aggregator():
    section("Real Pipecat user aggregator (catches frames the mute would drop)")
    def ordered(types, *wanted):
        try:
            idx = [types.index(w) for w in wanted]
        except ValueError:
            return False
        return idx == sorted(idx)

    seen, mute, _ = await run_through_real_user_aggregator(interrupt=True)
    types = [type(x) for x in seen]
    check("time limit: interruption gets past the muted aggregator", InterruptionFrame in types)
    check("time limit: goodbye, then end request, then pipeline EndFrame (in that order)",
          ordered(types, TTSSpeakFrame, EndWorkerFrame, EndFrame))
    check("time limit: candidate muted afterwards", mute.active is True)

    seen, mute, _ = await run_through_real_user_aggregator(interrupt=False)
    types = [type(x) for x in seen]
    check("normal finish: closing line, then end request, then pipeline EndFrame (in that order)",
          ordered(types, TTSSpeakFrame, EndWorkerFrame, EndFrame))
    check("normal finish: no interruption sent", InterruptionFrame not in types)


async def frames_after_user_aggregator(strategies, inputs: list[Frame]) -> list[Frame]:
    """Queues inputs through a real user aggregator using the given turn strategies."""
    user_agg, _ = LLMContextAggregatorPair(
        LLMContext(), user_params=LLMUserAggregatorParams(user_turn_strategies=strategies)
    )
    capture = Capture()
    worker = PipelineWorker(Pipeline([user_agg, capture]), enable_rtvi=False, idle_timeout_secs=None)
    runner = WorkerRunner(handle_sigint=False)
    await runner.add_workers(worker)

    async def feed():
        await asyncio.sleep(0.3)
        for frame in inputs:
            await worker.queue_frames([frame])
            await asyncio.sleep(0.2)
        await worker.queue_frames([EndFrame()])

    asyncio.create_task(feed())
    await asyncio.wait_for(runner.run(), timeout=15)
    return capture.seen


def turn_started(seen: list[Frame]) -> bool:
    return any(isinstance(f, (InterruptionFrame, UserStartedSpeakingFrame)) for f in seen)


async def check_turn_start():
    section("Candidate turn start (late words must not cut Sam off)")
    late_text = TranscriptionFrame(text="and that was the end of it", user_id="", timestamp=now_iso(), finalized=True)
    seen = await frames_after_user_aggregator(candidate_turn_strategies(), [late_text])
    check("late transcript alone does not start a turn or interrupt the bot", not turn_started(seen))

    seen = await frames_after_user_aggregator(candidate_turn_strategies(), [VADUserStartedSpeakingFrame()])
    check("real speech (VAD) still starts a turn and interrupts the bot", turn_started(seen))


def check_voice_config():
    section("Voice config (Gemini slow-start retry)")
    from screener.config import VOICE_CONFIG_PATH, load_voice_config

    vc = load_voice_config()
    check("retry on, first-reply timeout set",
          vc.llm_retry_if_slow_to_start is True and vc.llm_first_reply_timeout_secs > 0,
          f"timeout {vc.llm_first_reply_timeout_secs}s")
    base = json.loads(VOICE_CONFIG_PATH.read_text(encoding="utf-8"))
    bad_cases = {
        "retry flag not true/false": lambda d: d["llm"].__setitem__("retry_if_slow_to_start", "yes"),
        "zero timeout": lambda d: d["llm"].__setitem__("first_reply_timeout_secs", 0),
        "missing timeout": lambda d: d["llm"].pop("first_reply_timeout_secs"),
        "zero requests per minute": lambda d: d["llm"].__setitem__("max_requests_per_minute", 0),
    }
    with tempfile.TemporaryDirectory() as tmp:
        bom = Path(tmp) / "bom.json"
        bom.write_bytes(b"\xef\xbb\xbf" + VOICE_CONFIG_PATH.read_bytes())
        check("config saved with a UTF-8 BOM (Notepad) still loads", load_voice_config(bom).llm_model == vc.llm_model)
        path = Path(tmp) / "voice.json"
        for name, mutate in bad_cases.items():
            d = json.loads(json.dumps(base))
            mutate(d)
            path.write_text(json.dumps(d), encoding="utf-8")
            try:
                load_voice_config(path)
                check(f"rejects {name}", False)
            except ConfigError:
                check(f"rejects {name}", True)


async def check_gemini_retry():
    """Drives Pipecat's real GoogleLLMService retry code with a fake stream: the
    first request stalls (like the 16s free-tier stalls), the re-sent one is fast.
    No network: _stream_content is replaced, the API key is a dummy."""
    section("Gemini slow-start retry (real Pipecat code, fake stream)")
    import time

    from pipecat.services.google.llm import GoogleLLMService

    timeout = 0.3
    llm = GoogleLLMService(api_key="offline-dummy", retry_on_timeout=True, retry_timeout_secs=timeout)
    attempts = []

    async def fake_stream_content(_context):
        attempts.append(time.monotonic())
        stall = len(attempts) == 1

        async def chunks():
            if stall:
                await asyncio.sleep(5)
            yield "first chunk"

        return chunks()

    llm._stream_content = fake_stream_content
    start = time.monotonic()
    got = [c async for c in llm._stream_response(LLMContext())]
    elapsed = time.monotonic() - start
    check("stalled request is re-sent once", len(attempts) == 2)
    check("reply arrives soon after the timeout, not after the stall",
          got == ["first chunk"] and elapsed < timeout + 1.0, f"{elapsed:.2f}s with a 5s stall")

    attempts.clear()
    llm_off = GoogleLLMService(api_key="offline-dummy", retry_on_timeout=False, retry_timeout_secs=timeout)

    async def fast_stream_content(_context):
        attempts.append(time.monotonic())

        async def chunks():
            yield "first chunk"

        return chunks()

    llm._stream_content = fast_stream_content
    got = [c async for c in llm._stream_response(LLMContext())]
    check("a normal fast reply is not re-sent", len(attempts) == 1 and got == ["first chunk"])
    check("retry can be switched off in config", llm_off._retry_on_timeout is False)


# ---------------------------------------------------------------- simulation (Ticket 02B)
def check_personas():
    section("Personas: the persona model sees the card only")
    import screener.personas as personas
    from screener.personas import build_persona_prompt, list_personas, load_card, load_simulation_config

    sim = load_simulation_config()
    check("simulation.json loads", True, f"persona model {sim.persona_model}, {sim.max_interviewer_turns} interviewer turns")
    check("three personas found, expected.json is not a persona", list_personas() == ["lerato", "pieter", "thandi"])
    for pid in list_personas():
        card = load_card(pid)
        prompt = build_persona_prompt(card, sim)
        check(f"{pid}: prompt has the name and every card value, no unfilled placeholders",
              card["name"] in prompt and all(str(v) in prompt for v in card.values())
              and "{" not in prompt and "}" not in prompt)

    secret = "GROUND-TRUTH-SECRET-7731"
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "probe.json").write_text(json.dumps({
            "card": {"name": "Probe", "salary_stance": "won't say"},
            "ground_truth": {"salary_expectation": secret, "notes": secret},
        }), encoding="utf-8")
        original_dir = personas.PERSONAS_DIR
        personas.PERSONAS_DIR = Path(tmp)
        try:
            card = load_card("probe")
            prompt = build_persona_prompt(card, sim)
        finally:
            personas.PERSONAS_DIR = original_dir
    check("load_card returns the card section only", "ground_truth" not in card and secret not in json.dumps(card))
    check("ground_truth never reaches the persona prompt", secret not in prompt)


def check_interviewer_is_shared():
    section("Voice bot and simulate.py share the interviewer (no copies)")
    sources = {p.relative_to(ROOT).as_posix(): p.read_text(encoding="utf-8")
               for p in list(ROOT.glob("*.py")) + list((ROOT / "screener").glob("*.py"))}
    builds_service = [n for n, s in sources.items() if "GoogleLLMService(" in s and n != "screener/llm.py"]
    builds_prompt = [n for n, s in sources.items()
                     if "build_interviewer_prompt(" in s and n not in ("screener/llm.py", "screener/interview.py")]
    check("only screener/llm.py creates Gemini services", builds_service == [], ", ".join(builds_service))
    check("only screener/llm.py calls the interviewer prompt builder", builds_prompt == [], ", ".join(builds_prompt))
    check("bot.py and simulate.py both use interviewer_llm()",
          "interviewer_llm(" in sources["bot.py"] and "interviewer_llm(" in sources["simulate.py"])
    check("both use the same opening-line builder",
          "build_opening_line(" in sources["bot.py"] and "build_opening_line(" in sources["screener/simulation.py"])


async def check_simulated_request(cfg):
    section("Simulated interviewer request = voice request (offline conversion)")
    from screener.call_end import end_interview_schema
    from screener.config import load_voice_config
    from screener.interview import build_interviewer_prompt
    from screener.llm import interviewer_llm

    llm = interviewer_llm(load_voice_config(), cfg, "offline-dummy")
    tool = end_interview_schema(cfg.prompt.end_tool_name, cfg.prompt.end_tool_description)
    ctx = LLMContext(messages=[{"role": "assistant", "content": build_opening_line(cfg)},
                               {"role": "user", "content": "I'm a junior developer."}], tools=[tool])
    params = await llm.get_llm_adapter().get_llm_invocation_params(
        ctx, system_instruction=llm._settings.system_instruction,
        ensure_last_message_is_user=llm._should_inject_trailing_user_message())
    tool_names = [f["name"] for t in params["tools"] for f in t.get("function_declarations", [])]
    check("system instruction is the shared interviewer prompt",
          params["system_instruction"] == build_interviewer_prompt(cfg))
    check("end tool is advertised to Gemini", tool_names == [cfg.prompt.end_tool_name])
    check("conversation keeps both turns in order", [m.role for m in params["messages"]] == ["model", "user"])


def fake_interviewer(script: list):
    """Returns an interviewer turn that plays back Reply objects in order."""
    from screener.llm import Reply

    queue = list(script)

    async def turn(messages):
        turn.views.append(messages)
        return queue.pop(0) if queue else Reply(text="And another question?")

    turn.views = []
    return turn


def fake_persona():
    async def turn(messages):
        turn.views.append(messages)
        return f"Answer {len(turn.views)}."

    turn.views = []
    return turn


async def check_simulation_loop(cfg):
    section("Simulated call loop (fake Gemini, no requests)")
    from screener.llm import Reply
    from screener.personas import load_simulation_config
    from screener.simulation import END_REASON_NO_INTERVIEWER_REPLY, run_simulated_call

    sim = load_simulation_config()
    end = cfg.prompt.end_tool_name

    questions = [Reply(text=f"Question {i}?") for i in range(2, 7)] + [Reply(function_calls=[end])]
    interviewer, persona = fake_interviewer(questions), fake_persona()
    result = await run_simulated_call(cfg, sim, interviewer, persona)
    turns = result.transcript.turns
    check("normal finish: ends with the closing line from config",
          result.end_reason == END_REASON_COMPLETE and turns[-1]["text"] == cfg.prompt.closing_line)
    check("starts with the shared opening line", turns[0]["text"] == build_opening_line(cfg))
    check("turns alternate interviewer/candidate, numbered 1..n",
          [t["speaker"] for t in turns[:-1]] == [INTERVIEWER, CANDIDATE] * 6
          and [t["turn"] for t in turns] == list(range(1, len(turns) + 1)))
    check("same transcript format as voice", all(set(t) == {"turn", "speaker", "text", "started_at"} for t in turns))
    check("interviewer sees itself as assistant, candidate as user",
          [m["role"] for m in interviewer.views[0]] == ["assistant", "user"])
    check("persona sees the interviewer as user, itself as assistant",
          [m["role"] for m in persona.views[1]] == ["user", "assistant", "user"])

    interviewer, persona = fake_interviewer([]), fake_persona()
    result = await run_simulated_call(cfg, sim, interviewer, persona)
    turns = result.transcript.turns
    spoken_by_interviewer = sum(t["speaker"] == INTERVIEWER for t in turns)
    check("turn limit: ends with the time-limit goodbye",
          result.end_reason == END_REASON_TIME_LIMIT and turns[-1]["text"] == cfg.prompt.time_limit_goodbye)
    check("turn limit: interviewer spoke max turns, then the goodbye",
          spoken_by_interviewer == sim.max_interviewer_turns + 1, f"{spoken_by_interviewer} interviewer turns")

    interviewer = fake_interviewer([Reply(text="Thanks, that's all.", function_calls=[end])])
    result = await run_simulated_call(cfg, sim, interviewer, fake_persona())
    check("text plus end tool in one reply: text, then the closing line",
          [t["text"] for t in result.transcript.turns[-2:]] == ["Thanks, that's all.", cfg.prompt.closing_line])

    result = await run_simulated_call(cfg, sim, fake_interviewer([Reply()]), fake_persona())
    check("empty interviewer reply stops the run instead of looping",
          result.end_reason == END_REASON_NO_INTERVIEWER_REPLY)


async def check_reply_parsing_and_counting():
    section("Gemini reply parsing and request counting (real Pipecat path, fake stream)")
    from google.genai import types

    from pipecat.services.google.llm import GoogleLLMService
    from screener.llm import RequestCounter, reply

    def chunk(*parts):
        return types.GenerateContentResponse(candidates=[types.Candidate(content=types.Content(role="model", parts=list(parts)))])

    llm = GoogleLLMService(api_key="offline-dummy")

    async def fake_stream(_ctx):
        async def gen():
            yield chunk(types.Part(text="Let me think about this.", thought=True))
            yield chunk(types.Part(text="Thanks. "))
            yield chunk(types.Part(text="Where are you based?"))
            yield chunk(types.Part(function_call=types.FunctionCall(name="end_interview", args={})))
        return gen()

    llm._stream_content = fake_stream
    counter = RequestCounter(llm)
    r = await reply(llm, LLMContext())
    check("text joined, Gemini's thinking text skipped", r.text == "Thanks. Where are you based?")
    check("tool call captured", r.function_calls == ["end_interview"])
    check("one request counted", counter.count == 1)

    retrying = GoogleLLMService(api_key="offline-dummy", retry_on_timeout=True, retry_timeout_secs=0.2)
    calls = []

    async def stalls_once(_ctx):
        calls.append(1)
        stall = len(calls) == 1

        async def gen():
            if stall:
                await asyncio.sleep(3)
            yield chunk(types.Part(text="Hello."))
        return gen()

    retrying._stream_content = stalls_once
    counter = RequestCounter(retrying)
    r = await reply(retrying, LLMContext())
    check("a slow-start retry counts as two requests", counter.count == 2 and r.text == "Hello.")


# ---------------------------------------------------------------- profile (Ticket 03)
def sample_profile(schema: dict, turn: int = 2, marker: str | None = None) -> dict:
    """A schema-valid profile generated from the schema itself (no field names typed here)."""
    out = {}
    for name, prop in schema["properties"].items():
        if prop["type"] == "object":
            out[name] = {"value": marker or f"{name} text", "evidence": [] if marker else [turn]}
        else:
            item = {k: f"{name} {k}" for k, sub in prop["items"]["properties"].items() if sub.get("type") == "string"}
            out[name] = [] if marker else [{**item, "evidence": [turn]}]
    return out


def write_call(tmp: Path, turns: int = 6) -> Path:
    call_dir = tmp / "call"
    call_dir.mkdir()
    rows = [{"turn": i, "speaker": INTERVIEWER if i % 2 else CANDIDATE, "text": f"line {i}",
             "started_at": now_iso()} for i in range(1, turns + 1)]
    (call_dir / "transcript.json").write_text(json.dumps(rows), encoding="utf-8")
    return call_dir


def fake_generate(replies: list):
    """Plays back raw replies; an Exception instance is raised instead of returned."""
    queue = list(replies)

    async def generate(system, user, schema):
        generate.calls.append((system, user, schema))
        r = queue.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    generate.calls = []
    return generate


def check_profile_schema_and_prompt():
    section("Profile schema is the single source of field names")
    from screener.profile import (
        build_extraction_prompt,
        format_transcript,
        load_extraction_config,
        load_schema,
        schema_for_gemini,
    )

    schema, ext = load_schema(), load_extraction_config()
    names = list(schema["properties"])
    check("profile.schema.json is a valid JSON Schema", True, f"{len(names)} fields")
    check("every field is required", sorted(schema["required"]) == sorted(names))
    prompt = build_extraction_prompt(schema, ext)
    check("extraction prompt lists every schema field with its description",
          all(n in prompt and schema["properties"][n]["description"] in prompt for n in names))
    check("extraction prompt names the not-discussed marker", f'"{ext.not_discussed}"' in prompt)
    check("extraction prompt has no unfilled placeholders", "{" not in prompt and "}" not in prompt)
    sent = schema_for_gemini(schema)
    check("schema sent to Gemini is the same schema minus $schema",
          "$schema" not in sent and sent["properties"] == schema["properties"] and "$schema" in schema)
    text = format_transcript([{"turn": 3, "speaker": CANDIDATE, "text": "Hi.", "started_at": "x"}], ext)
    check("transcript lines carry turn numbers", "[turn 3] candidate: Hi." in text)

    code_files = ["bot.py", "simulate.py", "build_profile.py", "screener/profile.py",
                  "screener/ground_truth.py", "screener/llm.py", "screener/simulation.py"]
    hard_coded = [f"{f}: {n}" for f in code_files for n in names
                  if f'"{n}"' in (ROOT / f).read_text(encoding="utf-8") or f"'{n}'" in (ROOT / f).read_text(encoding="utf-8")]
    check("no code file hard-codes a profile field name", hard_coded == [], ", ".join(hard_coded))
    profile_src = (ROOT / "screener/profile.py").read_text(encoding="utf-8")
    check("profile step never reads call.json or mode (voice = simulated)",
          '"call.json"' not in profile_src and '"mode"' not in profile_src)


async def check_profile_paths():
    section("Profile step: valid, retry, invalid paths (fake Gemini, no requests)")
    from screener.profile import build_profile, load_extraction_config, load_schema

    schema, ext = load_schema(), load_extraction_config()
    good = json.dumps(sample_profile(schema))
    missing_field = sample_profile(schema)
    missing_field.pop(next(iter(schema["properties"])))
    bad = json.dumps(missing_field)

    with tempfile.TemporaryDirectory() as tmp:
        call_dir = write_call(Path(tmp))
        gen = fake_generate([good])
        r = await build_profile(call_dir, gen)
        saved = json.loads((call_dir / "profile.json").read_text(encoding="utf-8"))
        check("valid reply: status valid after 1 attempt, saved to profile.json",
              r["status"] == "valid" and r["attempts"] == 1 and saved["profile"] == json.loads(good))
        check("Gemini is sent the schema as its response schema", gen.calls[0][2]["properties"] == schema["properties"])
        check("clean profile: no evidence errors", r["evidence_errors"] == [])

    with tempfile.TemporaryDirectory() as tmp:
        r = await build_profile(write_call(Path(tmp)), fake_generate(["{not json", good]))
        check("bad first reply: retried once, then valid", r["status"] == "valid" and r["attempts"] == 2)

    with tempfile.TemporaryDirectory() as tmp:
        call_dir = write_call(Path(tmp))
        r = await build_profile(call_dir, fake_generate([bad, bad]))
        saved = json.loads((call_dir / "profile.json").read_text(encoding="utf-8"))
        check("invalid twice: saved with status invalid and the raw reply, no profile",
              saved["status"] == "invalid" and saved["raw_output"] == bad and saved["profile"] is None
              and saved["attempts"] == 2 and saved["validation_errors"])

    with tempfile.TemporaryDirectory() as tmp:
        r = await build_profile(write_call(Path(tmp)), fake_generate([RuntimeError("boom"), RuntimeError("boom")]))
        check("request failing twice: invalid, no crash", r["status"] == "invalid" and "boom" in r["validation_errors"][0])

    broken = json.loads(json.dumps(schema))
    first = broken["required"][0]
    broken["required"][0] = first + "_renamed"
    with tempfile.TemporaryDirectory() as tmp:
        call_dir = write_call(Path(tmp))
        r = await build_profile(call_dir, fake_generate([good, good]), schema=broken)
        check("broken schema (required field renamed): invalid path, raw saved, no crash",
              r["status"] == "invalid" and r["raw_output"] == good and (call_dir / "profile.json").exists())


def check_evidence_rules():
    section("Evidence rules (checked in code)")
    from screener.profile import check_evidence, load_extraction_config, load_schema

    schema, ext = load_schema(), load_extraction_config()
    turns = [{"turn": i, "speaker": INTERVIEWER if i % 2 else CANDIDATE} for i in range(1, 7)]
    obj_field = next(n for n, p in schema["properties"].items() if p["type"] == "object")
    arr_field = next(n for n, p in schema["properties"].items() if p["type"] == "array")

    def problems_for(mutate):
        p = sample_profile(schema)
        mutate(p)
        return check_evidence(p, schema, turns, ext.not_discussed)

    check("all-not-discussed profile: no errors",
          check_evidence(sample_profile(schema, marker=ext.not_discussed), schema, turns, ext.not_discussed) == [])
    probs = problems_for(lambda p: p[obj_field].__setitem__("evidence", [99]))
    check("cited turn that doesn't exist is flagged",
          any(e["turn"] == 99 and "does not exist" in e["problem"] for e in probs))
    probs = problems_for(lambda p: p[obj_field].__setitem__("evidence", []))
    check("fact with no cited turn is flagged", any("cites no turn" in e["problem"] for e in probs))
    probs = problems_for(lambda p: p[obj_field].update(value=ext.not_discussed, evidence=[2]))
    check("not-discussed value that cites turns is flagged", any("marked not discussed" in e["problem"] for e in probs))
    probs = problems_for(lambda p: p[obj_field].__setitem__("evidence", [1, 3]))
    check("evidence citing only interviewer turns is flagged", any("interviewer" in e["problem"] for e in probs))
    probs = problems_for(lambda p: p[arr_field][0].__setitem__("evidence", [42]))
    check("list items are checked too", any(e["field"] == f"{arr_field}[0]" and e["turn"] == 42 for e in probs))


def check_ground_truth():
    section("Ground truth (read only by the checker)")
    import screener.personas as personas
    from screener.ground_truth import check_against_ground_truth, load_ground_truth
    from screener.personas import list_personas, load_card
    from screener.profile import load_schema

    schema = load_schema()
    for pid in list_personas():
        truth = load_ground_truth(pid, schema)
        check(f"{pid}: ground_truth uses schema field names; card loader hides it",
              set(truth) <= set(schema["properties"]) and "ground_truth" not in load_card(pid), f"{len(truth)} fields")
    check("expected.json lists exactly the three personas",
          set(k for k in json.loads((personas.PERSONAS_DIR / "expected.json").read_text(encoding="utf-8")) if not k.startswith("_"))
          == set(list_personas()))

    names = list(schema["properties"])
    obj_field = next(n for n in names if schema["properties"][n]["type"] == "object")
    truth = {obj_field: {"fact": "declined, no number", "all_of": ["declin"], "none_of": ["\\d"]}}
    good = {obj_field: {"value": "Declined to share", "evidence": [2]}}
    bad = {obj_field: {"value": "R50,000 a month", "evidence": [2]}}
    check("checker passes a matching field", check_against_ground_truth(good, truth)[0].passed)
    result = check_against_ground_truth(bad, truth)[0]
    check("checker fails a wrong field and says why",
          not result.passed and "missing" in result.reason and "should not contain" in result.reason)
    evidence_only = {obj_field: {"value": "declined", "evidence": [12345]}}
    check("evidence numbers are not treated as field text",
          check_against_ground_truth(evidence_only, truth)[0].passed)

    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "x.json").write_text(json.dumps({"card": {"name": "X"},
                                                       "ground_truth": {"not_a_field": {"fact": "f"}}}), encoding="utf-8")
        original = personas.PERSONAS_DIR
        import screener.ground_truth as gt
        gt.PERSONAS_DIR = Path(tmp)
        try:
            load_ground_truth("x", schema)
            check("ground_truth with a field not in the schema is rejected", False)
        except ConfigError:
            check("ground_truth with a field not in the schema is rejected", True)
        finally:
            gt.PERSONAS_DIR = original


async def check_rate_limiter():
    section("Gemini pacing (free tier: requests per minute)")
    import time

    from screener.config import load_voice_config
    from screener.llm import RateLimiter, interviewer_llm, limiter_for, persona_llm

    limiter = RateLimiter(per_window=2, window_secs=0.4)
    start = time.monotonic()
    for _ in range(3):
        await limiter.acquire()
    elapsed = time.monotonic() - start
    check("third request in the window waits for a slot instead of failing", 0.35 <= elapsed < 1.5, f"{elapsed:.2f}s")

    voice = load_voice_config()
    check("cap is below Google's free-tier 15 per minute", 0 < voice.llm_max_requests_per_minute < 15,
          f"{voice.llm_max_requests_per_minute}/min")
    check("one shared pacer per model",
          limiter_for("model-a", 5) is limiter_for("model-a", 5) and limiter_for("model-a", 5) is not limiter_for("model-b", 5))

    from pipecat.services.google.llm import GoogleLLMService
    from screener.llm import _paced, reply

    service = GoogleLLMService(api_key="offline-dummy")

    async def fake_network(_ctx):
        async def gen():
            if False:
                yield None
        return gen()

    service._stream_content = fake_network
    _paced(service, RateLimiter(per_window=2, window_secs=0.4))
    start = time.monotonic()
    for _ in range(3):
        await reply(service, LLMContext())
    elapsed = time.monotonic() - start
    check("a paced service's third request waits for a slot", 0.35 <= elapsed < 1.5, f"{elapsed:.2f}s")

    llm = interviewer_llm(voice, load_interview_config(), "offline-dummy")
    persona = persona_llm(voice.llm_model, "x", "offline-dummy", voice.llm_max_requests_per_minute)
    check("interviewer and persona services are both paced",
          llm._stream_content.__name__ == "paced" and persona._stream_content.__name__ == "paced")


async def main():
    check_voice_config()
    await check_gemini_retry()
    cfg = check_interview_config()
    check_config_validation()
    check_transcript(build_opening_line(cfg))
    await check_normal_finish_hangup(cfg)
    await check_time_limit_hangup(cfg)
    await check_real_aggregator()
    await check_turn_start()
    check_personas()
    check_interviewer_is_shared()
    await check_simulated_request(cfg)
    await check_simulation_loop(cfg)
    await check_reply_parsing_and_counting()
    await check_rate_limiter()
    check_profile_schema_and_prompt()
    await check_profile_paths()
    check_evidence_rules()
    check_ground_truth()
    print(f"\n{sum(results)}/{len(results)} passed")
    return all(results)


if __name__ == "__main__":
    sys.exit(0 if asyncio.run(main()) else 1)
