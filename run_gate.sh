#!/usr/bin/env bash
# Pocketful gate — the four frozen phases, one command, one quotable output.
#
#   1. tests          python3 -m unittest discover -s tests -t .
#   2. byte-compile   python3 -m compileall over the whole tree
#   3. lint           python3 -m api.lint (stdlib-only; see api/lint.py)
#   4. api contract   python3 -m api.selfcheck (real HTTP over a real ledger)
#
# Exits non-zero if ANY phase fails, and every phase runs even when an earlier
# one failed, so a single run reports everything. Each phase is bounded by
# PHASE_TIMEOUT seconds (default 300) so a hang fails the gate instead of
# blocking it.
#
# No sqlite3 CLI is invoked anywhere: neither host has one (plan §1.3). The only
# database the gate writes is phase 4's throwaway fixture under TMPDIR.
#
# Phase 4 binds an ephemeral port (port 0), waits for readiness rather than
# sleeping, and counts a transport failure as a failure — a request lost before
# it is accepted is money lost, and nothing downstream of the socket can see it.
# It is a regression guard over the API boundary; the proof of that boundary is
# the reviewer's T6, not this file.

set -uo pipefail

cd "$(dirname "$0")" || exit 1

PY="${PYTHON:-python3}"
PHASE_TIMEOUT="${PHASE_TIMEOUT:-300}"
STATUS=0

# Bound each phase when coreutils timeout is present; run unbounded if not.
BOUND=""
if command -v timeout >/dev/null 2>&1; then
    BOUND="timeout ${PHASE_TIMEOUT}"
fi

banner() {
    printf '\n============================================================\n'
    printf '== %s\n' "$1"
    printf '============================================================\n'
}

record() {
    local rc="$1" name="$2"
    if [ "$rc" -ne 0 ]; then
        STATUS=1
        printf '\n*** PHASE FAILED: %s (exit %s) ***\n' "$name" "$rc"
        if [ "$rc" -eq 124 ]; then
            printf '*** phase %s exceeded PHASE_TIMEOUT=%ss ***\n' "$name" "$PHASE_TIMEOUT"
        fi
    fi
}

# -- tree pin -----------------------------------------------------------------
#
# One line that binds this run to the revision it actually read, because there
# is no VCS on this host and the tree moves under a running gate. Scope is
# stated, not implied: every file under tests/, ledger/, api/ and app/ — the
# trees this gate operates on. tests/ alone would stay green while the ledger
# moved beneath it (the tests phase imports the ledger); ledger/, api/ and tests/
# alone would stay green while the client moved beneath them (the client is a
# first-class package and the compile phase builds it). Excluded: __pycache__
# only, which is derived from the sources already in the pin (wipe and recompile
# leaves the pin unmoved). find -type f catches a new fixture of any extension
# by construction.
#
# Scope note, so the pin does not overstate itself: app/ is pinned and compiled
# here, and phase 1 runs the suite that carries the client's crash/retry evidence
# (test-author's, under tests/) — so the coverage of the client rests on a
# gate-resident test that imports it, not on this pin alone. The pin binds the
# client's bytes; the suite, not this line, speaks for its behaviour.
#
# Printed BEFORE and AFTER the run so a count quoted from it is a claim about a
# revision instead of a moment. A pin that moves mid-run does NOT fail the gate
# and does NOT change the exit status: a mismatch means a peer typed while the
# gate ran, not that the code is wrong, and a gate that went red for that would
# erode what red means. The printed line does the disqualifying.
#
# Locale: the sort below is forced to C, so the pin is a function of the bytes
# and the path list alone — never of the operator's collation. Without it the
# same tree yields different numbers on different hosts: under en_US.UTF-8
# punctuation is weighted away and `api/app.py` sorts before `api/__init__.py`,
# while byte order puts `__init__.py` first. The pin would then silently stop
# being a cross-host value while still looking like one. C is also the only
# collation that is deterministic for an arbitrary filename, ASCII or not.
#
# Deliberately applied to the sort and NOT exported for the whole run: the pin is
# an identity computation over bytes, whereas the phases are the run itself, and
# this gate's claim is host-scoped (T8 establishes the host). Forcing the whole
# run to the C locale would stop the gate exercising the locale the host actually
# uses, hiding a locale-sensitive defect here instead of surfacing it where the
# deploy would hit it.
TREE_PIN_TREES=(tests ledger api app)

tree_pin() {
    if ! command -v sha256sum >/dev/null 2>&1; then
        printf 'unavailable-no-sha256sum'
        return
    fi
    find "${TREE_PIN_TREES[@]}" -type f -not -path '*/__pycache__/*' \
        | LC_ALL=C sort | xargs sha256sum | sha256sum | cut -d' ' -f1
}

# The gate's own digest, printed once on a separate line and deliberately NOT
# folded into the tree pin. A file cannot carry a stable hash of itself inside
# its own input — folding it in is a fixed point that never converges — and
# keeping the two apart means the line says what it means: the tree pin pins
# what the gate READ, this pins which CHECKER ran. Both are needed, because a
# green run at a known tree revision is unpinned if the script producing it can
# itself have been edited.
#
# Caveat, stated rather than implied: this is an anchor for honest agents, not
# tamper-proofing. A self-reported hash cannot defend against a script that
# lies about it; it only makes an accidental edit visible.
gate_pin() {
    if ! command -v sha256sum >/dev/null 2>&1 || [ ! -r "$0" ]; then
        printf 'unavailable'
        return
    fi
    sha256sum "$0" | cut -d' ' -f1
}

banner "1/4 tests — $PY -m unittest discover -s tests -t . -v"
PIN_BEFORE="$(tree_pin)"
printf 'GATE PIN:          %s  (sha256 of this script; anchor, not tamper-proofing)\n' "$(gate_pin)"
printf 'TREE PIN (before): %s\n' "$PIN_BEFORE"
$BOUND "$PY" -m unittest discover -s tests -t . -v
record $? "tests"

banner "2/4 byte-compile — $PY -m compileall -q -f ledger api tests app"
$BOUND "$PY" -m compileall -q -f ledger api tests app
record $? "byte-compile"

banner "3/4 lint — $PY -m api.lint"
$BOUND "$PY" -m api.lint
record $? "lint"

banner "4/4 api contract — $PY -m api.selfcheck"
$BOUND "$PY" -m api.selfcheck
record $? "api-contract"

# The closing half of the pin. A run is only bound to a revision if both halves
# agree; this is judged here, after the last phase, and never charged to STATUS.
PIN_AFTER="$(tree_pin)"
printf '\nTREE PIN (after):  %s\n' "$PIN_AFTER"
if [ "$PIN_BEFORE" != "$PIN_AFTER" ]; then
    printf '\n*** PIN MOVED DURING RUN: %s -> %s ***\n' "$PIN_BEFORE" "$PIN_AFTER"
    printf '*** the tree changed while this gate ran — do not quote its exit status\n'
    printf '*** as bound to %s; re-run on a quiet tree. ***\n' "$PIN_AFTER"
fi

printf '\n============================================================\n'
if [ "$STATUS" -eq 0 ]; then
    printf 'GATE GREEN — tests, byte-compile, lint and api contract all passed\n'
else
    printf 'GATE RED — at least one phase failed (see the phase output above)\n'
fi
printf '============================================================\n'
exit "$STATUS"
