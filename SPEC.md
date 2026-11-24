# Voice Screener: Build Spec

A voice agent that runs a short candidate screening call in the browser, then writes a structured candidate profile, matches it against three sample roles and recommends a next step for a person to approve.

**Why it exists:** a portfolio demo for the Jem & I application (voice-first AI recruiter, stack includes Pipecat, ElevenLabs, Gemini, WebRTC, MCP). It is also reusable as a happyai voice intake, because the interview script and output schema are config files, not code.

**Cost rule:** every service runs on a free tier or free sign-up credit. No card on file anywhere. Runs locally only; employers see it through a Loom walkthrough and the GitHub repo, never a public URL that strangers could use to burn the free credits.

---

## 1. Architecture

```
Browser (localhost)  ──WebRTC (SmallWebRTC, peer-to-peer)──►  Pipecat bot (Python, local)
                                                                 │
                         ┌───────────────────────────────────────┤ pipeline
                         ▼                                       │
               Silero VAD (local, free)                          │
                         ▼                                       │
               Deepgram streaming STT ($200 free credit)         │
                         ▼                                       │
               Gemini Flash-Lite LLM (free tier)                 │
                         ▼                                       │
               ElevenLabs Flash TTS (free plan)  ────────────────┘
                                                   on disconnect
                                                         ▼
                    Transcript ─► Gemini structured output ─► profile.json (schema-validated)
                                                         ▼
                                     Match against 3 sample roles ─► recommendation (pending human approval)
                                                         ▼
                                     Review page (single HTML file, localhost)
```

### What runs where

| Piece | Runs | Cost | Account needed |
|---|---|---|---|
| Pipecat framework + dev runner | Local | Free, open source | No |
| SmallWebRTC transport | Local, peer-to-peer | Free, no API key | No |
| Silero VAD | Local | Free | No |
| Deepgram streaming STT | Cloud | $200 sign-up credit, no card | Yes |
| Gemini Flash-Lite | Cloud | Free tier | Google AI Studio key |
| ElevenLabs Flash TTS | Cloud | Free plan, 10k credits/month | Yes |
| Review page | Local | Free | No |

**Cloud dependencies, and why each is unavoidable:** speech-to-text, the LLM and text-to-speech. All three can be swapped for local models later, but local voice models on a Windows laptop would add latency and setup risk to a demo whose whole point is a natural conversation.

### Free-tier budget (checked 27 Oct 2026, verify before relying on it)

- **ElevenLabs free plan:** 10k credits/month, about 10 minutes of TTS per the pricing page. Flash models may stretch further; unconfirmed. No commercial licence on Free, which is fine for a demo. If credits run out, Starter is $1 for the first month.
- **Deepgram:** $200 sign-up credit, no card. Whether it covers streaming STT is not stated on the pricing page. Ticket 00 confirms it in the Deepgram console.
- **Gemini free tier:** Google doesn't publish free-tier limits; they show per project in AI Studio. Developer reports (Sep 2026, unverified) put Flash at about 20 requests/day and Flash-Lite at about 500/day. One call is roughly 10–15 turns plus 2 post-call requests, so **use Flash-Lite**. Free-tier prompts may be used by Google to improve products, so **synthetic data only, never a real candidate**.

**Credit maths for TTS:** the bot speaks about 40% of a 3-minute call, so roughly 1 minute of TTS per call, which gives about 10 full test calls a month on the free plan. Hence the hard rules below: short bot turns and a hard stop at 5 minutes.

**Voice-call budget across the build:** Ticket 01 needs 1 call and Ticket 02 needs 3. From Ticket 02B onwards, profile and matching are tested in text mode, which uses no ElevenLabs or Deepgram credit, leaving about 6 voice calls for final checks and the Loom recording.

**Text-mode budget:** a simulated call is about 26 Gemini requests (12 interviewer turns, 12 persona turns, 2 post-call). At the reported ~500 Flash-Lite requests a day, that's roughly 15–20 simulated calls a day. Run the full persona set once per change, not in loops.

---

## 2. Scope

**In:** browser voice call, barge-in (user can interrupt the bot), a fixed screening script, three character cards for playing the candidate, a text-only simulation mode, post-call profile, matching against three synthetic roles, a recommended next step that a person approves or rejects, a review page, and a per-call usage log.

**Out:** video, phone numbers or telephony, public hosting, real candidates, sending anything to anyone, accounts or logins, a database, MCP (possible v2: expose profiles and roles as MCP tools).

