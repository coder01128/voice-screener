# CLAUDE.md: Voice Screener

Rules for the build agent. Read SPEC.md for what to build. Work one ticket at a time and stop after each ticket's acceptance tests so Brad can run them.

## Project
- Python bot built on Pipecat. Runs locally on Windows. No public hosting.
- Package manager: `uv`. Python 3.12 (Pipecat needs 3.11+).
- All services must stay on free tiers or free sign-up credit. If a step would need a card or a paid plan, stop and ask.

## Hard rules
- Never read, print or log `.env` or any key. Never run a command that prints a secret. Keys are pasted by Brad.
- Env var names are exactly: `DEEPGRAM_API_KEY`, `GOOGLE_API_KEY`, `ELEVENLABS_API_KEY`, `ELEVENLABS_VOICE_ID`. Grep the repo before finishing a ticket to confirm no other spelling exists.
- No model names, voice IDs or prompt text hard-coded in pipeline code. They live in `config/` or `.env`.
- Look up current Pipecat, Gemini and ElevenLabs API details in their docs. Don't write integration code from memory. If unsure, say so and check.
- Synthetic data only. No real candidate names or details anywhere, including test fixtures.
- Nothing is sent to anyone. No email, no webhooks, no outbound messages.
- Commands given to Brad must be Windows (PowerShell) syntax, one per line, with what he should see afterwards.
- Never deploy, and never run any Vercel CLI command.
- Don't mark a ticket done until its acceptance tests pass. If something can only be verified by Brad (microphone, audio), say so plainly rather than claiming it works.

## Data contract
- `config/profile.schema.json` is the single source of truth for profile field names. The extraction prompt, the validator and the review page all read from it. Never duplicate field names by hand.
- Transcript turn numbers are the evidence key. Every evidence reference must point to a turn that exists; check it in code.
- Fields the candidate didn't cover are `"not discussed"`. Never infer or fill in.
- The voice bot and `simulate.py` build the interviewer prompt with the same function. Never copy it.
- Simulated and voice transcripts use the identical format. Nothing downstream may branch on `mode`.
- The persona-playing model only ever sees a card's `card` section. `ground_truth` is read only by the checker.
- Don't run simulations in loops. Every run prints its Gemini request count; the free tier is the budget.

## Design rules (review page and any UI)
- Text contrast at least 4.5:1. Minimum font size 13px.
- On dark backgrounds, text is off-white or another strong-contrast colour, never grey on grey.
- Buttons are unmistakably clickable: a filled background for primary actions, a visible coloured border for secondary ones. Never the same colour as the page background.
- Every interactive element has visible hover feedback: pointer cursor plus a visual change.
- Modals are centred, never bottom-anchored sheets.
- Dropdowns are sorted alphabetically.
- Works at 375px wide and on desktop with no horizontal scroll.
- Single HTML file, vanilla JS, no framework.
