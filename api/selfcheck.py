"""End-to-end contract check for the Pocketful API.

This starts the real HTTP server on a real socket over a real ledger database,
drives it with real requests, and asserts the plan §2.2 shapes and the
reject-not-coerce rules. It is the integrator's evidence for T3.

It is deliberately **not** part of ``tests/`` (owned by the test-author). It is
``run_gate.sh`` phase 4 by planner ruling — a gate that never exercises ``api/``
can be green over a broken API. Run it directly:

    python3 -m api.selfcheck      # exit 0 iff every check passed

It binds port 0, waits for readiness rather than sleeping, and counts a
transport failure as a failure: a request lost before it is accepted is money
lost, and nothing downstream of the socket can see it.

The fixture database is a throwaway under TMPDIR, deleted on exit; override the
location with ``POCKETFUL_SELFCHECK_DIR``. §1.3's durable-volume rule binds the
*deployed* database path — the server's ``--db`` — not this scratch file.
"""

from __future__ import annotations

import http.client
import json
import os
import shutil
import signal
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

from ledger import connect

from .app import create_server

SCRATCH_ROOT = os.environ.get("POCKETFUL_SELFCHECK_DIR") or None
READY_TIMEOUT_SECONDS = 10.0

RESULTS: list[tuple[bool, str]] = []

ACCOUNT_KEYS = {"account_id", "owner_id", "currency", "balance_minor", "allow_overdraft"}
# §C2.5: the balance read carries the owner. This constant was defined and never
# referenced — the pin was the literal dict in the balance row, so widening the
# body would have sailed straight past it. It is referenced now.
BALANCE_KEYS = {"account_id", "owner_id", "currency", "balance_minor"}
TRANSFER_KEYS = {"transfer_id", "status", "from_account_id", "to_account_id",
                 "amount_minor", "currency", "created_at"}
# §C2.5: the counterparty's owner rides BESIDE its id, never instead of it.
ACTIVITY_ITEM_KEYS = {"entry_id", "transfer_id", "direction", "amount_minor",
                      "balance_after_minor", "counterparty_account_id",
                      "counterparty_owner_id", "created_at"}


def check(ok: bool, label: str, detail: str = "") -> bool:
    ok = bool(ok)
    RESULTS.append((ok, label))
    suffix = f"  [{detail}]" if detail else ""
    print(f"{'PASS' if ok else 'FAIL'}  {label}{suffix}")
    return ok


def _install_scratch_cleanup(workdir: str) -> None:
    """Remove the fixture database even if this phase is killed.

    ``run_gate.sh`` bounds phase 4 with ``timeout``, which sends SIGTERM: a
    ``finally`` block never runs on that path, so a hung-then-bounded phase
    would leak a scratch database on every run. Raising here unwinds through the
    normal cleanup instead.
    """
    def _handler(signum: int, _frame: object) -> None:
        shutil.rmtree(workdir, ignore_errors=True)
        raise SystemExit(124)

    for name in ("SIGTERM", "SIGINT"):
        number = getattr(signal, name, None)
        if number is not None:
            signal.signal(number, _handler)


def _wait_until_ready(host: str, port: int) -> None:
    """Block until the server accepts a TCP connection, or fail loudly.

    Readiness is *probed*, never slept for: a fixed sleep is either slower than
    it needs to be or wrong on a loaded host. Bounded by
    ``READY_TIMEOUT_SECONDS`` so a server that never comes up fails the phase
    instead of hanging it.
    """
    deadline = time.monotonic() + READY_TIMEOUT_SECONDS
    last_error: OSError | None = None
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return
        except OSError as exc:
            last_error = exc
            time.sleep(0.02)
    raise RuntimeError(
        f"server on {host}:{port} was not ready within {READY_TIMEOUT_SECONDS}s "
        f"(last error: {last_error})")


class Client:
    """A fresh connection per call; the server closes each response."""

    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port

    def call(self, method: str, path: str, body: object = None,
             headers: dict[str, str] | None = None,
             raw_body: bytes | None = None) -> tuple[int, object]:
        payload = raw_body
        send_headers = dict(headers or {})
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            send_headers.setdefault("Content-Type", "application/json")
        conn = http.client.HTTPConnection(self.host, self.port, timeout=15)
        try:
            conn.request(method, path, body=payload, headers=send_headers)
            response = conn.getresponse()
            data = response.read()
            status = response.status
        finally:
            conn.close()
        try:
            parsed: object = json.loads(data) if data else None
        except json.JSONDecodeError:
            parsed = None
        return status, parsed


