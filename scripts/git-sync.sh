#!/usr/bin/env bash
# Routine 2-hourly snapshot: commit the working tree, push it, and get out of the
# way. Installed in cron; see the tail of this file for the schedule.
#
# WHY THIS IS A SCRIPT AND NOT AN AGENT
# -------------------------------------
# Band agents are event-driven — they only wake when something mentions them.
# "Push every two hours" therefore always means: something on a timer mentions
# the agent, or something on a timer does the push itself. The work here is
# deterministic (add, commit, push), so a shell script does it exactly, for free,
# and can never report a push that did not happen. The Git Keeper seat exists to
# handle the case this script cannot: a failure that needs a diagnosis. It is
# mentioned in the room only when something goes wrong.
#
# WHAT IT GUARANTEES
# ------------------
#   * An unchanged tree exits 0 with no commit, so the history stays meaningful.
#   * A credential-shaped string in the staged diff aborts the commit. The guard
#     is allowed to fail the push; it is never allowed to publish a secret.
#   * Authentication is non-interactive. There is no TTY on cron, so GIT_TERMINAL
#     _PROMPT is disabled and a missing token fails loudly instead of hanging.
#   * A failure is reported into the Band room, not just into a log nobody reads.
#
# Exit codes: 0 = pushed or nothing to do, 1 = failed (reported to the room).

set -uo pipefail   # deliberately NOT -e: every failure below has its own report.

# cron runs with a minimal PATH (/usr/bin:/bin) and this box keeps git and band
# under ~/.local/bin, so without this line the script dies at the first command
# with "git: command not found" — and reports it as a git failure.
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:${PATH:-}"

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROOM="a0449e0d-0400-4748-80ab-cc2783c3634e"      # New Session — the team room
KEEPER="a0ad0555-bd78-42cf-9001-dfa5eec66a31"    # Git Keeper identity id
TOKEN_FILE="${POCKETFUL_GITHUB_TOKEN_FILE:-$HOME/.config/pocketful/github-token}"
LOG="${XDG_STATE_HOME:-$HOME/.local/state}/pocketful-git-sync.log"
BRANCH=main

BAND=""
for c in /usr/bin/band "$HOME/.local/bin/band" "$(command -v band 2>/dev/null || true)"; do
    [ -n "$c" ] && [ -x "$c" ] && { BAND="$c"; break; }
done

export GIT_ASKPASS="$REPO/scripts/git-askpass.sh"
export GIT_TERMINAL_PROMPT=0
export POCKETFUL_GITHUB_TOKEN_FILE="$TOKEN_FILE"

mkdir -p "$(dirname "$LOG")"

log() { printf '%s %s\n' "$(date -Is)" "$*" >>"$LOG"; }

# Tell the room. Never includes diff content or anything token-shaped: a report
# about a leak must not itself be a leak, and the room is a shared record.
notify() {
    local msg="$1"
    log "NOTIFY: $msg"
    if [ "${DRY_RUN:-0}" = 1 ]; then
        log "dry run: not sending to the room"
        return
    fi
    [ -n "$BAND" ] || { log "cannot notify: band CLI not found"; return; }
    "$BAND" room send "$ROOM" --mention "$KEEPER" "$msg" >>"$LOG" 2>&1 \
        || log "notify failed (daemon down?)"
}

