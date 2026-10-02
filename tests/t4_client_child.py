"""Child process for the T4 crash-and-resume evidence. NOT a test.

`tests/test_client_retry.py` spawns this module twice per scenario. It is a
separate process on purpose: the obligation is that the client's idempotency key
survives the process dying, and an in-process `raise` would prove nothing about
that. This file is deliberately not named `test_*.py`, so `unittest discover`
leaves it alone.

Why the crash is deterministic. `ApiClient` takes an injectable transport, and
its docstring says why: an evidence driver can "control exactly when the process
dies". So the transport here does the real HTTP round trip — real socket, real
server, real `api/` — and then, with `--crash`, `os.kill`s itself *after* the
server has answered and *before* returning to `_attempt`. The record is therefore
still `pending` on disk when the process dies, which is exactly the state
`app.send.resume` is built to recover from. No sleeps, no races, no timing.

The outgoing `Idempotency-Key` is recorded here, in the transport, because that
is the one place that sees the header as the client actually put it on the wire —
before any server-side parsing. It is appended as one JSON line per request, line
buffered, so it is on disk before the kill.

Usage (all flags required, `--crash`/`--mutate-fresh-key` are bare flags):

    python3 -m tests.t4_client_child --base-url URL --store PATH --out PATH
            --phase {send,resume} --from ID --to ID --amount N --currency CODE
            [--crash] [--mutate-fresh-key]
"""

import json
import os
import signal
import sys
import urllib.error
import urllib.request
from dataclasses import replace

from app.client import ApiClient
from app.keystore import PendingStore
from app.keys import new_idempotency_key
# Not `from app import send`: the package re-exports a `send` *function* that
# shadows the submodule, so the module has to be reached by its full path.
from app.send import resume as resume_transfers, send as send_transfer


class FreshKeyOnRetryStore(PendingStore):
    """MUTANT: the retry path mints a fresh key instead of reusing the stored one.

    This is the defect the whole obligation is about. `PendingStore.outstanding`
    is the list `app.send.resume` iterates, and each returned record carries the
    key that `_attempt` puts on the wire — so returning a *re-minted* key here is
    exactly "the retry minted a new key", with nothing else about the client
    changed. `_require` maps the minted key back so `settle` still finds the
    record; without that the mutant would crash on settle and we would be testing
    the crash instead of the double-spend.

    A client that does this is a double-spend the ledger cannot catch: the retry
    is a different key, so the ledger sees a different transfer and applies it.
    """

    def __init__(self, path):
        super().__init__(path)
        self._minted = {}

    def outstanding(self):
        return [replace(rec, key=self._mint_for(rec.key))
                for rec in super().outstanding()]

    def _mint_for(self, original_key):
        fresh = new_idempotency_key()
        self._minted[fresh] = original_key
        return fresh

    def _require(self, key):
        return super()._require(self._minted.get(key, key))


def _parse(argv):
    opts = {"crash": False, "mutate-fresh-key": False, "currency": "USD"}
    it = iter(argv)
    for arg in it:
        if arg.startswith("--"):
            name = arg[2:]
            if name in ("crash", "mutate-fresh-key"):
                opts[name] = True
            else:
                opts[name] = next(it)
    return opts


def main(argv):
    opts = _parse(argv)
    log = open(opts["out"], "a", encoding="utf-8", buffering=1)
    crash = opts["crash"]

    def transport(method, url, body, headers):
        # The wire fact, recorded before the request and flushed before any kill.
        log.write(json.dumps({"event": "request", "method": method, "url": url,
                              "key": headers.get("Idempotency-Key")}) + "\n")
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                status, raw = response.status, response.read()
        except urllib.error.HTTPError as exc:
            status, raw = exc.code, exc.read()
        if crash:
            # Die with the server's answer already read and the record still
            # pending: the crash window this scenario is about.
            sys.stdout.flush()
            os.kill(os.getpid(), signal.SIGKILL)
        return status, raw

    client = ApiClient(opts["base-url"], transport=transport)
    store = (FreshKeyOnRetryStore(opts["store"]) if opts["mutate-fresh-key"]
             else PendingStore(opts["store"]))

    if opts["phase"] == "send":
        outcome = send_transfer(store, client,
                                from_account_id=opts["from"],
                                to_account_id=opts["to"],
                                amount_minor=int(opts["amount"]),
                                currency=opts["currency"])
        outcomes = [outcome]
    else:
        outcomes = resume_transfers(store, client)

    for outcome in outcomes:
        log.write(json.dumps({"event": "outcome", "status": outcome.status,
                              "transfer_id": outcome.transfer_id,
                              "key": outcome.key}) + "\n")
    log.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
