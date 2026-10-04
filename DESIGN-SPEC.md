# Pocketful — Wallet UI redesign specification

**Owner of this document:** Designer. **Owner of the implementation:** Frontend Engineer (`app/web.py`).
**Date:** 2026-10-04. **Applies to:** the Python-served UI at `pocketful.getn.space` (source of truth: `app/web.py`, which serves the document and the stylesheet from the application layer).

This is a specification, not a screenshot. Every value below is meant to be pasted into
the implementation. Where a rule is attached to correctness it says so and cites the
file and line I measured it against.

---

## 0. Scope, and what I do not decide

**In scope (mine):** tokens, page shell, breakpoints, the component set and every state of
it, money *formatting* (never money arithmetic), accessibility budget, and the copy deck.

**Out of scope (not mine), and named here so nobody assumes it is designed:**
the `$100` starting balance is a **ledger behaviour**, not a visual one. I specify how that
balance is *displayed*. Whether it exists, and how it is posted, belongs to the Ledger
Engineer, the Planner and the Integrator. Section 9 records the two places where my copy is
blocked behind that decision.

I do not edit `app/`. If the built UI differs from this spec, I will report it against
section 10 rather than change it myself.

### The three states the Frontend Engineer asked for

1. **Wallet view** — account name + balance + activity + send. §5.1–5.3, §5.6.
2. **Account creation.** §5.4.
3. **New / empty account showing the welcome $100.** §5.8. This is the state that carries
   the most risk of over-claiming, so it is specified twice: once for "the grant is real
   and posted", once for "the grant does not exist yet". The difference is copy, and the
   implementer must not ship the first copy while the second behaviour is live.

---

## 1. What is wrong with the current surface — measured

The complaint was "confusing and simple". Both are specific, and both are in the markup.

| # | Finding | Evidence |
|---|---|---|
| 1 | The account is shown as a **raw id**, never a name. | `app/web.py:204` renders `Account <code>{account_id}</code>`. |
| 2 | There is **no name on the read path**. `owner_id` is returned by `POST /accounts` (`api/app.py:115-122` `account_to_wire`) but **not** by `GET /accounts/<id>/balance`, which returns only `account_id`, `currency`, `balance_minor` (`api/app.py:300-315`), and not by the activity items, which carry `counterparty_account_id` only (`api/app.py:137-154`). | Read directly; independently reported by the Frontend Engineer. **This is an API gap, not a styling gap** — no client-side change can produce a name. **FIXED 2026-10-04 (§C2.5) — this row is the pre-amendment measurement and is kept as the record of why §5.1 degrades.** Current state, re-read: `api/app.py:350` returns `owner_id` on the balance read; `api/app.py:161` carries `counterparty_owner_id` on activity items. **The name is now on the read path, so "clear account names" is unblocked** — §5.1's name-absent branch is now an error path, not the normal one. |
| 3 | Activity rows are **wire fields, not prose**: `debit`/`credit`, a raw counterparty id, a raw timestamp. | `app/web.py:117-130`. |
| 4 | The **empty state is a table cell**. | `app/web.py:119` → `<td colspan='4'>No activity yet.</td>`. |
| 5 | **Pending transfers do not look pending.** A pending record and a settled row are both plain text; pending is a `<ul>` with the same visual weight as everything else. This is the single most important display in the product. | `app/web.py:133-149`. |
| 6 | **Three meanings, one look.** `.notice`, `.pending` and `.demo` are the same rounded tinted box; `.pending` and `.notice` are the *same colour* `#ffd8`. | `app/web.py:95-99`. |
| 7 | **The palette is light-only under a dark-capable declaration.** `:root { color-scheme: light dark }` (line 82) with hard-coded pastels: in dark mode the status boxes are pastel-on-dark and no contrast is controlled anywhere. | `app/web.py:81-101`. |
| 8 | **One column, no breakpoints.** `max-width: 44rem` (line 83) at every viewport; no `@media` rule exists in the stylesheet. | `app/web.py:81-101`. |
| 9 | **No focus, motion or target-size provision.** No `:focus-visible`, no `prefers-reduced-motion`, no minimum hit target. | Same block; absence measured. |
| 10 | **Nothing is hierarchy-ordered.** Send, Create and Activity are three equal `<h2>`s; the create form's optional "Account id" field reads `blank, or an id you choose`, and that id is the actual capability. | `app/web.py:207-242`. |

---

## 2. Constraints the design must live inside

These are not preferences; violating one makes the design unshippable.

**Two tiers, and the distinction matters** (planner, 2026-10-04 — a correction to an earlier
correction of mine). `app/selfcheck.py` is **not** a gate phase (phase 4 is `api/selfcheck.py`),
but rows inside `scenario_web_ui` (`app/selfcheck.py:407-668`) **are** gate evidence anyway:
`tests/test_persist_ordering.py:189` drives that scenario and `:208-211` assert every row it
produced is green, and phase 1 runs that file. Rows inside `run_local_checks`
(`app/selfcheck.py:669-743`) are not reached by anything in the gate — only `main()` (`:779`)
calls it. I verified both by caller search over the enclosing `def`, not by filename.

So: **tier 1** is enforced (a violation reddens the gate); **tier 2** binds as a requirement but
nothing will catch me. Both bind. I flag which is which, because the whole point of §5 is that a
rule nobody enforces is not a rule.

1. **No outbound network at build or run time.** System font stack only, no webfont, no CSS
   framework, no icon font. Icons are inline `<svg>` with `aria-hidden="true"`, or a text glyph.
   *(tier 2 — nothing checks this; it holds because the stack cannot do otherwise.)*
2. **No build step.** The server emits markup and CSS as strings. Everything below lives in
   `app/design.py` — the values in its `TOKENS` table, the stylesheet in its `STYLE`, the
   markup in its render functions (plan §C4). `app/web.py` imports `STYLE` and `_esc` from
   that module and keeps **no CSS and no second escaper of its own**.
3. **`DEMO_NOTICE` stays, unedited, as the first element inside `<body>`.** I am deliberately
   *not* moving it to a footer: `tests/test_web_accounts.py:281` and `app/selfcheck.py:455,472`
   assert its presence by content, and moving it buys nothing worth risking the gate for.
   I only restyle it (§5.7). The constant's text is not mine to change. *(tier 1 —
   `:455,472` are inside `scenario_web_ui`; the mutant at `:465-472` pins that the marker is a
   measurement of the responses, not a constant.)*
