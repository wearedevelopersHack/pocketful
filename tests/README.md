# tests/ — the invariant gate

Owned exclusively by the **test-author**. Nobody else edits anything in this
directory, ever. The suite is the gate: a green run from here is the only
accepted proof that a money-path change is safe.

## Running it

```sh
# the full suite
python3 -m unittest discover -s tests -t . -v

# just the harness self-check (proves the gate can fail; needs no ledger)
python3 -m unittest tests.test_harness_selfcheck -v
```

## Layout

| File | What it is |
|---|---|
| `invariants.py` | I1..I6 as reusable functions. Every one reads the database with **SQL**, not the ledger API, so a ledger bug cannot hide from its own check. |
| `harness.py` | Real on-disk SQLite (WAL needs a file), one connection per thread, a barrier-released `run_concurrent`, and the `Backend` abstraction. |
| `scenarios.py` | The five obligations from `INVARIANTS.md` §7, written against a `Backend`. |
| `mutants.py` | A correct `ControlLedger` plus five deliberately broken ledgers. |
| `fixture_schema.py` | Fallback DDL mirroring room plan §3; used only when `ledger/schema.py` exposes no init function. |
| `test_wire_refusal.py` | The **only** file that imports `api/`. Drives the real `api.selfcheck.run` against a real server, with and without a ledger that records refusals; see obligation 9. |
| `test_client_retry.py` | Drives the real `app/` client as a real process against the real `api/` server, SIGKILLs it after the 201, and asserts the retry reuses the key on the wire; see obligation 10. |
| `t4_client_child.py` | The child process obligation 10 spawns. Not a test — deliberately not named `test_*.py`. Holds the transport that records the outgoing header and `os.kill`s itself in the crash window. |
| `test_web_reload.py` | Drives the real `app.web` UI as a real server against the real `api/` and ledger, models the browser's reload, and asserts a reload of the POST's *response* mints no second key; see obligations 11 and 12. |
| `test_persist_ordering.py` | Drives `app.selfcheck.scenario_web_ui` under a store whose write is deferred past the attempt, so the persist-before-attempt row has a permanent falsifier; see obligation 13. |
| `test_web_accounts.py` | Red-first: drives the real `app.web` UI against the real `api/` for account creation and switching, and pins the account-agnostic pending store; also carries C3.7's negative — the create-success document announces no welcome — the taken-*address* refusal that must name the address it collided with (obligation 18), and the opaque-id rows: every id the API accepted must read back as its own account through `app.client`'s two interpolating methods. See obligation 14, obligation 16 and obligation 18. |
| `test_web_design.py` | Certifies `app/design.py`'s two markup constraints — the address whole in the header and truncated only in lists, a name beside the id and never instead of it — with a permanent falsifier per constraint, a hostile fixture through the name fields for the `_esc` mutant, and the cross-component row that the *readable* id survives on the real composition; see obligation 15. |
| `test_kill_and_resume_coverage.py` | Brings `app/selfcheck.scenario_kill_and_resume`'s own verdict under the gate, bounded to that scenario's rows, with a store-loss falsifier that must flip exactly one named row; see obligation 17. |
| `test_*.py` | The tests. |

## The five obligations

1. **Parallel transfer storm** (`test_parallel_storm.py`) — 200 concurrent
   transfers on 200 threads over overlapping account pairs, two storms at once.
   Exact expectations: the hot account (seeded with exactly 50 minor units and
   debited 100 times concurrently) lands on exactly 0, exactly 50 debits are
   accepted, and `sum(RING)` is exactly `400 + 50`. This is the scenario that
   catches a payer balance read outside the write transaction.
2. **Idempotent retry storm** (`test_idempotent_retry.py`) — one key fired 100×
   concurrently. Exactly one application, exactly one `"applied"`, 99
   `"replayed"`, and every caller receives the same `transfer_id`.
3. **Rounding sweep** (`test_rounding.py`) — `split(total, n)` over 600+ pairs
   including prime totals, negative totals, `n=1` and `n > total`. I6 every time.
4. **Failure atomicity** (`test_failure_atomicity.py`) — a failure injected
   mid-transfer by a SQLite trigger that aborts the second half of the double
   entry. No partial entries, no transfer row, no idempotency row survives, and
   the retry still applies exactly once.
5. **Invariant fuzz** (`test_invariant_fuzz.py`) — a randomized operation
   sequence (transfers, replays, conflicts, invalid requests) with I1, I2, I3
   and I4 asserted after **every** operation, over four seeds.
6. **Check-then-act mutation fixture** (`test_harness_selfcheck.py`,
   `scenario_overdraft_race`) — a permanent, always-run mutation probe. Twenty
   barrier-released concurrent debits of a payer that can afford exactly ten.
   The deliberately broken `CheckThenActLedger` reads the payer balance outside
   the write transaction, so every reader sees the same stale balance, applies
   more transfers than the payer can afford, drives src negative, and violates
   I4. Detection is asserted as **over**-application (`applied > affordable`,
   `src < 0`, I4 violated), not as an exact count: the stale-read gap widens the
   race window but does not promise all readers read before the first commit, so
   pinning the count made the fixture flake 1 run in 40. The gate asserts that this
   variant turns the race **red**, and that a correct implementation turns it
   green. It is not skipped when it is inconvenient: it means "the gate is
   green" without anyone having to trust it. Detection is not *guaranteed* — the
   mutant's stale read is unsynchronized, so a run in which enough readers read
   after the first commit shows no over-application and the gate goes red. That
   is the safe direction (a loud red, not a silent pass); it has not been
   observed to miss.