def is_minor(value: object) -> bool:
    """A wire money value is a real ``int``, never a ``bool``.

    ``isinstance(True, int)`` is ``True`` because ``bool`` subclasses ``int``, so
    a boolean where an amount belongs slips straight past an ``isinstance``
    shape check — and ``{"balance_minor": False} == {"balance_minor": 0}`` is
    also ``True``. §2.2 requires ``type(v) is int``; every money assertion below
    uses this, so a serializer emitting ``true`` for an amount fails here rather
    than passing.
    """
    return type(value) is int


def _counts(db_path: str) -> tuple[int, int, int]:
    """``(transfers, idempotency_keys, ledger_entries)`` read off the real file.

    Counting is done as a DELTA around a block of requests, never as an absolute:
    every granted USD account adds a transfer and two entries of its own (§C2.3),
    so an absolute count would pin the grant policy rather than the claim the row
    is making ("this refusal wrote nothing").
    """
    conn = connect(db_path)
    try:
        return (
            conn.execute("SELECT COUNT(*) FROM transfers").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM idempotency_keys").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM ledger_entries").fetchone()[0],
        )
    finally:
        conn.close()


def post_transfer(client: Client, body: dict, key: str | None) -> tuple[int, object]:
    headers = {"Idempotency-Key": key} if key is not None else {}
    return client.call("POST", "/transfers", body, headers=headers)