4. **`render_wallet` keeps its keyword signature.** `app/selfcheck.py:696-712` calls it as
   `render_wallet(account_id=..., balance_minor=..., currency=..., rows=[], pending=[...])`.
   Add optional keywords if needed; rename nothing. *(tier 2 — that row is in
   `run_local_checks`. Binding because a peer's driver depends on it, not because the gate
   reads it.)*
5. **`_pending_block` must keep naming both accounts.** `app/selfcheck.py:698` asserts the
   rendered pending item contains `acct-sender` **and** `acct-payee`. The redesign must not
   drop either id in favour of a friendly label — add the label *alongside*, never instead.
   *(tier 2 — `run_local_checks`. The in-gate pending row `:491` asserts only the heading
   string and the absence of a UUID, so this rule is real and unguarded.)*
6. **`format_minor` is pinned for these cases.** `app/selfcheck.py:737` asserts
   `format_minor(1234) == "$12.34"` and `format_minor(-5) == "-$0.05"`. Grouping (§6) must
   preserve both exactly. *(tier 2 for those two cases; **tier 1** for the one the gate does
   read: `:428` asserts `"$100.00" in page` on `GET /`, so `format_minor(10000, "USD")` is
   in-gate evidence. I found no `format_minor` assertion anywhere in `tests/` — the
   `12.34` hits there are the amount-**validation** matrix, a different subject.)*
7. **Money never touches a float.** `app/money.py` is the float boundary; the display path
   stays integer `divmod`/slicing end to end. *(tier 2.)*
8. **Some copy is already asserted in-gate, and renaming it reddens phase 1.** A sweep of
   `scenario_web_ui` for literal strings found exactly three, plus the negative ones:

   | Assertion | Line | What it constrains |
   |---|---|---|
   | `"Send money" in page` | `:428` | the send card's heading |
   | `"$100.00" in page` | `:428` | the balance rendering of `format_minor(10000)` |
   | `"Unconfirmed transfers" in page` | `:491` | the pending surface's heading |
   | `"idempotency" not in page.lower()`, `_UUID_ANY.search(page) is None` | `:431` | no key material in the HTML |
   | `DEMO_NOTICE` on 4 sampled paths | `:455` | the notice survives every route |

   `_UUID_ANY` requires dashes (`8-4-4-4-12`), so a dashless `acct-<hex>` in a `title=`
   attribute does not match. I checked this rather than assuming it, because §5.4 puts full
   ids in `title` attributes. **No copy string is asserted anywhere in `tests/` itself** —
   the coupling is entirely through `scenario_web_ui`, which is why it is invisible to
   anyone grepping the test directory. §5.5 was changed under this rule; see the note there.

   **Caveat added 2026-10-04 (measured):** of those three, only `"Send money"` and
   `"Unconfirmed transfers"` are *copy* claims. `"$100.00"` is **arithmetic-coupled** — it
   asserts the rendered balance, so the grant re-opening every account at 10,000 moves it.
   The same scenario holds `payer 9500` in-gate at `:511` and `:538`, both inside
   `scenario_web_ui`, and those rows are currently red for the grant while the copy rows are
   merely coupled to the same figure. So my anchors are real but **not quotable as green until
   #7's reconciliation settles** — a string can be a load-bearing copy decision and an
   arithmetic consequence at the same time, and that row is the second thing.

---

## 3. Design tokens

Emitted as custom properties on `:root`, with a dark override. **Every ratio below was
computed with the WCAG 2.x relative-luminance formula**, not estimated; the light and dark
tables are quoted in §3.4.

### 3.1 Colour

```css
:root {
  color-scheme: light dark;

  /* surfaces */
  --bg:            #f7f8fa;
  --surface:       #ffffff;
  --surface-2:     #eef2f7;
  --border:        #dfe4ec;   /* decorative separators only */
  --border-strong: #6b7280;   /* input/control boundaries — must clear 3:1 */

  /* text */
  --text:      #111827;
  --text-2:    #4b5563;
  --muted:     #5b6472;       /* on --bg; on --surface use #6b7280 */

  /* action */
  --accent:      #1d4ed8;
  --accent-text: #ffffff;
  --accent-dim:  #eff4ff;

  /* status: pending */
  --pending-bg:     #fff7ed;
  --pending-text:   #7c2d12;
  --pending-accent: #b45309;

  /* status: danger */
  --danger-bg:     #fef2f2;
  --danger-text:   #991b1b;
  --danger-accent: #b91c1c;

  /* status: success / credit */
  --ok-bg:     #f0fdf4;
  --ok-text:   #15803d;

  --focus: #1d4ed8;

  --radius-ctl: 6px;
  --radius-card: 10px;
  --radius-pill: 999px;
  --shadow-1: 0 1px 2px rgb(16 24 40 / 0.06), 0 1px 3px rgb(16 24 40 / 0.10);
}

@media (prefers-color-scheme: dark) {
  :root {
    --bg:            #0b0f16;
    --surface:       #131a24;
    --surface-2:     #18202c;
    --border:        #2a3646;
    --border-strong: #64748b;

    --text:   #e8edf5;
    --text-2: #b6c2d4;
    --muted:  #9aa7b8;

    --accent:      #2563eb;
    --accent-text: #ffffff;
    --accent-dim:  #16233a;

    --pending-bg:     #33260a;
    --pending-text:   #fcd34d;
    --pending-accent: #fbbf24;

    --danger-bg:     #3b1418;
    --danger-text:   #fecaca;
    --danger-accent: #fca5a5;

    --ok-bg:   #0f2a1a;
    --ok-text: #86efac;

    --focus: #93c5fd;
    --shadow-1: 0 1px 2px rgb(0 0 0 / 0.4);
  }
}
```

**`--border-strong: #64748b` is not arbitrary.** My first dark candidate (`#55657c`) measured
**2.95:1** against `--surface` `#131a24` — under the 3:1 that WCAG 1.4.11 requires of a
control boundary. It was changed to `#64748b` (**3.67:1**). Do not "tidy" this value back down.

### 3.2 Type

System stack only:

```css
--font-ui:   system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
--font-mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, "Liberation Mono", monospace;
```

| Token | Size | Line-height | Weight | Used for |
|---|---|---|---|---|
| `--t-display` | 2.5rem / 40px | 1.1 | 600 | the balance |
| `--t-h1` | 1.375rem / 22px | 1.25 | 600 | page title |
| `--t-h2` | 1.0625rem / 17px | 1.35 | 600 | card titles |
| `--t-body` | 1rem / 16px | 1.5 | 400 | body |
| `--t-small` | 0.875rem / 14px | 1.45 | 400 | helper, ids, timestamps |
| `--t-caption` | 0.75rem / 12px | 1.4 | 500 | labels, pills |