7. **Never-coerce guard** (`test_validation.py`,
   `scenario_amount_validation`) — `12.34`, `1.0`, `"500"`, `True`, `False`,
   `0`, `-500`, `2**53`, `None` and `[1]` must each be rejected with the
   ledger's own `InvalidAmount`, and each rejection must leave the ledger
   untouched. `True`/`False` are there because `bool` is an `int` subclass and
   `isinstance(v, int)` accepts them; `1.0` is there because it slips past an
   `v == int(v)` shape check that catches `12.34`. `assertRaises` alone is a
   weak test: a validator that writes half a double entry and *then* raises
   would pass it and destroy money, so entries, transfers, balances and the
   request's own idempotency key are all compared across the rejection.
   Each of those two assertions has its own mutant: `CoercingLedger` kills the
   "rejected *as* InvalidAmount" half, and `RejectAfterWriteLedger` — which
   applies a balanced transfer and *then* refuses — kills the untouched half
   while leaving I1 and I2 satisfied, so the kill is attributable to that
   assertion and not to a general invariant. `NonAtomicLedger` does not cover
   this shape (it validates before it writes), verified by drive.
   The key assertion has its own mutant too, and it is not the refusal-path one:
   `RefusalBurnsKeyLedger` fires on a funds refusal raised inside the
   transaction, which a bad amount never reaches, so it passes this matrix.
   `BurnsKeyOnBadAmountLedger` records the key for a refused *amount* and is
   killed at "amount 12.34 was rejected but recorded key 'reject-0'".
   `ShapeCheckLedger` — `int(v) == v` instead of a type check — is killed only
   by `1.0`, and `test_one_point_oh_is_the_only_value_that_separates_the_two`
   proves that in both directions: green with `1.0` removed, red with it present
   and naming it. So `1.0`'s place in the matrix is exercised, not asserted.
   `MAX_MINOR` is accepted and `MAX_MINOR + 1` is not.
   This is not one of the §7 obligations — it is the contract guard lifted from
   the T1 proof script, kept because it is the rule with the widest blast radius
   if it ever regresses.

8. **Refusal path** (`test_idempotent_retry.py`, `scenario_refusal_path`) — a
   refused transfer must leave no trace, including its idempotency key. The
   amount matrix only covers refusals raised *before* the transaction, which
   never had a key to lose; an **insufficient-funds** refusal is raised *inside*
   it, after the key has been claimed, and only the rollback gives it back.
   Getting this wrong is the one defect no invariant can see: the money never
   moves, so I1 and I2 hold, and the caller's retry is answered as a success for
   a transfer that never happened. So the assertion is not "no key row exists" —
   that is a shape — it is that a retry with that key still does the work. Two
   legs: (A) a refusal leaves no key, no entries, no transfer row, no balance
   move, and a retry with the same key and a *different* body still applies;
   (B) once a request has applied, same key + same body replays with the same
   `transfer_id` and no balance move, while same key + a different body is
   refused with the balance untouched. Leg B is what stops a ledger satisfying
   leg A by simply never writing keys.

9. **Wire refusal fixture** (`test_wire_refusal.py`) — obligation 8's assertion
   exists twice: once against the `Ledger` surface, and once at the HTTP
   boundary in `api/selfcheck.py`. The second had no mutant, so it was green
   without evidence. This file drives the real `api.selfcheck.run` against a
   real server over a real database, swapping in a ledger that refuses
   correctly and then claims the key anyway, via the pool's per-request
   resolution (`LedgerPool.ledger`) — no edit to `api/`.
   The acceptance criterion is that the **refused-retry row specifically** flips
   PASS -> FAIL, not merely that the run goes red: `run()` is one long scenario,
   so a red says nothing about which assertion fired. Measured against
   `api/selfcheck.py a7d67c83`, a faithful mutant moves **seven** rows of 59 —
   the refused-retry row, the block's two controls, both money rows, the
   provenance row (`the surviving K-rk key row points at the APPLIED transfer`),
   and the global `idempotency_keys` count, which moves because `run()` contains
   an *earlier* insufficient-funds refusal and that is the same defect. That is
   the point: "the run went red" was never evidence.
   The count is a measurement, not a constant, and it is pinned to the `api/`
   revision it was taken against: at the previous revision (`aebea943`) it was
   six, because the block's key row was a `COUNT == 1` clause that a burned key
   satisfied — this fixture is what showed that, and the provenance clause that
   replaced it is what the seventh flip now measures.
   That clause is `rk_transfer_ids == [retried_id]` (`api/selfcheck.py:506`),
   list equality against a one-element list, so the one assertion covers both
   defect shapes: a mutant that records only the refusal (borrowing a real id)
   gives `[borrowed]`, and one that records the refusal *and* the apply gives
   `[borrowed, retried_id]` — neither equals `[retried_id]`. That is also why the
   surviving separate count in the block is the transfers row and not a second
   key row: cardinality was never the clause that could see this.
   The attribution test therefore asserts the rows *before the first refusal* are
   untouched, which is what a faithful one-behaviour mutant guarantees and a
   broken harness would not.
   Falsifiability was checked directly: with a mutant that refuses but does not
   claim the key, the flip test fails.
   Boundary note: this is the only place in `tests/` that imports `api/`; the
   rest of the gate stops at `Ledger`, deliberately.