def run(client: Client, db_path: str) -> None:
    # ---- accounts ----------------------------------------------------------
    status, body = client.call("POST", "/accounts",
                               {"owner_id": "alice", "currency": "USD",
                                "account_id": "acct-alice"})
    check(status == 201 and isinstance(body, dict) and set(body) == ACCOUNT_KEYS,
          "POST /accounts -> 201 with exactly the §2.2 body", f"{status} {body}")
    check(isinstance(body, dict) and is_minor(body.get("balance_minor"))
          and body["balance_minor"] == 10_000
          and body.get("account_id") == "acct-alice"
          and body.get("currency") == "USD" and body.get("allow_overdraft") is False,
          "new USD account opens WITH the §C2.3 opening grant: balance_minor 10000",
          f"{body}")

    # §C2.2 + §C2.4: the account_id IS the natural idempotency key, and a repeat
    # is REJECTED rather than replayed. The clause that carries the money meaning
    # is the second one — the refusal must not have posted a SECOND grant.
    status, body = client.call("POST", "/accounts",
                               {"owner_id": "alice", "currency": "USD",
                                "account_id": "acct-alice"})
    check(status == 409 and body == {"error": "account_exists"},
          "re-open an existing id -> 409 account_exists (not idempotent)",
          f"{status} {body}")
    status, alice_after_dup = client.call("GET", "/accounts/acct-alice/balance")
    check(isinstance(alice_after_dup, dict)
          and alice_after_dup.get("balance_minor") == 10_000,
          "the duplicate create posted NO second grant (alice still 10000)",
          f"{alice_after_dup}")

    # §C2.1: the id is REQUIRED and client-supplied — the server never mints one.
    # This is the whole of the free-money hole: with a minted id, a retry after an
    # unknown outcome opens a SECOND account carrying a SECOND $100.
    for extra, label in (
        ({}, "absent"),
        ({"account_id": None}, "null"),
        ({"account_id": ""}, "empty"),
        ({"account_id": "   "}, "whitespace-only"),
    ):
        status, body = client.call("POST", "/accounts",
                                   {"owner_id": "nobody", "currency": "USD", **extra})
        check(status == 400 and body == {"error": "invalid_request"},
              f"account_id {label} -> 400 invalid_request, nothing created "
              f"(the server never mints one)", f"{status} {body}")

    # §C1.7: the reserved id names the system account and can never become a
    # caller's. A 4xx — a 500 here would report a policy refusal as a crash and
    # let a client tell the two apart.
    status, body = client.call("POST", "/accounts",
                               {"owner_id": "attacker", "currency": "USD",
                                "account_id": "__system__"})
    check(status == 409 and body == {"error": "reserved_account_id"},
          "account_id '__system__' -> 409 reserved_account_id (never 2xx, never 500)",
          f"{status} {body}")

    status, body = client.call("POST", "/accounts", raw_body=b"{not json",
                               headers={"Content-Type": "application/json"})
    check(status == 400 and body == {"error": "malformed_json"},
          "malformed JSON body -> 400 malformed_json", f"{status} {body}")

    status, body = client.call("POST", "/accounts",
                               {"owner_id": "x", "currency": "usd",
                                "account_id": "acct-lower"})
    check(status == 400 and body == {"error": "invalid_request"},
          "lowercase currency -> 400 invalid_request (not coerced)", f"{status} {body}")

    status, body = client.call("POST", "/accounts",
                               {"owner_id": 7, "currency": "USD",
                                "account_id": "acct-bad-owner"})
    check(status == 400 and body == {"error": "invalid_request"},
          "non-string owner_id -> 400 invalid_request", f"{status} {body}")

    for account_id, currency, overdraft in (
        ("acct-mint", "USD", True), ("acct-bob", "USD", False), ("acct-eur", "EUR", False),
    ):
        status, body = client.call("POST", "/accounts",
                                   {"owner_id": account_id, "currency": currency,
                                    "account_id": account_id, "allow_overdraft": overdraft})
        check(status == 201, f"create {account_id} ({currency})", f"{status} {body}")

    # The grant is USD-only, because the system account it posts against is
    # single-currency (§C1.2, INVARIANTS §1). A non-USD account still OPENS —
    # what is restricted is the grant, not the generality of creation.
    status, eur = client.call("GET", "/accounts/acct-eur/balance")
    check(isinstance(eur, dict) and is_minor(eur.get("balance_minor"))
          and eur.get("balance_minor") == 0 and eur.get("currency") == "EUR",
          "a non-USD account still opens, ungranted, at 0 (creation is not refused)",
          f"{eur}")

    status, body = client.call("GET", "/accounts/acct-alice/balance")
    check(status == 200 and isinstance(body, dict) and set(body) == BALANCE_KEYS
          and body == {"account_id": "acct-alice", "owner_id": "alice",
                       "currency": "USD", "balance_minor": 10_000}
          and is_minor(body["balance_minor"]),
          "GET balance -> 200 with exactly the §C2.5 body (owner_id on the read path)",
          f"{status} {body}")

    status, body = client.call("GET", "/accounts/ghost/balance")
    check(status == 404 and body == {"error": "unknown_account"},
          "GET balance of an unknown account -> 404 unknown_account", f"{status} {body}")

    # Fund alice from an overdraft-enabled account so the validation rejections
    # below are never rejected for lack of funds — and so the payer actually
    # crosses zero. The grant gives acct-mint 10000; sending 11000 leaves it at
    # -1000, which is what puts a NEGATIVE balance_after on the wire below.
    status, body = client.call("POST", "/transfers",
                               {"from_account_id": "acct-mint", "to_account_id": "acct-alice",
                                "amount_minor": 11_000, "currency": "USD"},
                               headers={"Idempotency-Key": "K-fund"})
    check(status == 201 and isinstance(body, dict) and body.get("status") == "applied",
          "funding transfer (overdraft payer) -> 201 applied", f"{status} {body}")
    check(isinstance(body, dict) and set(body) == TRANSFER_KEYS,
          "transfer response has exactly the §2.2 body", f"{body}")

    # ---- transfer validation: reject, never coerce -------------------------
    base = {"from_account_id": "acct-alice", "to_account_id": "acct-bob",
            "amount_minor": 500, "currency": "USD"}

    status, body = post_transfer(client, base, None)
    check(status == 400 and body == {"error": "invalid_idempotency_key"},
          "missing Idempotency-Key header -> 400 invalid_idempotency_key", f"{status} {body}")

    status, body = post_transfer(client, base, "k" * 256)
    check(status == 400 and body == {"error": "invalid_idempotency_key"},
          "Idempotency-Key longer than 255 chars -> 400 invalid_idempotency_key",
          f"{status} {body}")

    status, body = post_transfer(client, base, "")
    check(status == 400 and body == {"error": "invalid_idempotency_key"},
          "blank Idempotency-Key -> 400 invalid_idempotency_key", f"{status} {body}")

    for bad_value, label in (
        (12.34, "12.34 (JSON float, sent as 12.34)"),
        ("500", '"500" (JSON string)'),
        (True, "true (JSON boolean)"),
        (0, "0 (zero)"),
        (-500, "-500 (negative)"),
    ):
        body_in = dict(base)
        body_in["amount_minor"] = bad_value
        status, body = post_transfer(client, body_in, f"bad-{label}")
        check(status == 422 and body == {"error": "invalid_amount"},
              f"amount_minor {label} -> 422 invalid_amount", f"{status} {body}")

    # A rejected body must never have reached the ledger: alice is untouched.
    # 21000 = the 10000 opening grant + the 11000 funding transfer.
    status, body = client.call("GET", "/accounts/acct-alice/balance")
    check(isinstance(body, dict) and is_minor(body.get("balance_minor"))
          and body.get("balance_minor") == 21_000,
          "no rejected body reached the ledger (alice still 21000)", f"{body}")

    # Snapshot before the one transfer that is meant to land, so the database
    # block below can assert a DELTA rather than an absolute count.
    before_counts = _counts(db_path)

    # ---- idempotency -------------------------------------------------------
    good = dict(base)
    status, first = post_transfer(client, good, "K1")
    check(status == 201 and isinstance(first, dict) and first.get("status") == "applied",
          "valid transfer -> 201 applied", f"{status} {first}")
    transfer_id = first.get("transfer_id") if isinstance(first, dict) else None

    status, replay = post_transfer(client, good, "K1")
    check(status == 200 and isinstance(replay, dict)
          and replay.get("status") == "replayed"
          and replay.get("transfer_id") == transfer_id,
          "same key + identical body -> 200 replayed, same transfer_id",
          f"{status} {replay}")

    different = dict(base)
    different["amount_minor"] = 501
    status, conflict = post_transfer(client, different, "K1")
    check(status == 409 and conflict == {"error": "idempotency_conflict"},
          "same key + different body -> 409 idempotency_conflict (never 200)",
          f"{status} {conflict}")

    status, alice = client.call("GET", "/accounts/acct-alice/balance")
    status2, bob = client.call("GET", "/accounts/acct-bob/balance")
    check(isinstance(alice, dict) and is_minor(alice.get("balance_minor"))
          and alice.get("balance_minor") == 20_500
          and isinstance(bob, dict) and is_minor(bob.get("balance_minor"))
          and bob.get("balance_minor") == 10_500,
          "the conflicting body was not applied (alice 10000+11000-500=20500, "
          "bob 10000+500=10500)",
          f"{alice} {bob}")

    # ---- honest failures ---------------------------------------------------
    status, body = post_transfer(client,
                                 {"from_account_id": "acct-alice", "to_account_id": "acct-bob",
                                  "amount_minor": 1_000_000, "currency": "USD"}, "K2")
    check(status != 200 and status == 422 and body == {"error": "insufficient_funds"},
          "insufficient funds -> 422, never 200", f"{status} {body}")

    status, body = post_transfer(client,
                                 {"from_account_id": "acct-alice", "to_account_id": "acct-alice",
                                  "amount_minor": 10, "currency": "USD"}, "K3")
    check(status == 422 and body == {"error": "same_account_transfer"},
          "payer == payee -> 422 same_account_transfer", f"{status} {body}")

    status, body = post_transfer(client,
                                 {"from_account_id": "acct-alice", "to_account_id": "acct-eur",
                                  "amount_minor": 10, "currency": "USD"}, "K4")
    check(status == 422 and body == {"error": "currency_mismatch"},
          "transfer currency disagrees with an account -> 422 currency_mismatch",
          f"{status} {body}")

    status, body = post_transfer(client,
                                 {"from_account_id": "acct-alice", "to_account_id": "ghost",
                                  "amount_minor": 10, "currency": "USD"}, "K5")
    check(status == 404 and body == {"error": "unknown_account"},
          "unknown payee -> 404 unknown_account", f"{status} {body}")

    # §C1.8: the system account carries allow_overdraft so grants can debit it
    # without limit. A client-facing transfer touching it would therefore be a
    # mint of any amount, so BOTH ends are refused. The status matters as much as
    # the refusal: a 500 would report a policy refusal as a crash and let a caller
    # tell the two apart, while the grant itself would be decorative.
    for money_in, money_out in (("__system__", "acct-bob"), ("acct-alice", "__system__")):
        status, body = post_transfer(
            client,
            {"from_account_id": money_in, "to_account_id": money_out,
             "amount_minor": 10, "currency": "USD"},
            f"K-sys-{money_in}")
        check(status == 422 and body == {"error": "system_account_transfer"},
              f"transfer with __system__ as the {'payer' if money_in == '__system__' else 'payee'}"
              f" -> 422 system_account_transfer (never 2xx, never 500)",
              f"{status} {body}")

    # ---- activity: the uniform non-negative wire rule ----------------------
    status, body = client.call("GET", "/accounts/acct-alice/activity")
    items = body.get("items") if isinstance(body, dict) else None
    check(status == 200 and isinstance(items, list) and len(items) == 3
          and body.get("next_cursor") is None,
          "GET activity -> 200, newest first, null cursor on a partial page "
          "(grant credit + funding credit + the one debit)",
          f"{status} {body}")
    check(bool(items) and all(isinstance(item, dict) and set(item) == ACTIVITY_ITEM_KEYS
                              for item in items),
          "every activity item has exactly the §C2.5 shape", f"{items}")
    check(bool(items) and all(is_minor(item["amount_minor"])
                              and item["amount_minor"] >= 0 for item in items),
          "every activity amount_minor is a NON-NEGATIVE integer", f"{items}")
    check(bool(items) and all(item["direction"] in ("debit", "credit") for item in items),
          "direction is explicitly debit or credit", f"{items}")

    # §C2.5 evidence: the owner is ON the wire, so the page prints the name it is
    # handed instead of holding an id→name map of its own.
    check(bool(items) and all(isinstance(item.get("counterparty_owner_id"), str)
                              and item["counterparty_owner_id"].strip()
                              for item in items),
          "every activity item names its counterparty's OWNER (§C2.5 read path)",
          f"{items}")

    # The grant shows up as ONE credit from the named system account — a real
    # balanced double entry, not value appearing from nowhere (§C1.3).
    grants = [item for item in items or []
              if item.get("counterparty_account_id") == "__system__"]
    check(len(grants) == 1 and grants[0]["direction"] == "credit"
          and grants[0]["amount_minor"] == 10_000,
          "the opening grant is one credit from the named system account",
          f"{grants}")

    debits = [item for item in items or [] if item.get("direction") == "debit"]
    check(len(debits) == 1 and debits[0]["amount_minor"] == 500
          and debits[0]["counterparty_account_id"] == "acct-bob"
          and debits[0]["counterparty_owner_id"] == "acct-bob"
          and debits[0]["balance_after_minor"] == 20_500,
          "alice's debit shows +500 under direction=debit (sign never on the wire)",
          f"{debits}")

    status, bob_activity = client.call("GET", "/accounts/acct-bob/activity")
    bob_items = bob_activity.get("items") if isinstance(bob_activity, dict) else []
    from_alice = [item for item in bob_items or []
                  if item.get("counterparty_account_id") == "acct-alice"]
    check(status == 200 and len(from_alice) == 1 and from_alice[0]["amount_minor"] == 500
          and from_alice[0]["direction"] == "credit"
          and from_alice[0]["counterparty_owner_id"] == "alice",
          "bob's credit from alice: +500 under direction=credit, counterparty owner named",
          f"{from_alice}")

    status, mint_activity = client.call("GET", "/accounts/acct-mint/activity")
    mint_items = mint_activity.get("items") if isinstance(mint_activity, dict) else []
    mint_debits = [item for item in mint_items or [] if item.get("direction") == "debit"]
    check(status == 200 and len(mint_debits) == 1
          and is_minor(mint_debits[0]["amount_minor"])
          and mint_debits[0]["amount_minor"] == 11_000
          and is_minor(mint_debits[0]["balance_after_minor"])
          and mint_debits[0]["balance_after_minor"] == -1_000,
          "an overdraft payer's debit is +11000 amount_minor with a NEGATIVE "
          "balance_after (10000 grant - 11000) — the sign never leaves the wire",
          f"{mint_debits}")

    status, page = client.call("GET", "/accounts/acct-alice/activity?limit=1")
    page_items = page.get("items") if isinstance(page, dict) else []
    cursor = page.get("next_cursor") if isinstance(page, dict) else None
    check(status == 200 and len(page_items) == 1 and isinstance(cursor, str),
          "limit=1 -> one item plus a cursor", f"{status} {page}")

    status, older = client.call("GET", f"/accounts/acct-alice/activity?limit=1&before={cursor}")
    older_items = older.get("items") if isinstance(older, dict) else []
    check(status == 200 and len(older_items) == 1
          and older_items[0]["entry_id"] != page_items[0]["entry_id"]
          and int(older_items[0]["entry_id"]) < int(page_items[0]["entry_id"]),
          "the cursor pages to strictly older entries", f"{older}")

    status, body = client.call("GET", "/accounts/acct-alice/activity?limit=0")
    check(status == 400 and body == {"error": "invalid_limit"},
          "limit=0 -> 400 invalid_limit (not a silent empty page)", f"{status} {body}")

    status, body = client.call("GET", "/accounts/acct-alice/activity?before=abc")
    check(status == 400 and body == {"error": "invalid_cursor"},
          "before=abc -> 400 invalid_cursor", f"{status} {body}")

    status, body = client.call("GET", "/accounts/ghost/activity")
    check(status == 404 and body == {"error": "unknown_account"},
          "activity of an unknown account -> 404 unknown_account", f"{status} {body}")

    # ---- routing -----------------------------------------------------------
    status, body = client.call("GET", "/transfers")
    check(status == 405 and body == {"error": "method_not_allowed"},
          "GET on a POST route -> 405 method_not_allowed", f"{status} {body}")

    status, body = client.call("GET", "/nope")
    check(status == 404 and body == {"error": "not_found"},
          "unknown route -> 404 not_found", f"{status} {body}")

    # ---- database truth: the failed and conflicting attempts wrote nothing --
    #
    # A DELTA around the block, not an absolute count: every granted USD account
    # adds a transfer and two entries of its own (§C2.3), so an absolute number
    # would pin the grant policy rather than the claim this row is making. The
    # claim is that exactly ONE transfer and ONE key landed — K1 — and that the
    # replay, the conflict and the refusals above added nothing on top of the
    # snapshot taken before K1.
    transfers, keys, entries = _counts(db_path)
    check(transfers == before_counts[0] + 1,
          "exactly one transfer landed across the idempotency and refusal blocks "
          "(replay, conflict and refusals wrote none)",
          f"transfers {before_counts[0]} -> {transfers}")
    check(keys == before_counts[1] + 1,
          "exactly one idempotency row landed (refusals and conflicts wrote none)",
          f"idempotency_keys {before_counts[1]} -> {keys}")
    check(entries == before_counts[2] + 2,
          "double entry holds: the one landed transfer wrote exactly two entries",
          f"ledger_entries {before_counts[2]} -> {entries}")

    # ---- a refused transfer must not burn its key ---------------------------
    #
    # The ledger proves this at its own boundary
    # (tests/test_idempotent_retry.py::test_a_refused_transfer_does_not_burn_its_key);
    # nothing on the wire combined "refused" with "retry the same key", which is
    # where the money consequence actually lands. Both failure directions are
    # invisible to I1/I2 because neither writes an entry: if a refusal CLAIMS the
    # key, the caller's corrected retry compares fingerprints against a stored
    # refusal and that transfer can never be completed with that key; if a refusal
    # STORES A SUCCESS, the retry replays "applied" for money that never moved and
    # the caller is told it succeeded.
    #
    # Fresh accounts, and placed after the database-truth block above so the
    # counts proving the earlier scenario wrote nothing are not disturbed.
    for rk_account in ("acct-rk-payer", "acct-rk-payee"):
        status, body = client.call("POST", "/accounts",
                                   {"owner_id": rk_account, "currency": "USD",
                                    "account_id": rk_account})
        check(status == 201, f"create {rk_account} for the refused-retry check",
              f"{status} {body}")

    status, body = client.call("POST", "/transfers",
                               {"from_account_id": "acct-mint",
                                "to_account_id": "acct-rk-payer",
                                "amount_minor": 300, "currency": "USD"},
                               headers={"Idempotency-Key": "K-rk-fund"})
    check(status == 201, "fund the refused-retry payer with 300", f"{status} {body}")

    # The refused body must be one the payer genuinely cannot afford. Every USD
    # account now opens with a 10000 grant (§C2.3), so "unaffordable" is 11000
    # against the payer's 10000 + 300 funding — not the 1000 that was
    # unaffordable when a new account still started at 0.
    unaffordable = {"from_account_id": "acct-rk-payer", "to_account_id": "acct-rk-payee",
                    "amount_minor": 11_000, "currency": "USD"}
    affordable = dict(unaffordable, amount_minor=250)

    status, body = post_transfer(client, unaffordable, "K-rk")
    check(status == 422 and body == {"error": "insufficient_funds"},
          "refused-retry: unaffordable body under K-rk -> 422 insufficient_funds",
          f"{status} {body}")

    status, retried = post_transfer(client, affordable, "K-rk")
    retried_id = retried.get("transfer_id") if isinstance(retried, dict) else None
    # What each clause is actually for, since the label must not overclaim:
    # `status == "applied"` (with the 201) is the clause that rules out a
    # replayed refusal — a replay would answer 200/"replayed". `retried_id !=
    # transfer_id` cannot catch that case at all (a replayed refusal returns the
    # refusal's id, which differs from K1's too); it is a no-aliasing check, that
    # the id is one this call produced rather than borrowed from another key.
    # The refusal-wrote-nothing claim is carried by the balances below and by the
    # provenance check on the K-rk key row.
    check(status == 201 and isinstance(retried, dict) and retried.get("status") == "applied"
          and isinstance(retried_id, str) and retried_id != transfer_id,
          "K-rk retried with a CORRECTED affordable body -> 201 applied with a FRESH "
          "transfer_id (status 'applied' is what rules out a replayed refusal; "
          "retried_id != K1 is a no-aliasing check, not a replay detector)",
          f"{status} {retried} vs K1 {transfer_id}")

    # Controls, both directions, so this cannot be satisfied by an implementation
    # that simply never records keys: the identical body must replay rather than
    # apply a second time, and the original refused body must conflict against the
    # recorded apply rather than being refused a second time.
    status, rk_replay = post_transfer(client, affordable, "K-rk")
    check(status == 200 and isinstance(rk_replay, dict)
          and rk_replay.get("status") == "replayed"
          and rk_replay.get("transfer_id") == retried_id,
          "control: K-rk + the identical corrected body -> 200 replayed, same transfer_id",
          f"{status} {rk_replay}")

    status, rk_conflict = post_transfer(client, unaffordable, "K-rk")
    check(status == 409 and rk_conflict == {"error": "idempotency_conflict"},
          "control: K-rk + the original unchanged refused body -> 409 conflict "
          "(a ledger that never recorded keys would refuse it again instead)",
          f"{status} {rk_conflict}")

    # The contrast that gives that control its meaning: the SAME unaffordable
    # body under a key with no recorded history is refused, not conflicted. So
    # the 409 above is caused by K-rk being recorded, not by the body itself —
    # which is exactly what distinguishes recording from never-recording.
    status, fresh = post_transfer(client, unaffordable, "K-rk-never-used")
    check(status == 422 and fresh == {"error": "insufficient_funds"},
          "contrast: the same unaffordable body under a never-recorded key -> 422 "
          "refusal, not 409 (so the conflict above is about recording, not the body)",
          f"{status} {fresh}")

    status, rk_payer = client.call("GET", "/accounts/acct-rk-payer/balance")
    status2, rk_payee = client.call("GET", "/accounts/acct-rk-payee/balance")
    check(isinstance(rk_payer, dict) and is_minor(rk_payer.get("balance_minor"))
          and rk_payer.get("balance_minor") == 10_050
          and isinstance(rk_payee, dict) and is_minor(rk_payee.get("balance_minor"))
          and rk_payee.get("balance_minor") == 10_250,
          "exactly one application under K-rk: payer 10000+300-250=10050, "
          "payee 10000+250=10250, the refusal and the conflict moved nothing",
          f"{rk_payer} {rk_payee}")

    conn = connect(db_path)
    try:
        rk_key_rows = conn.execute("SELECT transfer_id FROM idempotency_keys WHERE key = ?",
                                   ("K-rk",)).fetchall()
        rk_rows = conn.execute(
            "SELECT COUNT(*) FROM transfers WHERE from_account_id = ? AND to_account_id = ?",
            ("acct-rk-payer", "acct-rk-payee")).fetchone()[0]
    finally:
        conn.close()
    # Provenance, not cardinality. A COUNT of 1 is satisfied by a mutant that
    # writes its single K-rk row on the REFUSAL, borrowing a pre-existing
    # transfer_id (idempotency_keys.transfer_id is NOT NULL REFERENCES transfers)
    # — the count coincides at 1 while the claim that the refusal wrote no row is
    # false, so the row would be green and its own label a lie. The assertion has
    # to observe WHICH transfer the surviving row points at: the applied one.
    rk_transfer_ids = [row[0] for row in rk_key_rows]
    check(rk_transfer_ids == [retried_id],
          "the surviving K-rk key row points at the APPLIED transfer "
          "(a refusal that wrote a borrowed row flips this)",
          f"K-rk transfer_ids={rk_transfer_ids} applied={retried_id}")
    check(rk_rows == 1,
          "exactly one transfer moved money under K-rk (the refusal and the conflict wrote none)",
          f"transfers={rk_rows}")


