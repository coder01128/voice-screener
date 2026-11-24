# Demo script (Loom, about 3 minutes)

The demo has six parts:

1. A live voice call, with you playing Thandi from her character card.
2. The profile, with an evidence click.
3. The role match.
4. Approving the match.
5. The `simulate.py --all` pass/fail check.
6. A config change that changes an outcome with no code change.

A full call takes about three minutes on its own. So the live call shows the first minute: the greeting, two answers and an interruption. The profile and match then come from a complete Thandi call you make before recording.

---

## Before you record (about 15 minutes)

1. **Put your headphones on.** Without them the bot hears itself.
2. **Make one complete voice call as Thandi.** Read `config/personas/thandi.json` (the `card` part only) and answer as her. Decline the salary question once. Let Sam finish and hang up. This is the call you'll show on the review page.
3. **Run the simulations** so their output is ready to show. This takes several minutes because Gemini's free tier allows 15 requests a minute.
   ```powershell
   uv run python simulate.py --all
   ```
   It should end with `=== Summary ===`, three lines reading `all checks pass`, and the total request count. Leave this PowerShell window open and don't scroll.
4. **Start the bot** in a second PowerShell window:
   ```powershell
   uv run bot.py -t webrtc
   ```
   Wait for `→ Open: http://localhost:7860`.
5. **Start the review page** in a third PowerShell window:
   ```powershell
   uv run python review_server.py
   ```
6. **Open these in Chrome:**
   - `http://localhost:7860/client`
   - `http://127.0.0.1:7870`

   In the review tab, select your complete Thandi call.
7. **Open `config/roles.json` in Notepad:**
   ```powershell
   notepad config\roles.json
   ```
   Find `Notice period of two months or less.` and leave Notepad open.
8. **Find the Thandi simulation's folder name** for part 6. In a fourth PowerShell window, run:
   ```powershell
   $thandi = (Get-ChildItem profiles -Directory -Filter "*-sim-thandi" | Sort-Object Name | Select-Object -Last 1).Name
   ```
   ```powershell
   $thandi
   ```
   It prints a name ending in `-sim-thandi`.

---

## The recording

### 0:00–0:15 What it is

> "This is a voice screening agent built on Pipecat. It runs a short screening call, writes a candidate profile where every field cites the transcript, matches it against three roles, and recommends a next step that a person approves. Every candidate here is invented, and nothing is ever sent to anyone."

### 0:15–1:10 Live call

1. In the call tab, click **Connect**.
2. Sam introduces itself as an AI and asks about your current role. Answer as Thandi: "I'm a junior React developer at a fintech in Durban, about eighteen months now."
3. When Sam starts its next question, **interrupt it mid-sentence** ("Sorry, one more thing…"). Sam stops and listens.
   > "That's barge-in: voice activity detection starts my turn and the bot stops talking."
4. Answer the skills question, then close the tab.
   > "A full call takes three minutes, so here's a complete one I did earlier."

### 1:10–1:50 Profile and evidence

1. Switch to the review tab, on your complete Thandi call.
2. Scroll the profile:
   > "Every field cites transcript turns. Salary records that she declined, never a guessed number, because nothing is inferred."
3. Click a **Turn N** chip next to notice period. The page jumps to that turn and highlights it.
   > "So anyone can check the claim against what she actually said."

### 1:50–2:15 Match and approval

1. Scroll to **Role matches**.
   - The Junior React Developer role shows **advance**.
   - The other two show their outcome and which requirements drove it.

   > "Gemini only rates each requirement. The recommendation is computed in code: a must-have not met is not a fit, a must-have unknown is a follow-up, otherwise advance."
2. Click **Approve** on the React role. It shows "Approved" with the time.
   > "It's pending until a person approves it. Nothing goes out automatically."

### 2:15–2:35 Simulation check

1. Switch to the simulation PowerShell window and scroll up through the `PASS` lines.
   > "Text simulation mode: a second model plays each character card, then the profile is checked field by field against ground truth written before the call, and every outcome against the expected one."
2. Point at Pieter's **follow up** line, which names the cloud platform requirement, then at the summary: three times `all checks pass`.

### 2:35–2:55 Config change, no code

1. In Notepad, change `two months or less` to `two weeks or less` and save.
   > "Thandi's notice period is one month. Tighten the requirement in config…"
2. In the fourth PowerShell window, run:
   ```powershell
   uv run python match_roles.py $thandi
   ```
   The React role now shows **not a fit** on the notice requirement.
   > "…and the outcome changes, with no code change."

### 2:55–3:00 Close

> "Every service is on a free tier, it runs locally, and the code and design decisions are in the repo."

---

## After recording

Put the role back:

```powershell
git checkout config/roles.json
```

Then re-run the match so Thandi's simulated call shows **advance** again:

```powershell
uv run python match_roles.py $thandi
```

Check your usage:

```powershell
uv run python usage_report.py
```
