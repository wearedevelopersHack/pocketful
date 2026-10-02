# Pocketful — Money Invariants

> This document is the shared contract for every agent in this room.
> It is not advice. Each rule below is a **testable obligation**, and any change
> that violates one is a defect regardless of whether tests pass.

## 0. The one invariant

**Total value in the system is conserved.** No operation may create, destroy, or
apply money twice.

Everything below exists to make that statement enforceable.

---

## 1. Representation (non-negotiable)

Money is an **integer count of minor units** (cents).

- Type: signed 64-bit integer (`i64` / `bigint` / `int64`). Pick one name and use it everywhere.
- **Never** `float`, `double`, `f32`/`f64`, or binary floating point. Not once, not for a percentage, not for an estimate, not "just in the UI".
- **Never** a fixed-point decimal library as the *storage* type unless it stores an integer internally — the persisted column and the wire format are integers.
- Currency is an explicit field, not implied. Multi-currency arithmetic is forbidden without an explicit conversion step carrying its own recorded rate.

**Why:** `0.1 + 0.2 != 0.3` in binary floating point. In a ledger that is money
appearing from nowhere. This is the single most common way this class of system
is silently wrong.

## 2. Double-entry

Every mutation to balances is recorded as **two or more ledger entries that sum to zero**.

- A transfer of 500 cents: `-500` from payer, `+500` to payee. Sum: `0`.
- A fee: `-500` payer, `+495` payee, `+5` fee account. Sum: `0`.
- Balances are a **derived view** over ledger entries. If a balance is stored as a
  cached column, it must be reconstructible from entries at any time, and a job
  must prove that.

**Forbidden:** any code path that mutates a balance without writing a corresponding
entry. Any "adjustment" that is not an explicit pair against an explicitly named
system account.

## 3. Idempotency

Every state-changing request carries a **client-supplied idempotency key**.

- Same key + same request ⇒ the operation is applied **exactly once**, and every
  subsequent call returns the **original result** (same transfer id, same status).
- Same key + *different* request body ⇒ rejected with a conflict error. Never silently
  apply the new body.
- Keys are stored with the result, atomically, in the same transaction that applies
  the transfer.

**Forbidden:** generating the idempotency key server-side from a timestamp, a random
value, or the request hash alone. The key is the *client's* claim of "this is the
same request I already sent."

**Why:** retries are not an edge case. A mobile client on a flaky network retries
constantly, and a retried transfer without idempotency is a double-spend.

## 4. Concurrency

- Reads that feed a write must be performed **inside the same transaction and
  isolation level** as the write. No "check then act" across transaction boundaries.
- No lost updates. Concurrent transfers touching the same account must serialize on
  that account (row lock, or optimistic version check with retry-on-conflict).
- The isolation level is **stated explicitly in code**, not inherited from a default.
- Deadlock is handled by retry, not by reducing isolation.

**Forbidden:** read-modify-write on a balance without a lock or a version predicate.

## 5. Rounding

- There are no fractional cents. Ever.
- Any operation that splits an amount (bill split, fee split, percentage) must
  distribute the remainder **deterministically**, and — this is the testable part —
  **the parts must sum exactly to the original total**.
- Rounding direction is a documented policy, not whatever the language's default
  division does.

**Why:** naive splitting loses or gains a cent per operation. Over many operations
that is money created or destroyed.

## 6. Executable invariants

These must be runnable as assertions, not prose. At minimum:

| # | Invariant | Checked by |
|---|---|---|
| I1 | `SUM(amount) over all ledger entries == 0` | after every test scenario |
| I2 | `SUM(balance) over all accounts == 0` (closed system) | after every test scenario |
| I3 | Every transfer's entries sum to `0` | per-transfer assertion |
| I4 | No account balance is negative unless its policy allows overdraft | per-account assertion |
| I5 | Replaying any request with its original idempotency key is a no-op | idempotency suite |
| I6 | Sum of split parts `==` the original total | rounding suite |

## 7. Required test obligations

The test author owns these. They are the gate, not a formality.

1. **Parallel transfer storm** — N concurrent transfers between overlapping account
   pairs; assert exact final balances and I1/I2 afterwards.
2. **Idempotent retry storm** — the *same* idempotency key fired 100× concurrently;
   assert exactly one application and I1/I2.
3. **Rounding sweep** — split a total across N parts for many (total, N) pairs
   including prime totals and N=1; assert I6 every time.
4. **Failure atomicity** — inject a failure mid-transfer; assert no partial entries
   survive and I1/I2 hold.
5. **Invariant fuzz** — a randomized operation sequence; assert I1/I2 after each op.

A test that only runs requests sequentially does **not** satisfy obligations 1–2.
Concurrency bugs do not reproduce in sequential tests.

## 8. Definition of done

A unit of work is done when:

- The change is accompanied by a test that **fails before it and passes after**.
- The full gate (tests, lint, build) runs green, with the command and its output quoted.
- I1 and I2 hold after the relevant scenario.
- No new floating-point arithmetic appears anywhere in the money path.

Claims are not evidence. Quote the command and the output.

---

## Appendix — Anti-patterns (each of these has shipped in real payment systems)

| Anti-pattern | What breaks |
|---|---|
| `float` for money | Silent value drift; I1 fails at scale |
| Balance mutated without a ledger entry | I2 drifts from I1; audit impossible |
| Server-generated idempotency key | Retries double-apply |
| `SELECT` then `UPDATE` outside a transaction | Lost update under concurrency |
| Sequential-only tests for concurrent logic | Bugs ship; the suite is green and useless |
| Rounding by truncation per-part | Remainder cents vanish; I6 fails |
| Retry on timeout by re-sending without the key | Double-spend |