Never below 12px. **Every money value uses `font-variant-numeric: tabular-nums`** so digits
line up column-wise; the existing `td.amt` already does this (`app/web.py:89`) — keep it and
extend it to the balance.

### 3.3 Space, radius, motion

```
Space  (4px base): --s1 4  --s2 8  --s3 12  --s4 16  --s5 24  --s6 32  --s7 48
Radius: --radius-ctl 6px  --radius-card 10px  --radius-pill 999px
Motion: 120ms ease-out for colour/opacity, 180ms ease-out for transform. Nothing else.
```

```css
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { transition: none !important; animation: none !important; }
}
```

### 3.4 Contrast — measured, not asserted

Computed with the WCAG relative-luminance formula. Body text target 4.5:1; control
boundaries, focus rings and large text 3:1.

**Light**

| Pair | Ratio | Target | |
|---|---|---|---|
| body `#111827` on `--bg` | 16.69:1 | 4.5 | PASS |
| body `#111827` on `--surface` | 17.74:1 | 4.5 | PASS |
| secondary `#4b5563` on `--surface` | 7.56:1 | 4.5 | PASS |
| muted `#6b7280` on `--surface` | 4.83:1 | 4.5 | PASS |
| accent button `#ffffff` on `#1d4ed8` | 6.70:1 | 4.5 | PASS |
| danger `#b91c1c` on `--surface` | 6.47:1 | 4.5 | PASS |
| danger `#991b1b` on `--danger-bg` | 7.60:1 | 4.5 | PASS |
| pending `#7c2d12` on `--pending-bg` | 8.83:1 | 4.5 | PASS |
| pending accent `#b45309` on `--surface` | 5.02:1 | 3.0 | PASS |
| success `#15803d` on `--ok-bg` | 4.79:1 | 4.5 | PASS |
| credit `#15803d` on `--surface` | 5.02:1 | 4.5 | PASS |
| control border `#6b7280` on `--surface` | 4.83:1 | 3.0 | PASS |
| focus `#1d4ed8` on `--bg` | 6.31:1 | 3.0 | PASS |

**Dark**

| Pair | Ratio | Target | |
|---|---|---|---|
| body `#e8edf5` on `--bg` | 16.33:1 | 4.5 | PASS |
| body `#e8edf5` on `--surface` | 14.87:1 | 4.5 | PASS |
| secondary `#b6c2d4` on `--surface` | 9.71:1 | 4.5 | PASS |
| muted `#9aa7b8` on `--surface` | 7.16:1 | 4.5 | PASS |
| accent button `#ffffff` on `#2563eb` | 5.17:1 | 4.5 | PASS |
| danger `#fca5a5` on `--surface` | 9.21:1 | 4.5 | PASS |
| danger `#fecaca` on `--danger-bg` | 11.17:1 | 4.5 | PASS |
| pending `#fcd34d` on `--pending-bg` | 10.24:1 | 4.5 | PASS |
| success `#86efac` on `--ok-bg` | 10.95:1 | 4.5 | PASS |
| control border `#64748b` on `--surface` | 3.67:1 | 3.0 | PASS |
| focus `#93c5fd` on `--bg` | 10.64:1 | 3.0 | PASS |

---

## 4. Page shell and breakpoints

### Structure

```
<body>
  <p class="demo" role="note">…DEMO_NOTICE…</p>      ← first child, order unchanged (§2.3)
  <header class="topbar">                             ← brand + active-account chip
  <main class="shell">
      <section class="col-a">  Account card (§5.1–5.2) + Banners (§5.7)
      <section class="col-b">  Send card (§5.3)
      <section class="col-c">  Pending card (§5.5, only when non-empty)
      <section class="col-c">  Activity (§5.6)
      <section class="col-c">  Create account (§5.4, collapsed by default)
  <footer class="foot">                                  ← static, non-normative
```

```css
.shell { max-width: 64rem; margin: 0 auto; padding: var(--s4);
         display: grid; gap: var(--s5); grid-template-columns: 1fr; }
@media (min-width: 40rem) {          /* 640px — tablet */
  .shell { padding: var(--s5) var(--s5) var(--s7); }
  .topbar__inner, .shell { max-width: 44rem; margin-inline: auto; }
}
@media (min-width: 60rem) {          /* 960px — desktop */
  .shell { max-width: 64rem;
           grid-template-columns: minmax(20rem, 24rem) 1fr;
           align-items: start; }
  .col-a { grid-column: 1; grid-row: 1 / span 2; }
  .col-b { grid-column: 2; grid-row: 1; }
  .col-c { grid-column: 1 / -1; }
}
```

Bands: **phone <40rem** single column with 16px gutters; **tablet 40–60rem** single column,
44rem measure; **desktop ≥60rem** two columns — a sticky-feeling account rail on the left,
send beside it, pending and activity full width beneath.

The layout must survive **320px width at 200% zoom** with no horizontal scroll. Long account
ids and long counterparty labels wrap or truncate; nothing overflows its card.

---

## 5. Components

Each is specified in **every state it can reach**, including the ones nobody designs.

### 5.1 Account identity (the "clear account name")

