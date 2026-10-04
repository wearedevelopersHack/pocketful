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
        if [ ! -s "$TOKEN_FILE" ]; then
            echo "git-askpass: no token at $TOKEN_FILE (see roles/git-keeper.md)" >&2
            exit 1
        fi
        # Strip any trailing newline; a token with a stray \n authenticates as
        # nothing and reports as a 403, which reads like a permissions problem.
        tr -d '\r\n' < "$TOKEN_FILE"
        ;;
esac