10. **Client key reuse across a crash** (`test_client_retry.py`,
    `t4_client_child.py`) — the T4 obligation lives entirely in `app/`: the key
    must be minted and durably persisted **before** the first attempt and reused
    **verbatim** on every retry. A client that remints on retry is a double-spend
    the ledger cannot catch, because a different key is a different transfer —
    and no invariant in `ledger/` or assertion in `api/` can see it, since both
    are behaving correctly.
    So the test drives the real client modules as a **real process**: the parent
    runs a real `api/` server over a real on-disk ledger, a child performs the
    send and is `SIGKILL`ed *after* the server answered 201 and *before* it could
    settle, and a second child resumes from the persisted store. The crash is
    deterministic, not raced: the child kills itself from inside its own
    `ApiClient` transport, which is injectable for exactly this purpose, so the
    server's answer has been read and the record is still `pending` on disk. An
    in-process `raise` would prove nothing here — the obligation is about the
    process dying.
    Asserted on **wire evidence**, never on a re-read of the client's own store:
    the outgoing `Idempotency-Key` as the transport put it on the wire, and the
    ledger's own verdict read with SQL (`idempotency_keys`, `transfers`,
    `ledger_entries`). The store file is only a fixture carrying state between
    the two children.
    The mutant is a retry path that mints a fresh key
    (`FreshKeyOnRetryStore.outstanding`), and
    `test_a_client_that_remints_on_retry_double_spends` runs the same flow
    against it and asserts the double-spend lands: a different key on the wire,
    two transfers, the payer debited twice. Without that test this file would be
    green without ever having been shown able to fail.
    Deliberately **not** a copy of the client's own `app/selfcheck.py` driver:
    two artifacts that share code agree because they share a bug. These two share
    only the contract and observe the wire independently.

11. **Reload of the send response** (`test_web_reload.py`) — the browser half of
    the key's lifetime, on the branch that carries money. The web layer must
    leave the browser holding a **GET** after every press. If the failed press
    answers with the rendered page instead of a 303, the address bar stays on
    `POST /send`, so a plain reload re-issues the POST, `PendingStore.begin`
    mints a second key, and the server applies a second transfer. Both transfers
    are balanced and correctly keyed and `api/` is behaving exactly as specified
    — no invariant in `ledger/` can see it. The money simply moves twice.
    The branch driven is `except RetryableSendError`, where the key is minted and
    the transfer has already applied server-side. The other failure branches are
    **not** driven, and that is a coverage gap, not a defect — measured, not
    assumed: with the terminal-refusal branch reverted to the pre-fix shape the
    file still passed (`Ran 4 tests … OK` as the reviewer measured it at
    `tests/test_web_reload.py 126976ae`), and the three contract tests reproduce
    that green here. On that branch a reload re-posts and is refused again with
    no money moved.
    The assertion is **not** on the page, because the page is what lies here: the
    notice "Nothing was sent twice" is rendered by precisely the code path that
    sent it twice. The evidence is the persisted record count and SQL over
    `idempotency_keys` / `ledger_entries`.
    Modelling the browser has to be explicit: a `3xx` is followed with a GET, a
    `200`/`202` body is answered by re-issuing the POST. `app/selfcheck.py`'s
    `[WEB]` row reloads a `GET /`, which the success-path 303 already protects,
    so it reported `36/36` across a live double-spend. That is the `COUNT == 1`
    blind spot in different costume: **a label over-claiming its scope**.
    Teeth, since the broken bytes no longer exist — no version control, and the
    fix landed at `app/web.py 9e254fac` — so the pre-fix branch of `_handle_send`
    is reconstructed as `PreFixSendHandler` from `f9de7cf2`, and
    `test_the_pre_fix_shape_inverts_the_assertion` runs **the same
    `_assert_reload_is_inert`** the contract test runs, requiring it to raise and
    to name the invariant that fired. Severity is pinned by
    `test_the_retry_press_does_not_compound_the_leak`: the pending key was
    genuinely recorded server-side, so Retry replays it. The leak is one
    duplicated debit per failed press that gets reloaded, not a cascade.
    Boundary: it patches no global state — the client is injected per server via
    `create_ui_server(client=...)` — so unlike `test_wire_refusal.py` it carries
    no serialization requirement.

