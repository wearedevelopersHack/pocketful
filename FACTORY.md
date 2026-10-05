# Pocketful factory

This is the submission factory for the official Pocketful stages. The earlier multi-agent product demo and its release process are documented in [LEGACY-FACTORY.md](LEGACY-FACTORY.md). The final BAND run uses a separate three-seat roster, generic mandates, and a fresh result checkout. The accepted stage folders are merged into this shared repository with their original commits preserved.

## Roster and setup

| Seat | BAND handle | Responsibility |
| --- | --- | --- |
| API Engineer | `@tuannvd2511/api-engineer` | Coordinates each stage and implements its service. |
| UI Designer | `@tuannvd2511/ui-designer` | Owns the browser interface and interaction checks. |
| Reviewer | `@tuannvd2511/reviewer` | Independently checks the written requirements and delivered revision. |

All three use the Codex ACP harness with model `gpt-5.5`, authenticated with a ChatGPT account. Their reusable instructions are in [mandates/](mandates/). The local Windows launcher was `C:\Users\boy03\AppData\Roaming\npm\codex-acp.cmd`; this path is an example from the run, not a runtime dependency of the service. The factory's final room ID is `bd8d60bb-486a-410c-9e6f-28b03f88f360`; its full BAND export is delivered as `room.json`.

To repeat the factory, create three distinct coding-agent identities in BAND, load the corresponding mandates, authenticate each runtime, point them at one clean result repository, and add them to one room. Dispatch the complete stage sequence once, with paths to the written specifications, the result repository, file ownership, acceptance rules, and instructions for the lead to pass the full task and specification to each recipient. The human does not steer a stage after dispatch. The coordinator makes the ordinary decisions within the room and reports an honest blocker if progress becomes impossible.

## Work and review

The API Engineer reads the entire current specification and delegates scoped work by exact `@handle`. Each delegated message includes the complete task and relevant specification text. Recipients acknowledge the handoff and reply to findings. File ownership is agreed before concurrent edits. The UI Designer owns the user interface once a stage requires one. The Reviewer stays independent from primary implementation, runs checks and inspects requirements outside the public tests, then accepts or rejects the committed revision with a reproducer. A rejected result goes back to its owner for a new change and another review.

Each accepted stage remains in a complete `stage-N/` folder with `Dockerfile`, `RUN.md`, and a service that runs in one container. The next stage starts by copying the previous accepted folder and extending it. The room and Git history provide the evidence for these handoffs and changes. The human writes submission documentation, runs an independent gate, exports the room, and publishes the existing commits.

## Design choices and failure handling

All seats work in one checkout, which makes changes visible to the reviewer and offline harness without syncing sandboxes. The tradeoff is possible edit collisions, so the lead assigns exact file ownership. Agents use the official written specifications for behavior; partial public checks provide feedback and cannot substitute for requirements. The delivered service is isolated in `stage-N/` because the prior hosted Pocketful demo uses a different API contract.

The initial delivery review rejected Stage 1 when its Dockerfile referenced an absent service file. The Reviewer posted the failing build command in the room; the API Engineer then added the service. The first complete host harness run found failures involving idempotency across different paths and export restoration. Later checks exposed a Stage 2 browser payment retry regression and Stage 4 statement balance/correction issues. The implementation and UI seats committed fixes, and the Reviewer separately accepted the corrected revisions. The Windows isolated harness encountered a path interpretation error when passing a Windows-style test path into the Linux test container; the host harness and direct Docker build provide separate checks.

If the runtime fails, the room keeps the error and the seat can resume its assigned work. A reviewer rejection requires a concrete fix and a checked revision. An unresolved requirement is reported as a limitation, never counted as an accepted stage. The public repository preserves the original agent commits and the prior product team's history without squashing.

## Measured run

The complete four-stage dispatch was posted on 2026-10-05 at 04:27:53 UTC (11:27:53 in Vietnam). The latest accepted correction before visual polish was `97ea9d9` at 15:46:25 UTC: 11 hours 18 minutes of wall time from dispatch, including runtime interruptions and review, not continuous model execution. The later visual polish was accepted at 22:16:54 UTC, 17 hours 49 minutes after the original dispatch. Accepted corrections include `f3339e1` and `2bdbbf9` for Stage 4, `0420e01` for Stage 2 payment behavior, and `97ea9d9` for carrying the UI behavior into later stages. `2c03b9f` validates imported Stage 2 state. The integration repository preserved these commits and the earlier product team's history with merge commits, without rewriting either lineage.

The host-mode official harness at integrated revision `ae881946` passed all four stage folders: Stage 1 147/147, Stage 2 35/35, Stage 3 6/6, Stage 4 5/5 (193 checks total). Its report is recorded outside the submission repository under `checks/final-run/integration-s4-02/report.json`. This is public-suite evidence; it does not prove every untested requirement. The later visual polish commit `486d6fc` was authored by the UI Designer after a human asked for a clearer walkthrough interface and pointed out a login navigation bug; it is separate from the initial full-stage dispatch. The UI Designer verified Python compilation, a Flask payment and retry smoke check, and a 390 × 844 Playwright login/payment/logout flow. The Reviewer independently accepted `486d6fc` at 22:16:54 UTC with no blocking findings. The official host harness then found seven Stage 2 browser assertion failures in that polish revision. The UI Designer fixed exact text contracts in `ff12a55` and restored visible handle-to-amount split mapping in `2f6842f` without rewriting the first commit. The Stage 2 browser suite passed 35/35 on each of stages 2, 3, and 4. The Reviewer independently accepted both corrections, including `2f6842f` at 23:01:47 UTC. The final Stage 4 Docker build passed, and the final official four-stage harness was rerun after these corrections.

The three seats were observed using `gpt-5.5` in local Codex session records. BAND's ACP sessions for this room lack agent-session UUID attribution, so `band usage` groups their estimated token cost with unrelated local sessions. We cannot report a defensible room-specific token count or provider bill from that aggregate; elapsed time above is the measured cost available for this run.