---

## 3. Tickets

Each ticket ends with acceptance tests. Do not start the next ticket until Brad has run the tests and confirmed.

### Ticket 00: Environment and accounts

**Brad does by hand, before Claude Code starts** (keys are never printed or handled by Claude Code):

1. Create a Deepgram account at deepgram.com. Open the API Keys page and create a key. In the billing or usage page, check that the $200 credit appears and whether it covers streaming. Note the answer.
2. Create an ElevenLabs account at elevenlabs.io. Open the API Keys page under your profile and create a key. Note your remaining credits.
3. Open aistudio.google.com, sign in, and click "Get API key", then "Create API key". In AI Studio, find the rate limits for the Flash-Lite model and note them.
4. Open PowerShell, then type each line and press Enter after each:
   ```
   mkdir C:\ccode\git-repos\voice-screener
   cd C:\ccode\git-repos\voice-screener
   ```
   The prompt should now end in `voice-screener>`.
5. Copy `SPEC.md` and `CLAUDE.md` into that folder.
6. Type `claude` and press Enter to start Claude Code in that folder.

**Claude Code does:**

- Check for Python 3.12 (Pipecat needs 3.11+) and `uv`. If either is missing, stop and give Brad the exact install steps for Windows.
- Confirm against the current Pipecat docs that Pipecat runs on native Windows. If it doesn't, stop and say so; the fallback is WSL, which is Brad's decision.
- Install Pipecat with the extras for SmallWebRTC, the dev runner, Silero, Deepgram, Google and ElevenLabs. Take the extra names from the current Pipecat docs, not from memory.
- Create `.env.example` with these names and nothing else: `DEEPGRAM_API_KEY`, `GOOGLE_API_KEY`, `ELEVENLABS_API_KEY`, `ELEVENLABS_VOICE_ID`. Create `.gitignore` with `.env`, `profiles/`, `logs/`.
- Tell Brad to copy `.env.example` to `.env` and paste his keys in Notepad. Never read or print `.env`.

**Acceptance:** `uv run python -c "import pipecat; print(pipecat.__version__)"` prints a version. `.env` exists and is git-ignored (`git check-ignore .env` prints `.env`).

### Ticket 01: The voice loop

- `bot.py`: Pipecat pipeline using SmallWebRTC transport → Silero VAD → Deepgram streaming STT → Gemini Flash-Lite → ElevenLabs Flash TTS → transport out.
- Use the Pipecat dev runner and its built-in browser client at localhost. No custom front end in this ticket.
- Interruptions on: if Brad speaks while the bot is talking, the bot stops.
- Placeholder system prompt: "You are a friendly assistant. Keep every reply to one or two short sentences."
- Model names and voice ID come from config or `.env`, never hard-coded in pipeline code. Look up current model names in the Gemini and ElevenLabs docs.
- Log per turn: time from end of user speech to first bot audio. Write to `logs/latency.log`.

**Acceptance:**
1. Brad opens the localhost page, clicks connect, allows the microphone and holds a 1-minute conversation.
2. Brad interrupts the bot mid-sentence; it stops within about a second.
3. `logs/latency.log` shows one line per turn.
4. Closing the tab ends the bot cleanly with no stack trace.

### Ticket 02: The screening interview

- `config/interview.json`: agent name ("Sam", never "Jem"), role context, and an ordered list of topics, each with an objective and a maximum of two follow-up questions:
  1. Current role and what they do day to day
  2. Strongest skills, with one concrete example
  3. What they want next
  4. Work arrangement (remote, hybrid, office) and location
  5. Notice period and availability
  6. Salary expectation (optional: if the candidate declines, accept it and move on)
- System prompt is built from the config at start-up. Rules in the prompt: one question at a time, one or two sentences per turn, never give advice or opinions about the candidate, never promise an outcome.
- Bot opens by introducing itself, saying it's an AI and the call takes about three minutes.
- Close: thanks the candidate and says a person will review the notes.
- **Hard stop at 5 minutes:** the bot wraps up politely and ends the call, enforced in code, not the prompt.
- Full transcript saved on disconnect to `profiles/<timestamp>/transcript.json` as an array of `{turn, speaker, text, started_at}`.

