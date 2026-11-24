# Voice Screener

A voice agent that holds a three-minute candidate screening call in the browser. After the call it writes a structured candidate profile in which every field cites the transcript turns it came from. It then rates the candidate against three sample roles and recommends a next step. A person approves or rejects that step on a local review page.

It is built on [Pipecat](https://github.com/pipecat-ai/pipecat) and runs locally on Windows. Every service it uses is on a free tier or free sign-up credit. The interview script, profile fields, roles and limits are all JSON files in `config/`, so the screener can be changed without touching code.

All candidate data in this repo is invented. Nothing is ever sent to anyone: no email, no webhooks, no messages.

---

## What happens on a call

1. You open the call page in the browser and click **Connect**. Sam, the AI interviewer, introduces itself as an AI and says the call takes about three minutes.
2. Sam asks six topics in order, one question at a time:
   - current role
   - strongest skills, with an example
   - what you want next
   - work arrangement and location
   - notice period
   - salary expectation (optional; declining is accepted)
3. You can interrupt Sam at any time. It stops talking and listens.
4. Sam ends the call in one of two ways:
   - after the last topic, by saying the closing line and hanging up
   - at five minutes, with a polite goodbye (enforced by a timer in code)
5. The transcript is saved, and Gemini turns it into a profile that is checked against a JSON Schema. Every evidence turn number is checked in code against the transcript.
6. Gemini rates each role requirement as met, partly met, not met or unknown. Code turns those ratings into **advance**, **follow up** (with the questions to ask) or **not a fit**.
7. The recommendation starts as **pending** until a person approves or rejects it on the review page.

---

## Architecture

```
 Browser (localhost:7860/client)
        │  WebRTC (SmallWebRTC, peer-to-peer, no account)
        ▼
 ┌──────────────────────── Pipecat pipeline (bot.py, local) ────────────────────────┐
 │                                                                                  │
 │  audio in ─► Deepgram Nova-3 ─► user turn ─► Gemini Flash-Lite ─► ElevenLabs ─► audio out
 │              streaming STT      Silero VAD    interviewer prompt    Flash v2.5   │
 │                                 (barge-in)    + end-interview tool   TTS         │
 │                                 Smart Turn                                       │
 │                                 (end of turn)                                    │
 │                                                                                  │
 │  observers: reply latency per turn, usage (audio seconds, characters)            │
 │  call timer: hard stop at 5 minutes                                              │
 └──────────────────────────────────────────────────────────────────────────────────┘
        │ when the call ends
        ▼
 profiles/<call id>/transcript.json, call.json
        │
        ├─► Gemini structured output ─► profile.json   (schema-validated, evidence-checked)
        │
        └─► Gemini rates requirements ─► code decides ─► matches.json  (status: pending)
                                                              │
                                                              ▼
                                   Review page (review_server.py, 127.0.0.1:7870)
                                   approve / reject, written back to matches.json

 logs/usage.csv: one row per call      logs/latency.log: one line per candidate turn
```

`simulate.py` runs the same interviewer, built by the same function, against a second Gemini model playing a candidate from a character card. It runs the same profile and matching steps, then checks the results against known answers. It uses no voice, so it spends no Deepgram or ElevenLabs credit.

### What runs where

| Piece | Runs | Cost |
|---|---|---|
| Pipecat, dev runner, SmallWebRTC | Local | Free, open source |
| Silero VAD, Smart Turn | Local | Free |
| Deepgram streaming speech-to-text | Cloud | $200 sign-up credit, no card |
| Gemini Flash-Lite | Cloud | Free tier (Google AI Studio key) |
| ElevenLabs Flash text-to-speech | Cloud | Free plan, 10,000 credits a month |
| Review page | Local | Free |

### Project layout

```
bot.py               voice bot (Pipecat dev runner)
simulate.py          text-only simulated calls with pass/fail checks
review_server.py     local review page server
usage_report.py      this month's usage against the free limits
check_keys.py        checks each .env value without spending credit
build_profile.py     rebuild a saved call's profile
match_roles.py       re-run role matching for a saved call
screener/            the code those scripts share
config/              everything you can change without code (see below)
review/review.html   the review page (one file, vanilla JS)
tests/offline_checks.py   offline checks: no network, no keys, no credit
```

---

## Setup on Windows

You need:

- Windows 10 or 11
- PowerShell
- Git
- Chrome or Edge
- a microphone
- **headphones**: the bot hears itself through laptop speakers and interrupts itself

Type each line into PowerShell and press Enter after each one.

### 1. Install uv (Python package manager)

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

The installer says where it put `uv`. Close PowerShell, open a new one, then:

```powershell
uv --version
```

You should see `uv` followed by a version number.

### 2. Get the code and install it

```powershell
git clone https://github.com/coder01128/voice-screener.git
```

```powershell
cd voice-screener
```

```powershell
uv sync
```

uv downloads Python 3.12 if you don't have it, then installs Pipecat and the other packages. You should see `Installed … packages` followed by a list of package names, each starting with `+`.

### 3. Create the three accounts and keys

None of these needs a card.

- **Deepgram** (speech-to-text):
  1. Sign up at deepgram.com.
  2. In the console, open **API Keys** and create a key.
  3. Check that the $200 credit shows on the billing page.
- **Google AI Studio** (Gemini):
  1. Sign in at aistudio.google.com.
  2. Click **Get API key**, then **Create API key**.
  3. Find the rate limits for the Flash-Lite model and note the requests per day.
- **ElevenLabs** (text-to-speech):
  1. Sign up at elevenlabs.io.
  2. Under your profile, open **API Keys** and create a key with **Text to Speech** and **Voices** access.
  3. Open **Voices**, pick a voice and copy its **voice ID**.

### 4. Put the keys in `.env`

```powershell
Copy-Item .env.example .env
```

```powershell
notepad .env
```

Paste each value after its `=` sign, then save and close Notepad:

```
DEEPGRAM_API_KEY=
GOOGLE_API_KEY=
ELEVENLABS_API_KEY=
ELEVENLABS_VOICE_ID=
```

Git ignores `.env`, so your keys are never committed. To confirm:

```powershell
git check-ignore .env
```

You should see `.env`.

### 5. Check the keys and the code

```powershell
uv run python check_keys.py
```

You should see three lines starting `OK`, for Deepgram, Gemini and ElevenLabs. This spends no credit. A `FAIL` line says which key is wrong.

```powershell
uv run python tests/offline_checks.py
```

You should see `PASS` lines ending with `176/176 passed`. This runs offline and uses no keys. A few `WARNING` lines about `503 UNAVAILABLE` or `boom` are expected: they come from the checks that simulate failures.

### 6. Make a call

Put your headphones on.

```powershell
uv run bot.py -t webrtc
```

Wait for `🚀 Bot ready! (WebRTC)` and `→ Open: http://localhost:7860`. Then:

1. Open **http://localhost:7860/client** in Chrome or Edge.
2. Click **Connect** and allow the microphone.
3. Sam greets you and starts the interview.

When the call ends, PowerShell logs:

- `transcript saved`
- `profile valid`
- `matches valid`
- `usage logged`

The call's files are in `profiles/<date-time>/`. Press **Ctrl+C** to stop the bot.

To play a candidate, read a character card in `config/personas/`, for example `thandi.json`, and answer as that person. Only the `card` section is for you; `ground_truth` is the answer key.

### 7. Review the call

```powershell
uv run python review_server.py
```

You should see `Review page: http://127.0.0.1:7870  (Ctrl+C to stop)`. Open that address. The page lists every call, newest first. For the selected call it shows the profile, the role matches with **Approve** and **Reject** buttons, and the transcript. Clicking a **Turn N** chip jumps to that turn.

### 8. Run the simulated calls

```powershell
uv run python simulate.py --all
```

This runs three text-only calls, one per character card. Each prints:

- the conversation
- the profile, with a PASS or FAIL per field against the card's ground truth
- the role matches, with a PASS or FAIL against `config/personas/expected.json`
- its Gemini request count

The run ends with a summary. It takes a few minutes because of the per-minute pacing described below. Run it once per change, not in a loop.

### 9. Check your usage

```powershell
uv run python usage_report.py
```

This prints this month's calls, Deepgram minutes and estimated dollars (with rand), ElevenLabs characters against the monthly credits, and Gemini requests today and this month. Use `--month 2026-11` for another month.

---

## Free-tier notes

These figures were checked on 24 Nov 2026. Free tiers change, so your own dashboards are the source of truth. The figures the report uses are in `config/usage.json`, with notes on where each came from.

- **Gemini.** The binding limit is **15 requests per minute per model**, not the daily limit. That figure is confirmed by a real 429 error from Google.
  - The code paces itself at 14 a minute, so it waits instead of failing. Simulated runs are where you notice this, because the interviewer and the candidate share one model.
  - Google doesn't publish the free-tier daily limit; AI Studio shows it for your project. The report assumes about 500 a day (a developer report, not confirmed) until you set the real figure in `config/usage.json`. Daily counts reset at midnight Pacific time.
  - Busy periods return `503 high demand`. Post-call steps and simulated turns retry with a doubling wait.
  - Google may use free-tier prompts to improve its products, which is one reason this repo uses invented candidates only.
- **ElevenLabs.** The free plan gives 10,000 credits a month.
  - Flash v2.5 costs 0.5 to 1 credit per character, depending on the source. The report assumes 1, the cautious figure.
  - In test calls, Sam spoke 750–1,650 characters per call, so the free plan covers roughly six to ten calls a month. Credits reset on your sign-up date each month.
- **Deepgram.** The $200 sign-up credit is a one-off, not monthly.
  - Streaming Nova-3 is listed at between $0.0048 and $0.0077 a minute. The report uses $0.0077, the higher figure.
  - The whole call's audio is streamed, silence included: test calls sent 3.5 to 5 minutes each, about 3–4 US cents (under R1).
- **Usage log.** `logs/usage.csv` gets one row per call, voice or simulated, with:
  - date and call length
  - Deepgram audio seconds
  - ElevenLabs characters
  - Gemini requests
  - median reply latency

  The audio seconds and characters are the figures Pipecat's own services report. Re-runs of `build_profile.py` and `match_roles.py` aren't logged; they print their own request counts.

---

## Changing the screener without code

| File | What it controls |
|---|---|
| `config/interview.json` | Agent name, role context, the six topics (question, objective, up to two follow-ups, optional), opening and closing lines, call length |
| `config/profile.schema.json` | Profile field names and descriptions. The single source of truth: the extraction prompt, the validator and the review page all read it |
| `config/roles.json` | The three roles, their must-haves and nice-to-haves, and the follow-up question for each |
| `config/extraction.json`, `config/matching.json` | Post-call prompts and labels |
| `config/voice.json` | Speech-to-text, LLM and text-to-speech models; retry and pacing settings |
| `config/personas/*.json` | Character cards (`card` for the person or model playing the candidate, `ground_truth` for the checker) and `expected.json` outcomes |
| `config/usage.json` | Free-tier limits the usage report compares against |
| `config/review.json` | Review page port and decision labels |

Edit a topic's question in `interview.json` and the next call asks the new wording. The bot re-reads config at the start of each call. Edit a must-have in `roles.json` and the next match changes.

---

## Design decisions

- **A cascade (speech-to-text → LLM → text-to-speech) instead of a speech-to-speech model.** Each stage can be swapped, measured and logged on its own, which is how most production voice agents on Pipecat are built.
- **SmallWebRTC.** It is peer-to-peer and free, with no Daily account. Pipecat's docs recommend Daily for production with many users on varied networks; this demo has one user on localhost.
- **Gemini Flash-Lite.** A screening call needs speed more than reasoning depth, and Flash-Lite has the most usable free tier. The limit that actually bites is 15 requests a minute, not the daily cap. That is why every Gemini request goes through one per-model pacer and post-call steps retry on errors.
- **Evidence on every field.** A person can check each claim against what the candidate actually said rather than trusting a summary. Turn numbers are checked in code. A field citing a turn that doesn't exist, or only the interviewer's words, is flagged.
- **"not discussed", never a guess.** A field the candidate didn't cover says so with no evidence. Nothing is inferred.
- **The recommendation is computed in code, not by the LLM.**
  - Any must-have not met gives **not a fit**.
  - Any must-have unknown gives **follow up**, with that requirement's question.
  - Otherwise the result is **advance**.

  The rule is fixed, testable and explainable; the LLM only rates requirements against the transcript.
- **Nothing is sent automatically.** A hiring decision affects a real person, so a human approves it on the review page.
- **The 5-minute stop is in code.** A prompt can be ignored; a timer can't. The bot hangs up itself, after the goodbye finishes playing.
- **A text simulation mode.** It tests the profile and matching logic against known answers without spending voice credit. The interviewer prompt and Gemini service come from shared code, so a simulated call tests the same interviewer as a voice call. Simulated and voice transcripts use the same format, and nothing downstream branches on which mode produced them.
- **Ground truth on the personas.** The profile is checked against facts written before the call, so accuracy is measured rather than eyeballed. The model playing the candidate only ever sees the `card` section.
- **Candidate turns start on voice activity only.** Pipecat's default also starts a turn when a transcript arrives. Late transcript segments then cut Sam off and made it ask the candidate to repeat answers it already had.
- **Headphones, not a mute.** Echo from laptop speakers can make the bot interrupt itself. The fix is headphones, not muting the candidate's microphone in code. The only time the candidate is muted is the final goodbye, after the interview is over.

---

## Glossary

- **VAD (voice activity detection):** a small local model (Silero) that decides, frame by frame, whether someone is speaking.
- **Turn-taking:** deciding whose turn it is to speak. Here, VAD starts the candidate's turn and a local end-of-turn model (Smart Turn) decides when it's over.
- **Barge-in / interruption:** the candidate speaks while the bot is talking, and the bot stops and listens.
- **End-of-turn latency:** time from the candidate actually stopping to the bot's first audio. Logged per turn in `logs/latency.log`; the median per call is in `logs/usage.csv`.
- **Streaming STT:** speech-to-text that receives audio continuously and returns words while the person is still speaking, instead of after a recording.
- **Frames and processors (Pipecat):** everything in the pipeline (audio, text, control signals) travels as a frame. Processors receive frames, act on them and pass them on.
- **WebRTC:** the browser standard for real-time audio and video between two peers.
- **STUN / TURN:** helpers that let two WebRTC peers find each other across networks. STUN discovers your public address; TURN relays traffic when a direct path is blocked. Not needed on localhost.
- **Structured output:** asking the LLM to reply in JSON that follows a given schema, instead of free text.
- **JSON Schema validation:** checking the reply against that schema in code before trusting it. An invalid reply is retried once, then saved as `"status": "invalid"` rather than guessed.
- **Evidence grounding:** tying each extracted fact to the transcript turns it came from, so it can be checked.

---

## Known limits

- **Gemini's time to first token sometimes spikes** to 10+ seconds on Google's side. A 3-second slow-start retry (`config/voice.json`) cuts most of these, but not all.
- **Runs locally only.** The demo is shown through a screen recording and this repo, never a public URL.
- **The usage report is an estimate.** It uses the figures in `config/usage.json`; each service's own dashboard shows the exact balance.
