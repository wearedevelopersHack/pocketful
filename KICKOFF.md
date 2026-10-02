# Kickoff — Pocketful

*A message for the room. Nothing below has been sent; sending it is the go signal.*

---

**@planner** — new room, new project. Read the attached room plan
(`INVARIANTS.md — Pocketful — Money Invariants`) before you do anything else. It
is the contract for this build and it outranks any plan you write.

**The objective.** Build Pocketful: a wallet and payments app — accounts, balances,
sending money between users, an activity feed. The hard part is not the UI. The
hard part is that **money must never be created, destroyed, or spent twice** under
concurrent transfers, retries, and rounding.

**Your first job is not to build. It is to decompose.**

Before any implementation task is assigned, produce on the board:

1. **A stack decision.** Nothing is chosen yet — language, datastore, API shape,
   test runner. Propose one, with the one-line reason each part is right for a
   ledger, and flag it for the human. This is the first thing that gates
   everything else.
2. **The interface contracts**, written down as concrete signatures before anyone
   implements against them:
   - the ledger's public operations (open account, transfer, get balance),
   - the API request/response shapes, including where the idempotency key and
     amount live on the wire,
   - the client's obligations (key generation and persistence).
3. **The task tree**, with exactly one owner per task and one owner per file, and
   each task's definition of done written explicitly.

**Ground rules for the room.**

- **Definition of done is evidence.** A task is done when its owner has quoted the
  exact command they ran and its passing output. A claim with no output is not
  done. That standard applies to you as well.
- **One owner per piece of state, one owner per file.** If two agents would edit
  the same file, that is one task, not two.
- **The test author owns the concurrency harness and the gate.** Nobody else
  edits `tests/`. If the gate is red, it is red — it is never made green by
  changing a test.
- **The reviewer is a gate, not a comment box.** A money-path change with an
  invariant defect is blocked.
- **There is no version control on this machine.** `git` is not installed. Do not
  run git commands, do not plan around branches or diffs. The board is the only
  history, and the working tree is the truth.

Report back when the decomposition and the stack proposal are on the board. Do not
start implementation until the human has answered the stack question.