**Acceptance (3 voice calls in total):**
1. Call one: Brad plays Thandi from `config/personas/` (see Ticket 02B; Claude Code writes the three cards in this ticket so they exist for testing). The call covers all six topics in order in about three minutes. Brad declines the salary question, as the card says, and the bot moves on without pushing.
2. Call two: Brad talks past five minutes; the bot ends the call itself.
3. The transcript files exist with turn numbers.
4. Call three: after changing a topic's wording in `interview.json`, the bot asks the new wording, with no code change.

### Ticket 02B: Character cards and text simulation mode

**Character cards** live in `config/personas/`, one JSON file each. Each card has two parts:
- `card`: a short brief Brad reads before playing the candidate (name, age, situation, strongest skill with one example, what they want next, arrangement, location, notice, salary stance, and one behaviour such as rambling or changing their mind).
- `ground_truth`: the facts the profile should capture, in the same field names as `config/profile.schema.json`. Used for automatic checking; never shown to the interviewer bot.

The three cards, written to match the roles in Ticket 04:

| Card | Situation | Behaviour | Expected outcome |
|---|---|---|---|
| Thandi, 29 | Junior React developer at a Durban fintech, 18 months. Built the customer dashboard. Wants remote with more backend work. One month notice. | Won't give a salary figure. | React role: **advance** |
| Pieter, 41 | Data engineer, ten years, strong in SQL and pipelines. Wants hybrid in Johannesburg. | Rambles; never mentions cloud platforms. | Data engineer role: **follow up**, naming the cloud must-have |
| Lerato, 34 | Operations coordinator at a logistics firm. Wants to move into tech, no coding experience. | Changes her answer on notice period mid-call. | React and data roles: **not a fit**; ops role: **advance** |

All names and details are invented. Claude Code writes the roles in Ticket 04 so these outcomes are the correct ones, and records the expected outcomes in `config/personas/expected.json`.

**Text simulation mode:** `uv run python simulate.py --persona thandi` (or `--all`).
- The interviewer uses exactly the same system prompt built from `interview.json` as the voice bot. Shared code, not a copy, so the two can't drift.
- A second Gemini Flash-Lite call plays the candidate from the `card` only (never `ground_truth`), in character, including the behaviour.
- The same 5-minute limit applies as a turn limit (about 16 interviewer turns).
- No Deepgram, no ElevenLabs. Output is a transcript in exactly the same format as a voice call, saved to `profiles/<timestamp>-sim-<persona>/`, marked `"mode": "simulated"`.
- After Tickets 03 and 04 exist, a simulated run also produces the profile and matches, then prints a check: each profile field compared against `ground_truth`, and each recommendation against `expected.json`, as pass or fail with the reason.
- Prints the number of Gemini requests the run used.

**Acceptance:**
1. `simulate.py --persona thandi` produces a transcript covering all six topics, and the persona declines the salary question.
2. The interviewer prompt is built by the same function the voice bot uses (Brad asks Claude Code to show where).
3. The run prints its Gemini request count.

### Ticket 03: Candidate profile

- `config/profile.schema.json`: JSON Schema for the profile. Fields: `summary`, `current_role`, `skills[]` (each with `name` and `example`), `wants_next`, `work_arrangement`, `location`, `notice_period`, `salary_expectation`, `open_questions[]`.
- Every field carries `evidence`: the transcript turn numbers it came from. A field the candidate didn't cover is `"not discussed"` with empty evidence. **Nothing inferred, nothing invented.**
- On disconnect: send the transcript to Gemini Flash-Lite with structured output against the schema. Validate the response in code. If validation fails, retry once, then save the raw response with `"status": "invalid"` rather than a guessed profile.
- Code check after generation: every evidence turn number must exist in the transcript. Any that don't are flagged in the profile as `evidence_error`.
- Save to `profiles/<timestamp>/profile.json`.

**Acceptance (text mode, no voice calls):**
1. `simulate.py --all` produces three profiles that validate against the schema, and the ground-truth check passes for every field on all three.
2. Spot check: Brad traces three fields by hand to the cited transcript turns. All three match.
3. Thandi's `salary_expectation` is `"not discussed"` or records that she declined, never a number.
4. Deliberately break the schema (rename a required field) and run once: the invalid path saves the raw output and marks it invalid rather than crashing.
5. The profile step works on the voice transcripts saved in Ticket 02, with no code difference between voice and simulated transcripts.

### Ticket 04: Matching and next step