12. **Unforeseen failure on a POST** (`test_web_reload.py`) — obligation 11's
    property made structural, because a list is not a guarantee.
    Obligation 11's protection was carried by the handlers' `except` lists, and a
    list is a claim about the completeness of an enumeration: six of six clauses
    were correct and the seventh path — `SendProtocolError`, raised at
    `app/send.py:138` when a 2xx body carries no usable `transfer_id` — was
    invisible to it. An unhandled exception here is not a 500; it is *no status
    line*, which is a re-POST, which is the same double-spend door. So the
    guarantee now belongs to *exiting* `do_POST` (`app/web.py aa2cccce`): a
    `finally` answers 303 unless a status line was already committed. Naming the
    eighth type is not the fix; not needing to name it is.
    The test injects the shape no enumeration can name: a transport that performs
    the transfer over real HTTP and *then* raises a type that appears on no
    `except` list and is not an `OSError`, so `ApiClient` (`app/client.py:76`)
    does not wrap it into the `ApiError` the clauses do name. The press must
    still answer 3xx, the reload must be a GET, and the record must be left
    **pending** — the transfer applied under that key, so replaying it is what
    makes the next attempt a replay instead of a second transfer.
    Both halves have permanent mutants. `NoExitGuardHandler` is the live
    `do_POST` with the guard removed; under it the press commits no status line
    at all, the assertion fails naming that, and the browser's re-POST costs a
    second debit. `DiscardingExitGuardHandler` answers 303 *and* discards the
    record, so it is green on every assertion a page-reader would write while the
    debit stands and no key remains that names it. The state assertion is what
    catches the second one, which is why "it redirects" is not the requirement —
    the record is.
    `NoExitGuardHandler` is reconstructed from `app/web.py 9e254fac`, whose bytes
    the fix overwrote, so as with `PreFixSendHandler` its fidelity is a judgment
    call and not the author's to certify.

13. **Persist-before-attempt has a falsifier** (`test_persist_ordering.py`) —
    the durability rule of §2.3, and the row that states it given teeth.
    `app/selfcheck.py`'s temporal row asserts
    `transport.store_at_send == [(key, True)]`: the store FILE was read from
    inside the transport at the instant the request left. That is the only place
    the ordering is observable, because a read taken after the POST returns
    cannot separate the two builds — a build that sent first and persisted
    afterwards leaves the same single record, the same key, the same amount and
    the same `pending` state, so every count-shaped assertion around it stays
    green. Cardinality is not durability.
    The row shipped with its witness in a comment and a scratch run, which is the
    shape this suite exists to remove: a claim whose witness cannot fail. The
    falsifier is a store subclass whose `begin` suppresses the write the mint
    performs, plus a transport subclass that pays that write on the far side of
    the HTTP call. Both are monkeypatches of module globals held for the duration
    of one `scenario_web_ui` call — the technique `test_wire_refusal.py` already
    uses on `api.app.LedgerPool.ledger` — and the store substitution is keyed on
    the first UI scenario's store path, so the two later UI servers in the same
    scenario run on unmodified stores. Like obligation 9 it patches a
    process-global during the call, so it carries the same rule: not
    parallelized.
    Two tests, because a mutant with no control proves nothing. The control runs
    the scenario unpatched and requires it entirely green, and that the row under
    test actually ran. The mutant requires **exactly one** row to flip, that it is
    this one, and that the deferral was really in force at send time — an
    attribution assertion, because "the run went red" was never evidence.
    Measured: control green over the whole scenario; mutant
    `FAIL … persisted BEFORE the attempt [store_at_send=[('<key>', False)]]` and
    nothing else. `app/selfcheck.py` itself is closed and was not touched; the
    falsifier belongs on the `tests/` side, where the gate runs it on every run
    rather than once on one host.

