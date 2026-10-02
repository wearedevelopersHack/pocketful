#!/usr/bin/env bash
# Heartbeat for the Session Reporter agent.
#
# Band agents are event-driven: they only wake when something @-mentions them.
# There is no "run every hour" switch on an agent itself. This script is that
# switch — it drops a mention into the reporter's room on a schedule, and the
# mention is what wakes the agent to write a digest.
#
# Installed in the user crontab. To change the schedule:  crontab -e
# To pause it:                                            crontab -r
# To watch it fire:                                       tail -f ~/.local/state/pocketful-reporter.log

set -uo pipefail

BAND=/usr/bin/band
ROOM=89251919-52e8-45d4-90dc-b579114a8c47        # the reporter's room ("New Session" #2)
REPORTER=402fad48-206c-4529-bf94-09e44dacf965    # session-reporter's identity id
WATCHED=a0449e0d-0400-4748-80ab-cc2783c3634e     # the room being reported on
LOG="${XDG_STATE_HOME:-$HOME/.local/state}/pocketful-reporter.log"

mkdir -p "$(dirname "$LOG")"

MSG="Digest time. Read the New Session room ($WATCHED) for anything since your last digest, then post a short update in this room: what the team is working on, what landed, what is blocked, and anything that needs the human. If nothing has changed since your last digest, reply with one line saying so and stop."

{
  printf '\n=== %s ===\n' "$(date -Is)"
  "$BAND" room send "$ROOM" --mention "$REPORTER" "$MSG"
  printf 'exit=%s\n' "$?"
} >>"$LOG" 2>&1