- `config/roles.json`: three synthetic roles: a junior React developer (remote allowed), a data engineer (must-haves include SQL, pipelines and a cloud platform; hybrid Johannesburg) and an operations coordinator. Each has must-haves and nice-to-haves, written so the persona outcomes in Ticket 02B are the correct ones.
- For each role, Gemini rates every requirement as met, partly met, not met or unknown, with evidence turn numbers from the transcript, and gives a one-line reason.
- The recommendation is calculated **in code** from those ratings, not decided by the LLM: any must-have rated not met gives "not a fit"; any must-have unknown gives "follow up", with the specific questions to ask; otherwise "advance".
- Recommendation status starts as `pending`. Nothing is sent anywhere. A person changes it to approved or rejected on the review page.
- Save to `profiles/<timestamp>/matches.json`.

**Acceptance (text mode, no voice calls):**
1. `simulate.py --all` matches every outcome in `expected.json`: Thandi advances for React, Pieter gets "follow up" naming the cloud must-have, Lerato advances only for the ops role.
2. Pieter's follow-up question names the missing cloud requirement specifically.
3. Editing a must-have in `roles.json` changes the outcome on re-run, with no code change.
4. Run `--all` three times. If any outcome flips between runs, report which one and why; consistency matters more than any single pass.

### Ticket 05: Review page

- Single vanilla HTML file, served by a small local endpoint that lists and reads the `profiles/` folders. No framework.
- Layout: list of calls on the left; for the selected call, the profile, then the role matches, then the transcript.
- Clicking an evidence number scrolls to and highlights that transcript turn.
- Approve and Reject buttons on each recommendation write the decision and a timestamp back to `matches.json`.
- A "Start a new call" link opens the voice page.

**Acceptance:**
1. All past calls are listed, newest first.
2. Clicking an evidence number highlights the right turn.
3. Approving a recommendation, then reloading the page, keeps the decision.
4. The page passes the design rules in CLAUDE.md at 375px wide and on desktop.

### Ticket 06: Usage log, README and demo script

- Per call, append to `logs/usage.csv`: date, call length, Deepgram audio seconds, characters sent to ElevenLabs, Gemini request count, median latency.
- A command that prints this month's totals against the free limits Brad noted in Ticket 00.
- `README.md`: what it is, architecture diagram, setup with literal Windows commands one per line, the free-tier notes, and a "Design decisions" section (below).
- `DEMO.md`: a 3-minute Loom script covering one live voice call with Brad playing a character card, the profile with an evidence click, the match result, approving it, a quick `simulate.py --all` run showing the pass/fail check, and the config file change.

**Acceptance:** after one call, `logs/usage.csv` has a new row with all six values filled. A fresh clone set up by following the README alone reaches a working call.

---

## 4. Design decisions to record in the README

These are the decisions Brad should be able to explain in an interview.

- **Why a cascade (STT → LLM → TTS) instead of a speech-to-speech model:** each stage can be swapped, measured and logged on its own, and it matches the stack Jem uses.
- **Why SmallWebRTC:** peer-to-peer and free, with no Daily account. Pipecat's docs recommend Daily for production with many users on varied networks; this demo has one user on localhost.
- **Why Flash-Lite:** a screening call needs speed more than reasoning depth, and Flash-Lite has the most usable free tier. In practice the limit that bites is 15 requests per minute per model (confirmed by a real 429), not the daily cap, so every Gemini request goes through one per-model pacer set to 14 a minute.
- **Why evidence on every field:** so a person can check each claim against what the candidate actually said, rather than trusting the summary.
- **Why the recommendation is computed in code, not by the LLM:** the rule is fixed, testable and explainable; the LLM only rates requirements against the transcript.
- **Why nothing is sent automatically:** a hiring decision affects a real person, so a human approves it.
- **Why the 5-minute stop is in code:** prompts can be ignored; a timer can't.
- **Why a text simulation mode:** it tests the profile and matching logic against known answers without spending voice credits, and the interviewer prompt is shared code so a simulated call tests the same interviewer as a voice call.
- **Why personas have ground truth:** the profile is checked against facts written before the call, so accuracy is measured rather than eyeballed.

## 5. Glossary for interview prep (Ticket 06 adds it to the README)

VAD, turn-taking, barge-in/interruption, end-of-turn latency, streaming STT, frames and processors (Pipecat), WebRTC, STUN/TURN, structured output, JSON Schema validation, evidence grounding.