def run_concurrency(client: Client) -> None:
    """The threading seam: concurrent requests, one ledger connection per thread.

    The payer can afford exactly ten 10-minor transfers. Thirty fired at once
    through the HTTP surface must apply exactly ten and refuse twenty with
    ``insufficient_funds`` — never a 5xx, never SQLITE_BUSY leaking out as a
    server error, and never an overdraft. This is what proves api/'s thread-local
    connection pool neither serializes into failures nor shares one connection
    across threads, and that the balance read that feeds the write stays inside
    the ledger's transaction.

    The payer's balance is reached by DRAINING it, not by funding it: every USD
    account now opens with a 10000 opening grant (§C2.3), so the scenario funds
    itself and then spends down to the exact 100 the storm needs.
    """
    workers = 30
    client.call("POST", "/accounts", {"owner_id": "storm", "currency": "USD",
                                     "account_id": "acct-storm", "allow_overdraft": True})
    client.call("POST", "/accounts", {"owner_id": "payer", "currency": "USD",
                                      "account_id": "acct-payer"})
    for index in range(10):
        client.call("POST", "/accounts", {"owner_id": f"q{index}", "currency": "USD",
                                          "account_id": f"q{index}"})
    # 10000 (the opening grant) - 9900 (drained to the storm account) = 100, which
    # is exactly ten 10-minor transfers.
    client.call("POST", "/transfers",
                {"from_account_id": "acct-payer", "to_account_id": "acct-storm",
                 "amount_minor": 9_900, "currency": "USD"},
                headers={"Idempotency-Key": "storm-drain"})

    statuses: list[tuple[int, str]] = []
    lock = threading.Lock()
    barrier = threading.Barrier(workers)

    def fire(index: int) -> None:
        barrier.wait()
        try:
            status, body = client.call(
                "POST", "/transfers",
                {"from_account_id": "acct-payer", "to_account_id": f"q{index % 10}",
                 "amount_minor": 10, "currency": "USD"},
                headers={"Idempotency-Key": f"storm-{index}"})
        except Exception as exc:  # a dropped connection is a finding, not a silent hole
            with lock:
                statuses.append((0, f"transport_error:{type(exc).__name__}"))
            return
        error = body.get("error") if isinstance(body, dict) and "error" in body else "applied"
        with lock:
            statuses.append((status, error))

    threads = [threading.Thread(target=fire, args=(index,)) for index in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    with lock:
        observed = list(statuses)

    applied = sum(1 for status, _ in observed if status == 201)
    refused = sum(1 for status, error in observed
                  if status == 422 and error == "insufficient_funds")
    check(applied == 10 and refused == 20 and len(observed) == workers,
          "30 concurrent transfers on a payer affording exactly 10: 10 applied, 20 refused",
          f"applied={applied} refused={refused} other={len(observed) - applied - refused}")
    check(all(200 <= status < 500 for status, _ in observed),
          "every concurrent request got an HTTP answer (no transport reset, no 5xx)",
          f"{sorted(observed)}")

    status, balance = client.call("GET", "/accounts/acct-payer/balance")
    check(status == 200 and isinstance(balance, dict)
          and is_minor(balance.get("balance_minor"))
          and balance.get("balance_minor") == 0,
          "the payer lands on exactly 0 under concurrency — never overdrawn", f"{balance}")

    status, activity = client.call("GET", "/accounts/acct-payer/activity?limit=200")
    items = activity.get("items") if isinstance(activity, dict) else []
    check(status == 200 and len(items) == 12
          and sum(1 for item in items if item["direction"] == "debit") == 11
          and all(item["amount_minor"] >= 0 for item in items),
          "the storm wrote 10 non-negative debits, atop the payer's grant credit "
          "and the one drain debit",
          f"items={len(items)}")


def main() -> int:
    workdir = tempfile.mkdtemp(prefix="pocketful-selfcheck-", dir=SCRATCH_ROOT)
    _install_scratch_cleanup(workdir)
    db_path = str(Path(workdir) / "selfcheck.db")
    server = create_server(db_path, host="127.0.0.1", port=0)
    port = server.server_address[1]
    print(f"Pocketful API self-check — http://127.0.0.1:{port} over {db_path}")
    print("-" * 72)

    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05},
                              daemon=True)
    thread.start()
    try:
        _wait_until_ready("127.0.0.1", port)
        client = Client("127.0.0.1", port)
        run(client, db_path)
        run_concurrency(client)
    except RuntimeError as exc:
        check(False, "the server bound and answered", str(exc))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        shutil.rmtree(workdir, ignore_errors=True)

    passed = sum(1 for ok, _ in RESULTS if ok)
    failures = [label for ok, label in RESULTS if not ok]
    print("-" * 72)
    print(f"{passed}/{len(RESULTS)} checks passed")
    for label in failures:
        print(f"  FAILED: {label}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