14. **Accounts: create and switch** (`test_web_accounts.py`) — the wallet UI's
    create form and presentational account switch (room plan #19/#20). **Red-first
    by construction**: it was written against `app/web.py aa2cccce`, which has no
    `/create` route and no `?account=` handling, and it was landed red — 7 of its
    rows failing, each naming the contract row it pinned — before T15 existed.
    The GREEN run is what made it evidence: after `app/web.py` became `248d97a7`,
    the full gate is `Ran 68 tests … OK`, phases 2–4 clean, `63/63 checks passed`,
    pin unmoved. (It is `Ran 72 tests` since T18 added the disclosure pair back
    and the pending-sender pair; see below.) **And the GREEN run earned its keep by finding two defects in
    this file, both mine, both invisible while the file was red** — see the note
    at the end of this obligation, because that is the reusable part.
    What it pins:
    creating an account answers 303 and lands on a GET of the new account, mints
    no idempotency key and writes no ledger entry; `GET /?account=<id>` renders
    that account's balance and not the boot account's; an unknown id is a named
    error page rather than a 500; `Retry` replays every record in the store —
    not only the on-screen account's — each with **its own key and its own body**;
    every POST that can mint a key or move money (`/send`, `/retry`, `/create`)
    answers 303 and lands on a GET, while an *unrouted* POST — which can do
    neither — must still **commit a response** rather than die on the socket,
    with the store byte-identical and no key minted. That last row is the
    narrowed form of "every POST answers 303", which was an over-claim; the
    narrowing and the row both came out of this file's own report, and the row is
    a guard rather than a red-first row.
    **The load-bearing row is the store.** `ui-pending.json` is account-agnostic
    and must be byte-identical before and after both a create and a switch, with
    no second store file appearing, because a per-account split would
    reintroduce the store-loss double-spend this project already extracted once.
    That row is a pure function, `_store_violations(store_path, before)`, and it
    is not left to trust: two tests drive it against the implementations the rule
    forbids — a per-account file, and a rewrite of the one file — so the guard
    cannot be vacuous. It is the one part of this file that is green immediately,
    which is what a falsifier is for.
    **A false pass found in this file's own first draft, recorded because it is
    the reusable lesson.** `test_retry_…` asserted `assertIn(other_account, page)`
    to pin which account was on screen. It passed on the broken tree: the id had
    arrived from the pending block's payee (`send $2.50 to acct-t14-other`), not
    from the account the page was showing. A substring search over a page is
    satisfied by *any* element on it. The assertion now reads the page's own
    account label through `_active_account()` and fails loudly if no such element
    exists. Same family as the label-versus-assertion and
    state-the-check-not-the-inference rules above: the check has to be taken on
    the element that answers the question.
    **Stated, not hidden:** the addendum's mechanism is a client-supplied hidden
    `account` field, so the UI no longer confines sends to the boot account —
    holding an id is the capability. On this demo that is already the model
    (ids are minted uuid hex, shown only to their creator), but it is a change in
    kind from a single-account console and belongs in front of the reviewer.
    **A row withdrawn on a false premise, then restored (T18).** The
    frontend-engineer asked this file to also pin "every `text/html` document
    carries `DEMO_NOTICE`". It was written and green, then withdrawn by the
    planner, who believed the property already ran in the gate via
    `app/selfcheck.py` "phase 4" — and I removed it on that premise after
    checking the competing home and finding it stronger. The premise was false in
    a way neither of us had measured: `run_gate.sh`'s phase 4 is
    `python3 -m api.selfcheck` (`run_gate.sh:138-139`) and the script never
    invokes `app/selfcheck.py`'s driver. Restored, and the ruling recorded here
    because the mechanical rule generalises: **what the gate runs is read out of
    `run_gate.sh`, never inferred from a filename that looks like it belongs
    there.** Two files one letter apart (`app/` and `api/`) were involved.
    The finer fact, which is why the withdrawal looked plausible: the notice rows
    do reach the gate, but *incidentally* — `app/selfcheck.py`'s
    `scenario_web_ui` (`:407`, notice rows at `:445-467`) is driven by
    `tests/test_persist_ordering.py:189`, whose `:203-208` asserts that every row
    the scenario produced is green. So the property had a check that was enforced
    by a blanket assertion about someone else's rows, and would have vanished
    silently had that assertion been narrowed to a subset. A named row with its
    own falsifier is the stronger home; it is back.
    Attributing the drafting lesson: writing it found a third defect of the same
    family as (a) and (b) at the end of this obligation — the falsifier's mutant
    called `app_web._document` to produce its output, which under
    `mock.patch.object` *is the mutant*, so it recursed until `RecursionError`
    instead of testing anything. The rule, stated precisely (and narrowed after a
    first, too-wide phrasing on the board): **never call the attribute you have
    rebound; always call the object you captured before rebinding.** A mutant
    that calls *through* the captured original is correct and is the better
    witness — it keeps every other rendered field real, so a kill is attributable
    to the clause under test rather than to a second difference the mutant
    introduced. Both mutants in this file do that: `_document_without_the_notice`
    calls `_REAL_DOCUMENT`, `_pending_block_without_the_sender` calls
    `_REAL_PENDING_BLOCK` and removes exactly the phrase the fix added.
    **The pending block says who is sending (T18's second row).** `_pending_block`
    lists the whole store unfiltered, so B's page can show a transfer the viewer
    is not party to; before the fix each item read `send <amount> to <to> (not
    confirmed)` and A's transfer read as B's. The row asserts that an item
    rendered on a page showing a *different* account names the record's
    `from_account_id` — and it reads the **item element**, not the page, because
    an id anywhere on the document (an activity counterparty, the account label,
    the hidden form field) satisfies a whole-page search while saying nothing
    about the record. Its falsifier patches `_pending_block` with the pre-fix
    renderer, reconstructing it by stripping the ` from <id>` phrase the fix
    added — a reconstruction, so its faithfulness is a reviewer's item, not the
    author's.
    Attribution: the surface (`POST /create`, the field names, the fallback
    rules) is the frontend-engineer's statement, not the author's reading; the
    account-agnostic-store and whole-store-retry rules are the room plan's.

    **The GREEN run found two defects in this file, and they are the reason a
    red-first file is not evidence until it is green.** Both were unreachable
    while the file was red, because the missing `/create` route failed the rows
    before the flawed assertions inside them could run.
    (a) A *malformed* assertion. One row ended with a sanity check written as
    `assertNotIn("<html", page.lower().replace("<!doctype html", ""))`. The
    doctype in the source is already lowercase (`app/web.py:156`), so the
    `replace` did strip it and the remaining text is `<html lang="en">` — the
    check asserted that an HTML page contains no `<html>` start tag, which is
    false for every implementation. (The frontend-engineer supplied that
    mechanism and is right; this author's first account of it — "lower-casing
    first makes the replace a no-op" — was wrong, and the planner's independent
    re-run of the expression confirms the unsatisfiability either way.) It was
    removed and replaced
    with a real one (an unknown account must not render an `Account <code>`
    label, i.e. must not claim to be showing the boot account). The row's actual
    assertions, that the page answers 200 and names the id, were passing and were
    left alone. Stated plainly because "an assertion was deleted after it failed"
    is exactly the shape that has to be justified rather than absorbed: the
    deleted line asserted a property that is false, not a property the code
    violated.
    (b) An assertion that **violated this suite's own scoping rule**, two
    sections below. The create row asserted `idempotency_row_count(conn) == 0`
    after the create. That is a global count, and the *fixture* legitimately holds
    two keys from its own funding transfers, so the check was false on arrival and
    said nothing about the create. It is now a **delta** measured around the
    create. The correction did not weaken it: any key the create minted still
    moves the number. The rule it broke was already written down in this file,
    which is the uncomfortable part worth keeping.

15. **Design markup: the id is whole where it is read, and never replaced by a
    name** (`test_web_design.py`) — the two constraints the designer stated for
    `app/design.py` (room plan #12), asserted against rendered markup because
    that is what they are claims about.

    `app/design.py`'s subject *is* the renderer — each component takes plain data
    and returns an HTML string — and `app/web.py:164-173` composes them by passing
    the server's values straight down. That trace is what makes the fixture
    honest: `name` is the server's `owner_id` and each activity row's label is its
    `counterparty_owner_id`, so the component is the injection surface, and a
    hostile string handed to `wallet_header(name=...)` here is the same string the
    server would hand it. *Which* component appears on which page is
    `test_web_accounts.py`'s subject, not this file's.

    Three things worth recording, because each was a correction rather than a
    first draft:

    (a) **The spec and the module disagree, and the row is on the module's side.**
    `DESIGN-SPEC.md:362` says the header id is *truncated* with the full id in a
    `title` attribute; `app/design.py:413` renders the address whole and its
    `title` is the static string `"Account ID"`. The module and the designer's
    instruction agree with each other, so the spec row is the artifact raised to
    the planner as the discrepancy. **Ruled** (planner, `600f2b9e`): the spec
    sentence is the stale artifact, the row certifies the **module**, and the
    designer is not to be asked to change working code. If the spec ever becomes
    the live intent, `test_the_header_shows_the_address_whole` is the row that
    fails, and it is meant to.

    (b) **A row I wrote from an inference, and the measurement that refuted it.**
    The first version of the switcher row asserted the `aria-current` entry had
    *no* full-id affordance, inferred from its having no `title`. It failed: the
    entry carries the id percent-encoded in its `href`. The row is now
    `test_the_full_id_stays_reachable_in_both_switcher_branches` and asserts the
    measured property in both branches — the plain entry through `title`, the
    active entry through `href` — so the check's shape is on the record instead of
    in the author's head.

    (c) **The `_esc` mutant carries a hostile fixture, or it is inert.** Removing
    an escaper changes nothing if nothing in the data needs escaping, so a mutant
    run against names like `acct-abc123` would be green for a reason unrelated to
    escaping. The fixture carries `<script>`, `"` and `&` through `owner_id` and
    `counterparty_owner_id` specifically, and each mutant **asserts its own
    premise** — that the defect it injected reached the output — before it asserts
    the effect.

    (d) **One row is about a coupling between two components, not about either
    one** (designer, `101dc286`). Every other row here hands one component its
    data and reads that component's output. The last pair is different in kind:
    the header is the *only* place the active account's id is **readable**, because
    the switcher's active entry carries it only percent-encoded in an `href` and
    has no `title` at all. An `href` is machine-reachability, not
    reader-reachability — so if the header ever truncated again, every
    single-component row above would stay green while the account's own id became
    unreadable on a touch screen. `test_the_active_id_is_readable_on_the_wallet_page`
    therefore measures the **real composition**, `app/web.py:render_wallet` (the
    function the server calls at `:422`), with an `HTMLParser` that separates body
    **text nodes** from **attribute values** — the distinction the claim is about.
    Its falsifier patches `app_web.wallet_header` to truncate and asserts its own
    premise first: the id must still be in `attrs` (machine-reachable) and must be
    absent from `text` (readable), or the mutant is measuring a page that *lost*
    the id rather than one that hid it.

    Every constraint is paired with a permanent in-gate falsifier that injects the
    defect into the *real* component's own output and runs the same predicate
    function the direct row uses, so a green falsifier is evidence about the
    predicate rather than about a re-typed copy of it. Separately, and not as gate
    evidence, all four defects were confirmed to redden the direct rows from a
    **source-level** edit to a copy of `app/design.py` (header truncation, escaping
    removed, the activity title dropped, the switcher title dropped) — in-test
    string surgery on output and a real broken renderer are different claims, and
    only the second one is about the module.

16. **C3.7's negative: the create-success document announces no welcome**
    (`test_web_accounts.py`) — the plan's own checkable form, "the create-success
    document contains no occurrence of `welcome` (case-insensitive)", asserted at
    the gate so the ratified decision cannot regress silently.

    The two rows are `test_the_create_success_document_announces_no_welcome` —
    which creates an account over the real stack, follows the 303 to the document
    it lands on, and searches it, plus the wallet GETs, through one shared
    predicate `_welcome_occurrences` — and
    `test_the_no_welcome_row_fires_on_a_grant_announcement`, which patches
    `app.web.create_form` to append the ruled-out copy (C3.7(a)'s shape: a note on
    the create form) and requires the **same predicate** to report it. The
    falsifier asserts its own **premise** first — the injected copy must reach the
    served document — because this file has already once been fooled by an inert
    mutant whose silence read as a pass.

    Three things worth recording:

    (a) **The row is deliberately not vacuous, in two independent ways.** The
    landed-on document must be the new account's real page (`_active_account`
    reads the address element; the balance element must render
    `format_minor(OPENING_GRANT_MINOR, "USD")`, computed rather than spelled), so
    an error page or a stub cannot be "no welcome" by emptiness; and the
    falsifier supplies a document that *does* carry the copy.

    (b) **The plan's checkable form is a raw substring search, so the fixture has
    to stay out of its own way.** The first draft used the fixture name
    `t14-welcome-owner` and went red **on correct code**: the occurrences it found
    were the user's own account name, echoed into the `<h1>` and the switcher. The
    negative is a property of the app's *copy*; the fixture name is now neutral
    (`t14-owner`), and the boundary is written into the row's docstring rather
    than left as an accident of the fixture. A creator who names their account
    "welcome" would still redden the literal search — that is a fact about the
    check's shape, raised as such, not a defect in the implementation.

    **The direction of that defect is one-way, and it is worth stating because it
    is what makes the row safe to keep.** A search over the whole rendered
    document can only ever report too **many** occurrences: anything the app puts
    on the page is inside the string being searched, so the search cannot miss
    copy that is present. It over-reports (a legitimate use of the word, the
    user's own name); it does not under-report. So a red from this row is a claim
    about the *fixture* until the rendered document is read, while a green is a
    real statement about the served bytes. An *alias* collision — the account name
    carrying the copy — can therefore only move this row toward a false positive,
    never toward a false negative, and the row's green half is not weakened by it.
    The same is not true of element-scoped rows, where the scope can silently
    exclude the very node the defect lives in; those need the exclusion written
    down instead (obligations 13 and 15).

    (c) **What the row does not cover.** Documents the UI does not assemble —
    the stdlib `send_error` pages named in obligation 13's boundary note — and any
    HTML not produced by `app/design.py`'s components. The claim is the covered
    set.

17. **The kill-and-resume scenario's verdict, in the gate**
    (`test_kill_and_resume_coverage.py`) — T33, and the reason it exists is a
    *membership* fact rather than a missing assertion. `app/selfcheck.py` is not a
    gate phase (`run_gate.sh:138-139` is `api.selfcheck`), and its scenario
    functions reach the gate only through a `tests/` row that calls them. Until
    this file, the only such caller was `tests/test_persist_ordering.py`, and it
    calls `scenario_web_ui` and nothing else — so
    `app/selfcheck.scenario_kill_and_resume`, **including its mutant run that must
    double-debit**, sat outside every green run quoted in this room. A regression
    on that path was invisible to the gate whether or not its rows read green.
    `app/selfcheck.py`'s own docstring asks for this row in as many words.

    The row asserts the scenario's verdict over the scenario's **own** rows: both
    runs (`mutate=False` and `mutate=True`), bounded by an index slice taken
    around the calls, every label checked to be one of the scenario's two. It
    re-asserts none of the scenario's conditions and copies none of them — the
    owner keeps the assertions, this file makes them gate-visible.

    Non-vacuity is asserted rather than assumed: the slice is judged by one
    shared predicate, `_slice_problems`, which requires it to be non-empty, to
    contain only this scenario's labels, to contain **both** runs, to contain the
    row the falsifiers attribute to — and, per run, to hold **at least** the
    breadth recorded in `MIN_ROWS` (9 normal, 6 mutant; measured 2026-10-05 against
    `app/selfcheck.py 1fbfdbee0c49f777`, where both runs sat **exactly** on the
    floor, so a one-row removal has no slack to hide in — re-counted at
    `ce42ba10` on the way there and at `54c300040059ed4b` when the row was first
    written, 9 / 6 at all three, the same breadth across three revisions of the
    file). That last clause is the one that matters most
    and it came from the integrator (`59427f8b`): the coverage that existed before
    this file was a blanket all-green assertion, and that form stays green when a
    row quietly **disappears** — so a scenario could be narrowed with nothing to
    show for it. A floor rather than an equality, so the owner adding rows does not
    redden this file and a removal does.

    Two falsifiers, one per failure shape, and the report keeps them apart because
    they are different defects:
    - **a row goes red** — a store-loss mutant (the parent's view of the pending
      store reports no outstanding record while the record is really on disk)
      must flip exactly **one** row, `SURVIVING_RECORD_ROW`, and the report must
      name it as failing without calling it a narrowing;
    - **a row disappears** — `_check_swallowing_a_row` drops one of the normal
      run's rows, so the scenario makes one fewer assertion and every assertion it
      still makes **passes**. The coverage row must still redden, and the report
      must say *narrowed* without inventing a failure.

    Each premise is measured before its effect (the real reader sees the record the
    mutant hides; the swallowed row really was swallowed), so an inert mutation
    cannot make a guard's silence look like a pass.

    Boundary: this covers the two runs `main` makes of this scenario and nothing
    else in that file. `run_local_checks` and the web scenarios remain outside the
    gate — the rest of `app/selfcheck.py` is the owner's runnable evidence, not
    enforcement, and saying "the gate is green" still does not mean "the repo is".

18. **An account id is opaque, and a refusal names what it refused**
    (`test_web_accounts.py`, classes `AccountIdsAreOpaqueInAPath` and the
    taken-address rows of `AccountsAndSwitching`) — T41 and T42, and they share
    one shape: a **string the API accepted** being turned into something else on
    the way to a human.

    **The id.** `app/client.py` builds `/accounts/{id}/balance` and
    `/accounts/{id}/activity` by interpolation, and a URL path segment is not an
    account id. Three failure shapes, and only the first is "no answer": a space
    or a control character raises `http.client.InvalidURL` inside urllib so the
    request is never sent; `/`, `?` or `#` re-cut the path onto a different
    route; and a `%` makes the server decode the segment one time more than the
    caller meant, so an id that looks like the encoding of another id **reads
    that other account, with a 200**. The rows create two accounts that are each
    other's trap (`acct a b` owned by `alice`, `acct%20a%20b` owned by
    `mallory`) and require each to read back as its own — keyed on `account_id`
    and `owner_id`, never on the amount, because a retarget that landed on the
    same balance would slip past a figure.

    Each of the two call sites has its own falsifier, and each asserts its
    **premise as the retarget itself** — an answer came back for the *other*
    account — rather than as "the read raised". That distinction is the finding:
    a mutant producing only `InvalidURL` would satisfy "the row went red" while
    leaving the silent wrong-account read in place. Under each mutant the sibling
    site is asserted still quoting, so one site reverted means one row red and
    the attribution is exact. The mutants are built by `inspect.getsource` of the
    landed method with `quote` rebound to the identity, not retyped, so they
    cannot drift from the body they mutate and they revert exactly one site.

    Boundary: this stays **client-side**. `%20` in a path segment *is* a space
    (RFC 3986), so decoding it at the server is correct and hardening the parser
    against it would be a regression. And `tests/test_web_accounts.py`'s own
    `RecordingTransport` asserts an **unquoted** path
    (`urlsplit(url).path == f"/accounts/{self.account_id}/balance"`), green only
    because its fixture id is legal — a trap for the next row that reuses that
    fixture with a non-safe id, recorded here rather than left to be discovered.

    **The refusal.** Two names can derive one address (`Grace Hopper`,
    `grace  hopper` and `Grace-Hopper` all render `acct-grace-hopper`), so a
    create refusal whose subject is the *name* is describing something that is
    not taken. The `name_taken` redirect therefore carries the address in its own
    parameter and the create card renders it. The row asserts the **composition**
    and no wording at all — the address travels in its own parameter, it is the
    address the server actually created (read from the first create's landed
    page, not re-derived, so a derivation that moved cannot make the row agree
    with itself), and the create card's banner is where it lands. A sibling row
    pins the explicit-id refusal (`account_exists`), so the address sentence
    cannot pass by flattening both branches into one.

    The read is scoped twice, and both scopes are load-bearing: `banner--error`
    is emitted by page-level banners too, so the class alone is not
    discriminating; and a document-wide search for the *address* is green on the
    broken tree because `wallet_header` carries the active id in a `title`
    attribute. The row reads the `section#create`, then the banner inside it,
    then the text node.

    Red-first was **not** available here: `app/web.py` landed the fix before the
    row could be written. The substitution is stated plainly rather than papered
    over — the red is delivered by an in-gate mutant that restores the pre-fix
    rendering (the static copy, no address, no placeholder) and leaves every
    other line of the landed code in place, with its premise asserted first so an
    inert patch cannot make the row's red read as proof.

### Scoping rule

Assertions on the operation under test are scoped to that operation: the key it
used (`idempotency_count_for_key`), the transfer id it produced
(`transfer_count_for_id`), and **deltas** in `ledger_entries` / `transfers`
measured around it. A global table count includes the fixture's seed transfers
and never tested what it claimed to.

## Why `test_harness_selfcheck.py` matters

A test that cannot fail is not a test. Every scenario is run against a
deliberately broken §2.1 implementation and must detect it:

| Mutant | Defect | Caught by |
|---|---|---|
| `CheckThenActLedger` | payer balance read outside the write transaction | parallel storm **and** the obligation-6 mutation probe (red run required) |
| `UnbalancedLedger` | credit half of the double entry never written | parallel storm / I1 |
| `NonIdempotentLedger` | idempotency key ignored | retry storm |
| `NonAtomicLedger` | each entry committed separately | failure atomicity |
| `TruncatingSplitLedger` | remainder truncated away | rounding sweep / I6 |
| `CoercingLedger` | `int()` instead of reject: `12.34`→12, `"500"`→500, `True`→1 | amount-validation matrix |
| `RefusalBurnsKeyLedger` | a refusal is recorded under the caller's key | refusal-path scenario |
| `RejectAfterWriteLedger` | applies the transfer, then refuses it | amount-validation matrix (untouched check) |
| `BurnsKeyOnBadAmountLedger` | records the key for a refused amount | amount-validation matrix (key check) |
| `ShapeCheckLedger` | `int(v) == v` shape check instead of a type check | amount-validation matrix (`1.0` only) |

Each mutant is also run against a **correct control** in the same file, so a
detection cannot be a false positive from a scenario that is red no matter what.

## Rules this suite obeys

- It is never weakened to make a red gate green. A red gate is a finding.
- No production code lives here. `ControlLedger` and the mutants are test
  scaffolding for the self-check; nothing in the gate ships them.
- Obligations 1 and 2 are genuinely concurrent (`threading`, released from a
  shared barrier, one connection per request). A sequential loop does not
  satisfy them and is not used.
- If a frozen §2.1 signature turns out to be wrong, that is reported to the
  planner — the test is not silently adapted to the implementation.