This is the fix for finding #1, and it has an interface dependency: **the name is not on the
read path** (§1, finding #2). The component's contract is therefore defined as *input the
label, degrade honestly when it is absent*.

```
┌──────────────────────────────────────────────┐
│  Alice                          [ Active ]    │   ← name: --t-h1, --text
│  acct-3f9a…1c2  ⧉                             │   ← id: --t-small, --font-mono, --muted
│                                              │
│  $100.00                                      │   ← §5.2
└──────────────────────────────────────────────┘
```

| State | Treatment |
|---|---|
| name available | Primary label = the server's `owner_id` (or a future `display_name`), title-cased **only if** it is not an opaque token. |
| name absent (today's read path) | The heading becomes the neutral **"Account"** and the address carries the identification, with a quiet "This account has no name yet." line. A raw `acct-…` id is **never the largest thing on the page** (`plan.md` §C3.1), so the id is not promoted into the heading to fill the gap. **No invented name.** |
| id always | Shown as the secondary line in mono, truncated, with the **full id in a `title` attribute** and a copy button (`⧉`, `aria-label="Copy account ID"`, 44×44 target). The id is the capability, so it must stay reachable and copyable. |
| long name (>28ch) | Truncate with ellipsis in the primary line; full text in `title`. |
| unknown account (API 404) | The existing dedicated error document (`app/web.py:360-365`), restyled to §5.7. Never a blank card. |

**Title-casing rule.** `"alice"` → `"Alice"`. A 32-char hex string is **not** title-cased —
it is shown as an id. Do not prettify an opaque token into a fake name; that is the same
class of lie as inventing one.

**Superseded — the switcher is IN (`plan.md` §C3.3, §C4).** An earlier draft of this section
designed the switcher out, on the grounds that a list of "your accounts" needs client-side
persistence and the only client-side store is `ui-pending.json`, which must stay **one store
and account-agnostic**. The Planner has since ruled: the switcher ships, and its persistence
is a **separate** cookie, `pocketful_accounts` — a JSON list of **ids only**, capped at 12,
`Path=/`, `HttpOnly`, `SameSite=Lax`. That does not touch the pending store, so the
store-loss double-spend this section was protecting against is not engaged.

The visual requirements stand and are what §10 reviews against:

| Rule | Source |
|---|---|
| The switcher renders as a switch list; the active entry carries `aria-current="page"` | §C3.3 |
| **Ids only.** The cookie never holds a name or a balance; every name, balance and row comes from the server for the active account | §C3.3, §C3.4 |
| An id the server does not know renders as **unavailable** — never with an invented balance — and is dropped on the next write | §C3.4 |
| An empty list renders **nothing**, not an empty box | this spec |
| No new list endpoint; the switcher is the cookie, not an enumeration surface | §C3.3 |

Implemented as `account_switcher(*, accounts, active_id)` in `app/design.py`.

### 5.2 Balance

- Value from `format_minor(balance_minor, currency)` — integer minor units in, string out
  (`app/money.py:81-95`). The client never sums rows to produce it (`app/wallet.py:68-78`).
- `--t-display`, weight 600, tabular numerals, `--text`.
- **The currency symbol is the same size and weight as the digits** — only its colour is
  lighter. Shrinking or greying the symbol out of legibility is how `$100` and `100` start
  to look alike.
- Zero renders `$0.00` — never `$0`, never `0.00`.
- Negative renders `-$12.34` (sign before symbol, as `format_minor` already emits).
- Change of value never animates a number rolling; it is a state, not a slot machine.

### 5.3 Send money card

| Field | Spec |
|---|---|
| To account | Label **"To account ID"**. The input accepts an id, not a name — accounts are addressed by id only, so the label must not imply a name will resolve. Placeholder `acct-bob`. `autocomplete="off"`. |
| Amount | Label **"Amount (USD)"** — the currency comes from the active account, so it is stated, never implied. `inputmode="decimal"`, placeholder `12.34`. |
| Submit | Primary button "Send". |

| State | Treatment |
|---|---|
| default | `--border-strong` 1px input borders, `--radius-ctl`, 44px min height. |
| focus | 2px `--focus` ring, 2px offset — see §7. |
| submitting | Submit disabled, label stays "Send", no spinner theatre; the page navigates anyway (every POST answers 303 — `app/web.py:338-341`). |
| local reject (`invalid_amount`) | Banner §5.7 danger variant: *"That amount was not accepted. Use digits with at most two decimal places; nothing was sent."* Copy already exists at `app/web.py:257-258`; keep the wording, it states the no-loss guarantee. |
| same account | Danger banner, copy from `app/web.py:259`. |
| insufficient funds / unknown account / currency mismatch | Danger banner, existing copy at `app/web.py:270-272`. **Every refusal message keeps its "Nothing was sent." clause** — that clause is the reassurance, not padding. |

Helper line under the form, `--t-small`, `--muted`: *"Amounts are sent as whole minor units;
more than two decimal places is rejected, never rounded."* (Existing wording at
`app/web.py:217-218` is correct and stays.)

### 5.4 Create an account

The fix is prominence and honesty, not new machinery. The route and the API call already
exist (`app/web.py:504-535`).

Presentation: a card, **collapsed by default** behind a "Create an account" card button, so
it stops competing with Send. Expanded:

| Field | Label | Notes |
|---|---|---|
| Owner | **"Account name"** | Required. This is the value that will become the account's display label. Helper: *"Shown wherever this account appears."* |
| Currency | "Currency" | Default `USD`. |
| Account id | "Account ID (optional)" — under an **"Advanced"** disclosure | Today's helper text `blank, or an id you choose` is the confusing part. Replace with: *"Leave blank to get a random ID. If you choose one, it is how people will address this account — and anyone who knows it can act as this account."* That last clause is the `DEMO_NOTICE` model restated at the point of decision. |

| State | Treatment |
|---|---|
| default | Collapsed card with one primary action. |
| expanded | As above; owner field focused. |
| missing owner | Danger banner: *"Enter a name for the account you are creating."* (existing meaning at `app/web.py:276`). |
| id taken | Danger banner: *"That account ID is already taken. Nothing was created."* (`app/web.py:277`). |
| success | 303 → the new account becomes active (`app/web.py:535`). Land on §5.8. |

**Blocked copy: none.** This card's explanatory line is *"Creating an account mints no key and
moves no money."* — true today (`app/web.py:504-509`). An earlier revision added *"Do not ship
the welcome-bonus sentence before the ledger posts it."* That sentence no longer exists: §5.8
decision (c) renders **no** welcome copy on any path, so there is nothing blocked here and
nothing to release when the ledger posts. See §9 item A, which is closed by removal rather than
by unblocking.

### 5.5 Unconfirmed transfers — **the most important display in the product**

Pending is a state, not an absence. A transfer in flight must never read as settled.

```
┌─ Unconfirmed transfers ────────────────────── [ Pending ] ─┐
│  $2.50   acct-3f9a…1c2 → acct-77b1…9e0      2026-10-04 14:09 │
│  This transfer was sent but not confirmed.                  │
│  [ Retry ]                                                  │
└─────────────────────────────────────────────────────────────┘
```

**The heading is `Unconfirmed transfers` and it is not free copy.** This section first specified
`Needs attention`; I reverted that name on 2026-10-04 and changed `app/design.py` with it.
`app/selfcheck.py:491` asserts `"Unconfirmed transfers" in page` on the 303 redirect target, that
row is driven in-gate by `tests/test_persist_ordering.py:189`, and a component rendering anything
else reddens phase 1 — through a row whose *label* ("the redirect target shows the pending
transfer without its key") gives no hint that it is also pinning a word.

I reverted the artifact rather than the assertion, for two reasons. The row's subject is *a
pending transfer is visible after the redirect*, and a row with that subject deserves a stable
anchor more than this surface deserves a synonym. And the actual design point — **pending is
distinct, not a quieter row in the feed** — is carried by the `Pending` pill, which the row does
not touch. `Needs attention` was adding a third word for something the pill already said.

| Rule | Spec |
|---|---|
| Surface | `--pending-bg`, 1px `--pending-accent` left border 3px, `--radius-card`. |
| Pill | "Pending" — `--t-caption`, uppercase, `--pending-text`. **Not** colour-only: the word is present. |
| Heading | "Needs attention" (`--t-h2`). |
| Row content | Amount (`format_minor`, tabular) · **sender → payee**, both ids named · timestamp. |
| **Both ids must appear** | `app/selfcheck.py:698` asserts the rendered item contains `acct-sender` **and** `acct-payee`. On B's page the record is in A's store; naming only the payee would read as B's own transfer. Add a friendly name *beside* an id if one is available — never instead of it. |
| Retry control | **One button for the whole set.** `resume()` replays the entire store (`app/web.py:537-554`), so a per-row Retry would be a control that lies about its scope. Do not add per-row retry without a behaviour change. |
| Retry copy | Keep the existing sentence: *"Pressing Retry reuses the key already stored on the server — it cannot create a second transfer."* (`app/web.py:144-146`). This is the strongest reassurance in the UI; it stays. |
| Absent state | **No card at all** when nothing is pending. An empty "Needs attention" box would teach people to ignore it. |
| Retrying | Button disabled, label "Retrying…". |
| Retry failed | Danger banner §5.7 with the existing `error=protocol` copy (`app/web.py:261-263`) — it tells the user the record is below and nothing was sent twice. |

### 5.6 Activity

Replace the wire-fields table with a readable list, keeping `<table>` semantics (a screen
reader gets the column relationships for free).

| Column | From | Presentation |
|---|---|---|
| When | `created_at` | Humanised date, `title` = raw value. Monospace is not required; tabular alignment is. |
| What | `direction` | **Words, not the wire enum.** `debit` → "Sent to"; `credit` → "Received from". Never colour-only. |
| Who | `counterparty_account_id` | Truncated id, full value in `title`. If the counterparty is the **system/faucet account**, show the label the server supplies (§9 item B) — do not guess it client-side. |
| Amount | `amount_minor` | `−$2.50` for debit, `+$2.50` for credit, `--font-mono`-free but tabular, right-aligned. The sign is a character, not a colour. |

**Two rules about the amount cell, both learned the hard way on 2026-10-04.** First, **the sign
is suppressed when the amount is unavailable** — the module emits the placeholder alone, never
`+—`, because a direction glyph on a placeholder asserts a credit for a figure the module does
not hold. Second, **that suppression is a degradation, not a repair**: it removes a claim the
module cannot justify, it does not supply the missing figure. So supplying the amount is the
caller's obligation and stands on its own — otherwise a real balance renders as a bare `—`,
quieter and still wrong. Third, and this is why the fix is safe: **the `What` column carries
direction in words**, so a row with no sign is still readable. Do not drop `direction` from the
row on the assumption that the sign covers it — the sign is optional by construction now and
the words are not.

- `debit` amount colour `--text`; `credit` colour `--ok-text`. **Both also carry the explicit
  `+`/`−` glyph**, so colour is never the only channel.
- Keep `<caption class="sr-only">Activity</caption>` and `scope="col"` on headers.
- **Empty state** (§5.8) replaces `No activity yet.` in a cell with a real block.
- Keep the footer note: *"Balance and activity are the server's values, shown verbatim."*
  (`app/web.py:241`). It is true and it is load-bearing for trust.

### 5.7 Banners

Three meanings get three distinct treatments — the current build gives them one (finding #6).

| Banner | Surface | Icon | Role |
|---|---|---|---|
| Danger | `--danger-bg` / `--danger-text`, 3px `--danger-accent` left border | `!` | `role="alert"` |
| Notice / success | `--ok-bg` / `--ok-text` | `✓` | `role="status"` |
| Pending | §5.5 | `⚠` | `role="status"` |
| **Demo notice** | `--surface-2` / `--muted`, 3px `--border-strong` left border, `--t-small`, **first child of `<body>`** | none | `role="note"` |

The demo notice must become visually **quiet** — it is permanently true, so it must not
shout from the top of every page the way a fresh error does. Text, constant and position are
unchanged (§2.3); only the styling changes.

**Corrected 2026-10-04 — "quiet" was first written as invisible.** The rule as originally
specified (1px `--border` on a `--surface-2` panel) gave the notice no visible boundary at
all. Measured: panel vs page **1.06:1 light / 1.17:1 dark**; 1px border vs page **1.20:1 /
1.57:1** — both below the 3:1 non-text threshold (WCAG 1.4.11), in *both* themes. The text
passed AA (5.32:1 / 6.70:1), so the notice read as loose paragraphs on the background rather
than as a notice — for the one string on the page that carries a trust guarantee ("an account
id is the only credential", `app/web.py:93`). Quiet is not the same as absent: a permanently
true notice still has to be *an element*. It now takes the banner family's own shape — the
same `border-left: 3px solid` the danger and notice variants use — in the **neutral**
`--border-strong` rather than a meaning-coloured accent, which is what distinguishes it from
them: same affordance, no alarm. Measured on the new rule: border vs panel **4.30:1 light /
3.44:1 dark**, border vs page **4.55:1 / 4.03:1**, text unchanged. Landed in
`app/design.py`.

### 5.8 Empty, first-run, and the new-account state

**No activity, account exists** (replaces `app/web.py:119`):

> **No activity yet**
> Transfers you send and receive will appear here.
> Amounts are shown as the server records them.

Centred block, `--muted`, `--s` padding, inside the Activity card — not a table row.

**New account, grant is live** (the state the Frontend Engineer asked for):

> **$100.00**  ·  *Received from Pocketful  +$100.00* (the grant's own activity row)

Rendered as: balance at `--t-display`, and the grant appears in Activity as an ordinary
`credit` row. It must look like a normal ledger entry, because it *is* one — a real posting
from a named system account (`ledger/core.py:260-277` writes the transfer and both entries),
not a special-cased UI banner. **There is no welcome note at all** — decided 2026-10-04,
option (c), below. The row is the welcome.

**Corrected 2026-10-04 — the trigger was wrong, and it was wrong in a way that tells the
user something false.** This section used to say the welcome copy is *"driven by the server's
balance … if `balance_minor == 10000` because the grant posted, the first copy is correct"*.
That is an inference the display cannot support: **a balance of 10000 does not mean a grant
posted.** Anyone who receives exactly $100.00 from a friend has the same balance, and the
page would congratulate them on a welcome bonus they were never given. I built the rule to
avoid a client-side flag and reached for the wrong server fact instead.

The only evidence that a grant posted is **the create response**. `POST /accounts` returns
201 carrying `balance_minor = opening_grant_minor` for the account it just opened
(`api/app.py:333`), and the client is the one that issued that request — so it *knows*, once,
on the redirect that follows. So:

| Where | Rule |
|---|---|
| Create-success redirect | ~~The welcome note is shown from the **201 response's own balance**, once.~~ **Withdrawn — see the decision below.** |
| Any later `GET` of an account page | **No welcome copy, ever.** Show the balance and the activity rows. If the account's balance is 10000, it is 10000 — a number, not a provenance. |
| The activity row for the grant | Counterparty is the system account's owner, which `counterparty_owner_id` resolves to **`Pocketful`** (`ledger/core.py:61`, `SYSTEM_ACCOUNT_OWNER_ID`). The row reads `Received from Pocketful  +$100.00`. |

**DECISION 2026-10-04 — option (c): the grant's activity row *is* the welcome, and there is no
welcome note.** The Frontend Engineer raised the tension rather than papering over it, and it is
real: a one-shot note on the redirect requires either a marker in the URL, which survives a
reload and so contradicts rule 2, or server-side one-shot state that does not exist and is not
the client's to invent. Four constraints, three satisfiable. Dropping the note is the only
option that needs no new state, adds no coupling, and is **true on every render** rather than
true once. The user learns the grant where it actually exists — a ledger row that reads
`Received from Pocketful  +$100.00` — and **only after creating**. That is still better design
than a banner that explains money the page already shows.

**Correction 2026-10-04, caught by the Frontend Engineer: the "before creating" half of that
justification was fiction, and checking it turned up a second, harder reason to drop the idea.**
I had written that the user also learns it *before* creating, from the create form's own note
`New accounts start with $100.00.` Measured: `create_form` (`app/design.py:445-487`) renders a
name field, a currency field, the Advanced `<details>` and the submit button — **no grant note,
because I never wrote one.** I cited my own copy deck as though it were the artifact.

**The note must not be added, for a reason that is a falsification rather than a preference.**
The grant is **USD-only** — `ledger/core.py:190-196` refuses it for any other currency, and
`api/selfcheck.py:257-259` asserts that a non-USD account opens **ungranted, at 0** — while
`create_form` offers a **currency field defaulting to USD and freely editable**
(`app/design.py:472`). So a user who picks EUR reads "New accounts start with $100.00" and
receives nothing. The note would be a promise the client cannot verify, on a form that itself
offers the choice that breaks it. This is the third time in one afternoon that the honest
version of a rule is *say only what you can check*: the balance does not prove a grant, the
string does not prove its provenance, and a form note does not prove the form's own options.

**So under (c): no welcome note, before or after.** The activity row is the whole of the
welcome, and it is true on every render.

**Consequence for `WELCOME_BONUS_LABEL`:** under (c) it has **no reachable render path**.
`grep -rn "WELCOME_BONUS_LABEL" app/ tests/` returns only its definition. It is dead code of
exactly the `BALANCE_KEYS` kind this build spent a thread on, and it should be **deleted**. I am
*not* deleting it now: the planner has frozen the tree before review and the Frontend Engineer
has verified `app/design.py` at a digest, so moving the module for a cosmetic removal would
invalidate every peer's measurement for no functional gain. **Pending edit, recorded here so it
is not forgotten:** remove `WELCOME_BONUS_LABEL` at the next unfrozen window, and report it as a
reversal of a ratified addition rather than letting it disappear quietly.

**Do not key UI copy on `__system__`.** The client receives `counterparty_account_id =
"__system__"` and could special-case it into a nicer label, and I am ruling against it: the
reserved id is a ledger internal (`ledger/core.py:185-187` refuses it as a user account id),
and copy that depends on it breaks silently if the reserved name ever changes. The honest row
is the counterparty's name.

**LIVE RULING, 2026-10-04: (c). Nothing renders welcome copy, on any path.** This paragraph
used to end by sending the explanation of *why* the money is there to "the create-success path
where the client has real evidence", and to say `WELCOME_BONUS_LABEL` "has a home on that
path". **That was the pre-(c) revision and it survived my first pass at the rewrite, which left
§5.8 asserting both rulings at once — the Planner caught it and was right to treat it as a
blocking loose end rather than a stale paragraph.** There is no create-success note, because a
one-shot note is unsatisfiable without server state (below), and `WELCOME_BONUS_LABEL` has no
render path anywhere. The constant is to be **deleted**, not given a home.

**A second correction, from the same re-read: the empty-activity state can never show a grant.**
The grant posts a real transfer, so a freshly created account always has ≥1 activity row and
`activity_table` never falls through to `empty_state` for it. Any design that put the welcome
copy in the *empty* state would have been unreachable — the two states are mutually exclusive
by construction, not by care.

**New account, grant does not exist yet** (today):

> **$0.00**  ·  *This account has no funds yet. Send it money from another account.*

The two states are mutually exclusive **by the ledger, not by a rule I impose**: a granted
account has a grant row and no note; an ungranted one has a zero balance and the empty-activity
copy. Neither renders welcome copy, on any path — see the live ruling above. The instinct this
paragraph originally carried, that the welcome copy must never be keyed off a `?account=`
parameter, survives as the stronger statement: **no welcome copy exists to key off anything.**
The correction history matters here — the *instrument* (a balance threshold) was wrong, and
then the *feature* was withdrawn too, so the caution is now subsumed rather than restated.

**First-run (no account at all):** the current UI always has a boot account (`--account`,
`app/web.py:582`), so a true empty first-run is the API-404 document (§5.1). Restyle it to
the §5.7 danger pattern with a "Create an account" action as the primary path out.

---

## 6. Money display rules (correctness is attached to these)

1. **Input is an integer count of minor units. Output is a string. Nothing in between
   changes the value.** `format_minor` only (`app/money.py:81-95`).
2. **`$100` is stored as `10000`.** The grant, if it lands, is `10000` minor units —
   never `100.0`, never `100`, never a float. If any code path computes the grant with a
   decimal or float literal, that is a defect against INVARIANTS §1, not a design choice.
3. **No implied precision.** Always exactly two decimals. `$100.00`, never `$100.0`; and
   never a third decimal, ever.
4. **Thousands grouping is required above $999.99** for legibility, and it must be
   **integer-only string slicing** in `format_minor` — no `locale`, no float, no
   `Decimal`. Grouping must preserve `format_minor(1234) == "$12.34"` and
   `format_minor(-5) == "-$0.05"` (`app/selfcheck.py:737`). Implementer's call whether to
   land it in this pass; if deferred, say so and I will note it as an open item rather than
   silently missing.
5. **Currency is always shown.** No bare numbers for money, anywhere — including activity
   rows and the pending card.
6. **The client never derives a balance.** No summing of the rendered rows
   (`app/wallet.py:5-9`); the number on screen is the server's value verbatim.
7. **Display never rounds, and never formats a value it is about to send.** User text →
   `parse_amount_to_minor` → integer; display is a separate, one-way edge.

---

## 7. Accessibility budget

| Item | Requirement |
|---|---|
| Contrast | §3.4 — AA for body text (4.5:1) and control boundaries/focus (3:1), in **both** themes. |
| Focus | `:focus-visible { outline: 2px solid var(--focus); outline-offset: 2px; }` on every interactive element. Never `outline: none` without a replacement. |
| Hit targets | 44×44 CSS px minimum for buttons, the copy control and links that act. Inline text links are exempt. |
| Semantics | `<table>` retained for activity with `<caption>` and `scope="col"`; `role="alert"` on danger, `role="status"` on notices and pending; `role="note"` on the demo line. |
| Colour independence | Direction is carried by the words "Sent to"/"Received from" **and** the `+`/`−` glyph. Pending is carried by the word "Pending". Nothing means anything by colour alone. |
| Motion | `prefers-reduced-motion: reduce` removes all transitions (§3.3). |
| Zoom / reflow | Usable at 320px at 200% zoom; no horizontal scrolling; no clipped text. |
| Labels | Every input has a `<label>` wrapping or `for`-bound. The create form's "Advanced" disclosure is a real `<details>`/`<summary>`, keyboard operable. |
| Live regions | The banner container is `aria-live="polite"` for status and `assertive` semantics via `role="alert"` for errors. |

---

## 8. Copy deck

All strings LTR, sentence case, no exclamation marks, no "Oops".

| Slot | String |
|---|---|
| Brand | `Pocketful` |
| Account id label | `Account ID` |
| Copy button | `Copy account ID` (aria-label) |
| To field | `To account ID` |
| Amount field | `Amount (USD)` |
| Send heading | `Send money` — **in-gate string**, `app/selfcheck.py:428`; do not reword |
| Send button | `Send` |
| Amount helper | `Amounts are sent as whole minor units; more than two decimal places is rejected, never rounded.` |
| Retry button | `Retry` |
| Retrying | `Retrying…` |
| Pending heading | `Unconfirmed transfers` — **in-gate string**, `app/selfcheck.py:491`; do not reword |
| Pending pill | `Pending` |
| Pending note | `This transfer was sent but not confirmed.` |
| Retry guarantee | `Pressing Retry reuses the key already stored on the server — it cannot create a second transfer.` |
| Create heading | `Create an account` |
| Owner field | `Account name` · helper `Shown wherever this account appears.` |
| Account id (advanced) | `Leave blank to get a random ID. If you choose one, it is how people will address this account — and anyone who knows it can act as this account.` |
| Create button | `Create account` |
| Create note — **grant absent** | `Creating an account mints no key and moves no money.` |
| Create note — **grant live** | *(withdrawn 2026-10-04 — §5.8: the grant is USD-only while the form lets the user pick a currency, so any grant note is false for a non-USD choice. The activity row is the welcome.)* |
| Empty activity heading | `No activity yet` |
| Empty activity body | `Transfers you send and receive will appear here.` |
| New account — grant live | *(no note — the grant's activity row is the welcome, §5.8 decision (c))* |
| New account — grant absent | `This account has no funds yet. Send it money from another account.` |
| Activity footer | `Balance and activity are the server's values, shown verbatim.` |
| Demo notice | unchanged constant `DEMO_NOTICE` (`app/web.py:109-110`) — not mine to reword |

---

## 9. Blocked, and needing a decision elsewhere

**A. The `$100` copy is blocked on the ledger.** **RESOLVED 2026-10-04 — and the resolution
removed the copy rather than unblocking it: §5.8 decision (c) rules that there is no welcome
note at all, so this item has no blocked sentence left to ship.**

*Kept as the record of what was required and why it was refused:* the sentence "New accounts
start with $100.00." may not ship until the grant is *posted and observable*, because the copy
would describe behaviour that does not exist. The Frontend Engineer correctly refused to fake
it client-side. Required server-side, by the Ledger Engineer / Integrator, with the Planner
sequencing — **the first two landed; the third did not:**
- a **named system/faucet account** and a **balanced posting** (`−10000` system, `+10000`
  new account) so I1/I2 still hold (INVARIANTS §0/§2);
- the grant written **in the same transaction as account creation** (§3 of INVARIANTS);
- an **idempotency story for `POST /accounts`**. This is the one I want in writing before
  the UI says anything: that route currently mints no key and moves no money by design
  (`app/web.py:504-509`, and `api/app.py:290-294` says it is *deliberately not idempotent*).
  A retried create that grants is **repeatable free money**. Either creation gains a
  key, or the grant is a separate idempotent operation. Not my call — but it must not be
  nobody's. **Status: NOT landed.** The plan ruled (§C2.4) that creation is deliberately not
  replay-keyed, and the client-side half of the answer is #9, owned by the Frontend Engineer.
  I am not calling this satisfied — the row above exists because a retried create that grants
  is repeatable free money, and until #9 lands that is a live question, not a closed one.
  *(I had briefly written "all three landed" here. Two had. Checking before publishing is the
  whole point of this document; leaving the sentence would have made this the fourth thing
  today that was true when taken and false when read.)*

**A2. "Every new account gets $100" is true in USD and false in every other currency — a
product-scope collision, not a UI defect.** Raised by the Frontend Engineer, 2026-10-04. The
grant is USD-only (`ledger/core.py:190-196`; `SYSTEM_ACCOUNT_CURRENCY = "USD"` at `:66`) while
`create_form` offers a free-text currency field defaulting to USD (`app/design.py:470-473`), so
a user can create a EUR account one click from the form and receive nothing.

**My ruling on the UI: option (a) — accept it, change nothing in the form.** Three reasons.
First, decision (c) already removed the only place the client asserted anything about the grant,
so there is no sentence in the product left to be false. Second, restricting the currency field
(option (b)) would delete a real capability — a non-USD account opens fine and holds transfers,
which §C2.5 and INVARIANTS §1 both support — in order to prop up a claim nobody is making any
more; that is the tail wagging the dog, and it is the client asserting a rule it can only
reimplement rather than know. Third, whether the *product* grants in every currency is option
(c), and it belongs to the ledger: a currency-general grant needs a system account per currency,
which `SYSTEM_ACCOUNT_CURRENCY` does not have. **So the client asserts nothing, the ledger does
what it does, and the user discovers the grant by seeing it in their activity — which is the
honest version of the owner's ask that the UI can deliver.** If the owner wants the literal
"every account", that is a ledger item, and the UI consequence would still be *no* note rather
than a different one.

*(Separate observation, not part of this decision: a free-text currency field with no validation
accepts `usd`, `US$` and `Euro` equally. That is a real design smell, but it predates the
redesign and is not mine to change mid-freeze.)*

**B. The name is not on the read path — and exposing it is a contract amendment, not a free
win** (§1 finding #2). **RESOLVED 2026-10-04 — §C2.5 landed: the owner is on the read path
(`api/app.py:350`) and beside the counterparty id (`api/app.py:161`).** What follows is the
pre-amendment analysis and the withdrawal of my "improves for free" claim, kept because it is
why §5.1 was built to degrade. Bullets 1-2 are the Integrator's corrections; bullet 3 is my
own observation and is labelled as such:

- `owner_id` **is** already on `POST /accounts` (`api/app.py:118`; the in-gate row
  `api/selfcheck.py:154` asserts the 201 body by exact set against `ACCOUNT_KEYS`, which
  includes it). The gap is the **balance read path only**.
- `GET /accounts/<id>/balance` is pinned twice: `plan.md:423` fixes the 200 body to
  `{"account_id", "currency", "balance_minor"}`, and `api/selfcheck.py:196-198` asserts that
  body by **exact equality**. `api/app.py:110-112` states the rule as "no extra keys, no
  missing ones". So adding `owner_id` moves the plan contract *and* that gate row in the same
  change. My earlier phrase "it improves for free" was wrong; this is the amendment `plan.md`
  §C2.5 now carries.
- **Freshly corrected, and it corrects me twice.** An earlier revision of this bullet read
  "`api/selfcheck.py:46` defines `BALANCE_KEYS` and **nothing references it**" and carried it
  under the heading "Corrected by the Integrator". Both halves were wrong. **The observation is
  mine, not the Integrator's** — I derived it by reading `api/selfcheck.py:42-48` directly; the
  Planner has confirmed they did not make it, and the transcript shows no band message
  containing it. The attribution was a compaction artifact and is withdrawn, because a
  false attribution in a spec is the same shape as the false premises this build keeps
  catching. **And the fact is stale:** `BALANCE_KEYS` *was* defined and unreferenced, and the
  §C2.5 amendment has since landed — `api/selfcheck.py:262` now asserts
  `set(body) == BALANCE_KEYS`, and the constant's own comment at `:46-48` records the history
  ("defined and never referenced — the pin was the literal dict in the balance row … It is
  referenced now").
- **The lesson that survives, and it is the one worth keeping:** the balance body was pinned
  *twice* — by a literal dict in one row and by an unreferenced constant next to it — and only
  the literal governed. Widening the body would have satisfied `BALANCE_KEYS` and still failed
  the row actually asserted. That is the same defect class as the assertion-label sweep in
  §2 item 8: **the thing that looks like the rule is not always the thing that enforces it.**
  My §9B re-verification above is the current state (`plan.md:423` + `api/selfcheck.py:262`),
  not the pre-amendment one.
- There is **no name field anywhere**: `accounts(account_id, owner_id, currency,
  allow_overdraft, version, created_at)` (`ledger/schema.py:20-22`) and `Account`
  (`ledger/types.py:60-66`) carry `owner_id` only. "Clear account names" therefore means the
  client *labels* with `owner_id`, unless a ledger column is added — a Ledger Engineer
  decision, not something the client invents.

Until the amendment lands, §5.1 degrades to the address as specified — no invented names, and
no client-side lookup table, which would be a second source of truth about identity.

**C. Identifying the grant's counterparty (B + A).** **RESOLVED 2026-10-04 — and resolved
without the flag this item asked for.** It read: *"for §5.8's Activity row to read 'Welcome
bonus' rather than a raw system id, the server must mark that entry (a flag, or a documented
system account id). Do not pattern-match the id client-side."* The row does not read "Welcome
bonus" any more — decision (c) means the label *is* the counterparty's name — and that name
arrives on the ordinary read path with no flag and no client-side id matching:
`counterparty_owner_id` is resolved in the same read (`ledger/core.py:434`), carried on the
wire (`api/app.py:161`), and typed at `ledger/types.py:121`, where it resolves to **Pocketful**
(`SYSTEM_ACCOUNT_OWNER_ID`, `ledger/core.py:61`). So the requirement was met by the layer that
owns the fact, and the warning in the last sentence is satisfied trivially: the client matches
no id at all.

**D. Grouping in `format_minor`** (§6.4) — my recommendation, the implementer's decision,
pinned by the existing assertion at `app/selfcheck.py:737`.

**E. Account switcher** — **resolved: it ships.** Designed out in an earlier draft; the
Planner ruled in `plan.md` §C3.3 (cookie `pocketful_accounts`, ids only, capped 12) and
`§C4` (`account_switcher`). See §5.1 for the reconciled requirements. This section is kept,
rather than deleted, so the reversal is on the record rather than silently overwritten.

---

## 10. Review checklist (what I will measure against)

When the build is running I will compare the rendered product to this document and report
against these, quoting what I measured rather than impressions:

1. `/` renders and every §5 component is present in its default state.
2. Contrast spot-checked on the rendered page for `--text`, `--muted`, buttons and banners,
   in both `prefers-color-scheme` values.
3. Keyboard-only pass: tab order reaches send, create, retry, copy; focus ring visible at
   every stop; no focus trap.
4. Each specified state is *reachable*: empty activity, pending present, danger banner,
   unknown account, create-collapsed and create-expanded.
5. **Gate-covered copy survives** (§2 item 8, verified in-gate via
   `tests/test_persist_ordering.py:189`): `"Send money"`, `"$100.00"` and `"Unconfirmed
   transfers"` are all still present on the pages that assert them, and no key material leaks
   into the HTML. I will run the phase-1 gate, not just read the code.
6. `app/selfcheck.py:696-712` and `:737` still pass — the pending item names both accounts;
   `format_minor(1234)`/`(-5)` unchanged. *(Not gate-covered — that file is nobody's phase —
   so these are mine to check by running it, not to assume green.)*
7. `DEMO_NOTICE` still present, still first child of `<body>`, still unstyled-loud.
8. 320px at 200%: no horizontal scroll.
9. No float, no `locale`, no invented precision anywhere on the display path.
