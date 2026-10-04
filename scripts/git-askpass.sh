#!/usr/bin/env bash
# Credential bridge for unattended git pushes.
#
# git invokes this via GIT_ASKPASS and passes the prompt it wants answered as $1.
# We answer the username from a constant and the password with the token, which is
# read from a mode-600 file OUTSIDE the repository.
#
# Why it is built this way: the token must never reach .git/config, the remote
# URL, or any process argv. A credential helper or a `https://user:token@host`
# remote would put it in both, where it survives in the repo and in `ps`. Here it
# is read from a file at the moment git asks and never written anywhere.
#
# It deliberately does NOT fall back to an interactive prompt: a cron job has no
# TTY, and a silent empty password would surface later as a confusing 403.

set -euo pipefail

TOKEN_FILE="${POCKETFUL_GITHUB_TOKEN_FILE:-$HOME/.config/pocketful/github-token}"

case "${1:-}" in
    Username*)
        printf 'x-access-token'
        ;;
    *)
        # Source 1: an authenticated `gh` on this machine. Preferred when
        # present, because a gh login is self-proving — it was created by a
        # browser consent that succeeded, not by pasting a token whose
        # permissions nobody verified. `gh auth login --web` is also the
        # one-command route to a working credential.
        if command -v gh >/dev/null 2>&1; then
            if GH_TOK="$(gh auth token 2>/dev/null)" && [ -n "$GH_TOK" ]; then
                printf '%s' "$GH_TOK"
                exit 0
            fi
        fi

        # Source 2: the fine-grained PAT file.
        if [ -s "$TOKEN_FILE" ]; then
            # Strip any trailing newline; a token with a stray \n authenticates
            # as nothing and reports as a 403, which reads like a permissions
            # problem and sends you looking in the wrong place.
            tr -d '\r\n' < "$TOKEN_FILE"
            exit 0
        fi

        echo "git-askpass: no credential. Either run 'gh auth login --web --git-protocol https' or write a PAT to $TOKEN_FILE (see roles/git-keeper.md)." >&2
        exit 1
        ;;
esac
