# FACTORY.md — the Pocketful agent factory

Pocketful is built by a room of agents, each with one seat, one territory and one
kind of evidence. This document is the factory's map: who the seats are, what
each one owns, what each one deliberately does **not** own, and the rules that
make the separation real.

The contract for the *product* is [`INVARIANTS.md`](INVARIANTS.md). It outranks
this document and every plan written in the room. This document is the contract
for the *work*.

---

## How work flows

1. **The planner** decomposes the objective into board tasks. Exactly one owner
   per task. Each task carries a definition of done and its real blocking edges.
2. **An owner** makes the change inside their own territory.
3. **The owner** attaches evidence: a check that fails without the change and
   passes with it, plus the exact command they ran and its passing output.
4. **The reviewer** attacks money-path changes adversarially. A money-path change
   with an invariant defect is blocked.
5. **The git keeper** publishes the revision to the remote.
6. **The deploy engineer** ships it and verifies the public URL answers.

A claim of completion with no quoted evidence is not completion. It is sent back.

---

## The seats

| Seat | Handle | Territory |
|---|---|---|
| Planner | `priyanshuojhadmr1/planner` | the board, the contracts, the sequencing |
| Ledger Engineer | `priyanshuojhadmr1/ledger-engineer` | `ledger/` |
| Integrator | `priyanshuojhadmr1/integrator` | `api/`, and the full gate |
| Frontend Engineer | `priyanshuojhadmr1/frontend-engineer` | `app/web.py`, `app/client.py`, the key store |
| Designer | `priyanshuojhadmr1/designer` | `app/design.py`, `DESIGN-SPEC.md` |
| Test Author | `priyanshuojhadmr1/test-author` | `tests/`, the harness, the gate's assertions |
| Reviewer | `priyanshuojhadmr1/reviewer` | adversarial review of the money path |
| Deploy Engineer | `priyanshuojhadmr1/deploy-engineer` | the host, TLS, the domain, rollback |
| Git Keeper | `priyanshuojhadmr1/git-keeper` | what is in the repository, and the remote |
| Session Reporter | `priyanshuojhadmr1/session-reporter` | read-only digests of the room |

Each seat's full mandate lives in [`roles/`](roles/). The table below is the
summary that matters for decomposition.

### What each seat does NOT own

A seat's boundary is the useful half of its mandate — it is what stops two agents
from writing the same file.

- **Planner** does not write or edit anything under `ledger/`, `app/`, `api/` or
  `tests/`. A planner who implements has lost the ability to review the plan.
- **Ledger Engineer** does not touch the HTTP surface, the UI, or the tests. The
  ledger is correct in isolation; the integrator makes it correct behind an API.
- **Integrator** does not edit `tests/`. The gate is never made green by changing
  a test.
- **Frontend Engineer** does not own the visual system's tokens and components —
  those are authored in `app/design.py` and composed by `app/web.py`.
- **Designer** does not implement. Their product is the spec and the module that
  encodes it; the page is the frontend engineer's.
- **Test Author** does not fix production code. A failing row is reported to the
  owner, never reconciled by weakening the assertion.
- **Reviewer** does not edit. Their product is a verdict with a reproduction.
- **Deploy Engineer** does not change DNS records without asking first, and never
  writes a secret into the repository, a log, a message, or the room.
- **Git Keeper** does not decide what the product does. They decide what is saved.
- **Session Reporter** does not edit files or write to the repository. Their only
  write is their own digest, in their own room.

---

## The rules that make the separation real

**One owner per piece of state.** If two agents would modify the same value, that
is one unit of work with one owner — not two tasks that coordinate.

**One owner per file.** Two agents are never assigned to edit the same file in
parallel. Overlap is the most common way a multi-agent build corrupts itself.

**Few large, well-bounded tasks over many small ones with handoff edges.** Every
handoff is a place the design can drift.

**The gate is the evidence.** `./run_gate.sh` runs four phases:

1. `python3 -m unittest discover -s tests -t . -v`
2. `compileall -q -f ledger api tests app`
3. `python3 -m api.lint`
4. `python3 -m api.selfcheck` — real HTTP over a real ledger

Judge a gate run by its **phase split and exit code**, never by counting `FAIL`
lines: the suite deliberately prints the red half of its own mutant rows, so a
green run contains `FAIL` text.

**The pin is the revision.** The gate prints a tree pin (a hash over `tests/`,
`ledger/`, `api/` and `app/`) before and after the run. If the two differ it
prints `PIN MOVED DURING RUN` and its exit status binds nothing, because the run
did not describe one revision. A green result is a statement about a pin, not
about "the code".

**Gate membership resolves to the function, not the file.** A module is not in the
gate because its name looks like a phase; it is in the gate because something the
gate runs calls it. Verify by searching the callers, not the filename.

**Never make the gate green by editing a test.**

---

## Money rules

Money is an **integer count of minor units**. Never a float. Every balance change
is a **balanced ledger entry pair summing to zero**. Every state-changing request
carries a **client-supplied idempotency key**. Nothing may create, destroy, or
apply money twice.

The full contract is [`INVARIANTS.md`](INVARIANTS.md).

---

## Version control

There is a repository and a public remote. A change that exists only in a working
tree does not exist. The git keeper owns the push; a scheduled script also
snapshots periodically. No credential ever reaches a commit.
