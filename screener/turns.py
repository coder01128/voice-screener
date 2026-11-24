"""How the bot decides the candidate has started a turn.

Pipecat's default starts a user turn on either VAD speech or a transcript.
The transcript trigger misfires on long answers with pauses: the end-of-turn
model releases the turn on one finalized Deepgram segment, the answer's last
segment lands ~0.1s later, and that late text "starts" a new turn. That cuts
the bot off, and with no new speech the turn ends empty, so Pipecat's empty-
turn recovery has the bot ask the candidate to repeat something it already
has (seen in Ticket 02 call 2).

Starting turns on VAD alone keeps barge-in on real speech. Late text is not
lost: the user aggregator holds it and it opens the candidate's next turn.
The stop side keeps Pipecat's default (Smart Turn, waiting for a transcript).
"""

from pipecat.turns.user_start.vad_user_turn_start_strategy import VADUserTurnStartStrategy
from pipecat.turns.user_turn_strategies import UserTurnStrategies


def candidate_turn_strategies() -> UserTurnStrategies:
    return UserTurnStrategies(start=[VADUserTurnStartStrategy()])