fail() {
    notify "**git-sync failed.** $1

Command: \`$2\`
Output:
\`\`\`
${3:0:1500}
\`\`\`

@Git Keeper — diagnose the class (credential / rejected / diverged / guard) and report; do not force-push \`$BRANCH\`."
    exit 1
}

cd "$REPO" || { log "repo missing at $REPO"; exit 1; }
log "--- run start (branch $BRANCH) ---"

# A half-finished merge or rebase is a human mid-decision. Committing through it
# would make their conflict resolution part of an automated snapshot.
if [ -d .git/rebase-merge ] || [ -d .git/rebase-apply ] || [ -f .git/MERGE_HEAD ]; then
    fail "A merge or rebase is in progress in the working tree; refusing to commit through it." \
         "git status" "$(git status 2>&1 | head -20)"
fi

if [ "${DRY_RUN:-0}" != 1 ] && [ ! -s "$TOKEN_FILE" ]; then
    fail "No GitHub token at \`$TOKEN_FILE\`, so an unattended push cannot authenticate. This needs the human — a token cannot be minted here." \
         "test -s $TOKEN_FILE" "file missing or empty"
fi

git add -A || fail "git add failed." "git add -A" "$(git add -A 2>&1)"

if git diff --cached --quiet; then
    log "nothing to commit; tree already clean"
    exit 0
fi

# --- secret guard -----------------------------------------------------------
# Patterns are matched against the STAGED diff only, so a secret that is already
# published is not silently tolerated but also does not block forever.
SECRET_HITS="$(
    git diff --cached -U0 -- . \
    | grep -aEo 'ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|gho_[A-Za-z0-9]{20,}|sk-ant-[A-Za-z0-9_-]{20,}|sk-[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{10,}|BEGIN [A-Z ]*PRIVATE KEY|ANTHROPIC_AUTH_TOKEN[[:space:]]*=|AWS_SECRET_ACCESS_KEY[[:space:]]*=' \
    | sed -E 's/^(github_pat_|ghp_|gho_|sk-ant-|sk-|AKIA|xox[baprs]-).*/\1/' \
    | sort -u || true
)"

if [ -n "$SECRET_HITS" ]; then
    FILES="$(git diff --cached --name-only | head -40)"
    git reset -q
    fail "The secret guard caught credential-shaped text in the staged diff, so nothing was committed. Identify the pattern, remove it from the *source* (the file, or the ignore rules), and let the next run pick it up. Do not bypass this by committing anyway." \
         "git diff --cached (staged, then reset)" "pattern families matched (values withheld):
$SECRET_HITS

files that were staged:
$FILES"
fi

# --- commit -----------------------------------------------------------------
CHANGED="$(git diff --cached --name-only)"
N_FILES="$(printf '%s\n' "$CHANGED" | wc -l)"
BY_DIR="$(printf '%s\n' "$CHANGED" | awk -F/ '{print $1}' | sort | uniq -c | sort -rn | head -8 | sed 's/^ *//' | tr '\n' ',' | sed 's/,$//; s/,/, /g')"

MSG="sync: snapshot $N_FILES file(s)

Automatic 2-hourly snapshot by scripts/git-sync.sh at $(date -u '+%Y-%m-%d %H:%M UTC').
Changed areas: $BY_DIR

This is a mechanical checkpoint of the working tree, not a reviewed change set;
the reasoning for any individual edit lives in the room.

Co-Authored-By: Claude Code <noreply@anthropic.com>"

COMMIT_OUT="$(git commit -q -m "$MSG" 2>&1)" || fail "git commit failed." "git commit" "$COMMIT_OUT"
log "committed $(git rev-parse --short HEAD) ($N_FILES files)"

if [ "${DRY_RUN:-0}" = 1 ]; then
    log "dry run: commit made, push skipped"
    exit 0
fi

# --- push, then verify what is actually on the remote ------------------------
PUSH_OUT="$(git push origin "$BRANCH" 2>&1)"
PUSH_RC=$?
if [ $PUSH_RC -ne 0 ]; then
    fail "git push to origin/$BRANCH failed." "git push origin $BRANCH" "$PUSH_OUT"
fi

LOCAL_SHA="$(git rev-parse HEAD)"
REMOTE_SHA="$(git ls-remote origin "refs/heads/$BRANCH" 2>/dev/null | awk '{print $1}')"

if [ "$LOCAL_SHA" = "$REMOTE_SHA" ]; then
    log "pushed and verified: origin/$BRANCH = ${LOCAL_SHA:0:12}"
    exit 0
fi

# A push that reports success but leaves the remote elsewhere is exactly the
# failure a person would never notice. Say so rather than claiming success.
fail "The push reported success but the remote is not at our revision — treat this as a failed push." \
     "git ls-remote origin refs/heads/$BRANCH" "local  HEAD        = ${LOCAL_SHA:0:12}
remote origin/$BRANCH = ${REMOTE_SHA:0:12}"

# cron entry (avoid the top of the hour; every box on earth fires at :00):
#   23 */2 * * * /home/priyanshu/band/pocketful/scripts/git-sync.sh
