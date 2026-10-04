"""Pocketful's visual system as code — colour, type, space, radius and the
components built from them.

Owner: **designer** (plan §C4). Consumed by ``app/web.py``, which keeps the
routes, the PRG flow, ``DEMO_NOTICE``, ``_document`` and the pending store. This
module owns none of those; it is pure rendering, so it can be read, tested and
byte-compiled without a server, a socket or a database.

The rules this module is built to keep:

* **Pure functions.** No routes, no ledger, no network, no file reads, no clock.
  Every function takes values and returns a string.
* **One escaper.** :func:`_esc` is the application's only escaper, exported so
  ``app/web.py`` uses this one instead of growing a second. Every interpolated
  value below passes through it, including values that look safe — "looks safe"
  is how escaping gets skipped.
* **An address is never invented and never hidden.** A person *reads* a name and
  *pays* an address (plan §C3.1). Where a name is unavailable the component says
  less rather than inventing more, and a raw ``acct-…`` id is never the largest
  thing on the page.
* **Money arrives as text.** This module performs no arithmetic on money and
  formats none on its own account: the caller passes a display string produced
  by :func:`app.money.format_minor` from integer minor units. There is no
  arithmetic here to round and no float to drift. The single exception is the
  documented fallback in :func:`_amount_display`, which is still integer-only.
* **No JavaScript** (plan §C3.5). Every affordance here works with a keyboard,
  a screen reader and no script: ``<details>`` for the advanced field,
  ``user-select: all`` instead of a clipboard API, ``aria-current`` instead of a
  client-side active class.
* **Colour is never the only channel.** Direction carries a ``+``/``−`` glyph
  *and* the words "Sent to"/"Received from"; pending carries the word "Pending".

``DESIGN-SPEC.md`` is the rationale and the measured record (contrast ratios,
breakpoints, the state tables). This module is the artifact. Where the two
disagree, that is a finding for the reviewer, not a silent win for either.
"""

from __future__ import annotations

import html
from urllib.parse import quote

from .money import format_minor

# -- tokens --------------------------------------------------------------------
#
# The one place a colour, a space or a radius is written. Keys are the CSS
# custom-property names, so the table *is* the stylesheet's variable block rather
# than a description of it. ``TOKENS_DARK`` is an additive second table (plan §C4
# fixes ``TOKENS``; it does not forbid a companion) holding only the values that
# change under ``prefers-color-scheme: dark``.
#
# Every foreground/background pair below was measured with the WCAG 2.x
# relative-luminance formula; the ratios are recorded in DESIGN-SPEC.md §3.4.
# One value is load-bearing and must not be "tidied": dark ``--border-strong`` is
# #64748b because the first candidate measured 2.95:1 against ``--surface``,
# under the 3:1 WCAG 1.4.11 requires of a control boundary.

TOKENS: dict[str, str] = {
    # type
    "--font-ui": ('system-ui, -apple-system, "Segoe UI", Roboto, '
                  '"Helvetica Neue", Arial, sans-serif'),
    "--font-mono": ('ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, '
                    '"Liberation Mono", monospace'),
    "--t-display": "2.5rem",
    "--t-h1": "1.375rem",
    "--t-h2": "1.0625rem",
    "--t-body": "1rem",
    "--t-small": "0.875rem",
    "--t-caption": "0.75rem",
    # space (4px base)
    "--s1": "0.25rem",
    "--s2": "0.5rem",
    "--s3": "0.75rem",
    "--s4": "1rem",
    "--s5": "1.5rem",
    "--s6": "2rem",
    "--s7": "3rem",
    # radius / motion / elevation
    "--radius-ctl": "6px",
    "--radius-card": "10px",
    "--radius-pill": "999px",
    "--motion-fast": "120ms",
    "--motion-slow": "180ms",
    "--shadow-1": "0 1px 2px rgb(16 24 40 / 0.06), 0 1px 3px rgb(16 24 40 / 0.10)",
    # surfaces
    "--bg": "#f7f8fa",
    "--surface": "#ffffff",
    "--surface-2": "#eef2f7",
    "--border": "#dfe4ec",
    "--border-strong": "#6b7280",
    # text
    "--text": "#111827",
    "--text-2": "#4b5563",
    "--muted": "#5b6472",
    # action
    "--accent": "#1d4ed8",
    "--accent-text": "#ffffff",
    "--accent-dim": "#eff4ff",
    # status: pending
    "--pending-bg": "#fff7ed",
    "--pending-text": "#7c2d12",
    "--pending-accent": "#b45309",
    # status: danger
    "--danger-bg": "#fef2f2",
    "--danger-text": "#991b1b",
    "--danger-accent": "#b91c1c",
    # status: success / credit
    "--ok-bg": "#f0fdf4",
    "--ok-text": "#15803d",
    "--focus": "#1d4ed8",
}

TOKENS_DARK: dict[str, str] = {
    "--shadow-1": "0 1px 2px rgb(0 0 0 / 0.4)",
    "--bg": "#0b0f16",
    "--surface": "#131a24",
    "--surface-2": "#18202c",
    "--border": "#2a3646",
    "--border-strong": "#64748b",
    "--text": "#e8edf5",
    "--text-2": "#b6c2d4",
    "--muted": "#9aa7b8",
    "--accent": "#2563eb",
    "--accent-text": "#ffffff",
    "--accent-dim": "#16233a",
    "--pending-bg": "#33260a",
    "--pending-text": "#fcd34d",
    "--pending-accent": "#fbbf24",
    "--danger-bg": "#3b1418",
    "--danger-text": "#fecaca",
    "--danger-accent": "#fca5a5",
    "--ok-bg": "#0f2a1a",
    "--ok-text": "#86efac",
    "--focus": "#93c5fd",
}

# REMOVED 2026-10-04: WELCOME_BONUS_LABEL = "Welcome bonus".
#
# It was added as a single home for the opening grant's row label, gated on the
# grant posting. Decision (c) in DESIGN-SPEC.md §5.8 retired the whole feature:
# the grant's row is labelled with the counterparty's name, which resolves to
# SYSTEM_ACCOUNT_OWNER_ID ("Pocketful") through the ordinary read path
# (`counterparty_owner_id`), so there is no separate welcome label to render —
# on the create-success path or anywhere else. The constant had no render path
# and was dead code of the same kind as the unreferenced BALANCE_KEYS this build
# spent a thread on. Deleted at the Planner's sequencing, before the T25 review
# anchors on this file, so the deletion did not invalidate a review already done.

# Empty-state and helper copy, likewise single-sourced.
NO_ACTIVITY_MESSAGE = "Transfers you send and receive will appear here."

# The amount cell's placeholder. Named because two places depend on the same
# value: ``_amount_display`` returns it, and ``activity_table`` suppresses the
# direction sign when it sees it — "+—" would assert a credit for a figure the
# module does not have (2026-10-04, found by the Frontend Engineer).
_AMOUNT_UNAVAILABLE = "—"


def _root_block(tokens: dict[str, str]) -> str:
    lines = "\n".join(f"  {name}: {value};" for name, value in tokens.items())
    return f":root {{\n{lines}\n}}"


_COMPONENT_CSS = """
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { transition: none !important; animation: none !important; }
}
*, *::before, *::after { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text);
       font: 400 var(--t-body)/1.5 var(--font-ui); }
h1, h2, p, ul, ol, table { margin: 0; }
a { color: var(--accent); }
:focus-visible { outline: 2px solid var(--focus); outline-offset: 2px; }
.vh { position: absolute; width: 1px; height: 1px; overflow: hidden;
      clip-path: inset(50%); white-space: nowrap; }

/* shell — the classes app/web.py composes the page from (DESIGN-SPEC.md §4) */
.topbar { border-bottom: 1px solid var(--border); background: var(--surface); }
.topbar__inner { max-width: 64rem; margin: 0 auto; padding: var(--s3) var(--s4);
                 display: flex; gap: var(--s4); align-items: center;
                 justify-content: space-between; flex-wrap: wrap; }
.brand { font-size: var(--t-h1); font-weight: 600; }
.shell { max-width: 64rem; margin: 0 auto; padding: var(--s4);
         display: grid; gap: var(--s5); grid-template-columns: 1fr; }
.foot { max-width: 64rem; margin: 0 auto; padding: var(--s5) var(--s4) var(--s7);
        color: var(--muted); font-size: var(--t-small); }
@media (min-width: 40rem) {
  .topbar__inner, .shell, .foot { max-width: 44rem; }
  .shell { padding: var(--s5) var(--s5) var(--s7); }
}
@media (min-width: 60rem) {
  .topbar__inner, .shell, .foot { max-width: 64rem; }
  .shell { grid-template-columns: minmax(20rem, 24rem) 1fr; align-items: start; }
  .card--account { grid-column: 1; grid-row: 1 / span 2; align-self: stretch; }
  .card--send { grid-column: 2; grid-row: 1; }
  .card--wide { grid-column: 1 / -1; }
}

/* cards */
.card { background: var(--surface); border: 1px solid var(--border);
        border-radius: var(--radius-card); padding: var(--s4);
        box-shadow: var(--shadow-1); display: grid; gap: var(--s3); }
.h1 { font-size: var(--t-h1); font-weight: 600; }
.h2 { font-size: var(--t-h2); font-weight: 600; }
.lede { color: var(--text-2); font-size: var(--t-small); }
.hint { color: var(--muted); font-size: var(--t-small); }

/* account identity */
.addr-row { display: flex; gap: var(--s2); align-items: baseline; flex-wrap: wrap; }
.lbl { color: var(--muted); font-size: var(--t-caption); text-transform: uppercase;
       letter-spacing: 0.04em; }
.addr { font-family: var(--font-mono); font-size: var(--t-small);
        color: var(--text-2); user-select: all; word-break: break-all; }
.balance { font-size: var(--t-display); font-weight: 600; line-height: 1.1;
           font-variant-numeric: tabular-nums; }
.cur { color: var(--text-2); font-weight: 500; }

/* controls */
.btn { min-height: 44px; padding: var(--s2) var(--s4); font: inherit;
       border-radius: var(--radius-ctl); border: 1px solid var(--border-strong);
       background: var(--surface); color: var(--text); cursor: pointer; }
.btn:disabled { cursor: not-allowed; opacity: 0.6; }
.btn-primary { background: var(--accent); border-color: var(--accent);
               color: var(--accent-text); font-weight: 600; }
.field { display: flex; flex-direction: column; gap: var(--s1);
         font-size: var(--t-small); }
.field > span:first-child { color: var(--text-2); font-weight: 500; }
input { min-height: 44px; padding: var(--s2); font: inherit; color: var(--text);
        background: var(--surface); border: 1px solid var(--border-strong);
        border-radius: var(--radius-ctl); }
.form { display: grid; gap: var(--s3); }
.form-row { display: flex; gap: var(--s3); flex-wrap: wrap; align-items: end; }
.form-row .field { flex: 1 1 12rem; }
.adv { border-top: 1px solid var(--border); padding-top: var(--s3); }
.adv summary { cursor: pointer; color: var(--text-2); font-size: var(--t-small);
               min-height: 44px; display: flex; align-items: center; }
.adv[open] summary { margin-bottom: var(--s3); }

/* banners — three meanings, three treatments */
.banner { display: flex; gap: var(--s2); padding: var(--s3) var(--s4);
          border-radius: var(--radius-ctl); border-left: 3px solid;
          font-size: var(--t-small); }
.banner__icon { font-weight: 700; }
.banner--error { background: var(--danger-bg); color: var(--danger-text);
                 border-left-color: var(--danger-accent); }
.banner--notice { background: var(--ok-bg); color: var(--ok-text);
                  border-left-color: var(--ok-text); }

/* the demo notice (DESIGN-SPEC.md §5.7). It was specified and never written into
   STYLE, so `<p class="demo" role="note">` (app/web.py:133) rendered unstyled while
   every other banner had a rule -- the one permanently-true, trust-bearing notice on
   the page was the one with no treatment. Quiet by design: it is true on every page,
   so it must not shout like a fresh error. Undo: this rule is the only `.demo` in
   the sheet, so deleting it restores the unstyled state exactly. */
.demo { background: var(--surface-2); color: var(--muted);
        border-left: 3px solid var(--border-strong); border-radius: var(--radius-ctl);
        padding: var(--s3) var(--s4); margin: 0 0 var(--s4);
        font-size: var(--t-small); }

/* pending — the loudest thing on the page */
.card--pending { background: var(--pending-bg); border-color: var(--pending-accent);
                 border-left-width: 3px; }
.card--pending .h2 { color: var(--pending-text); }
.pill { display: inline-block; padding: 2px var(--s2); border-radius: var(--radius-pill);
        font-size: var(--t-caption); font-weight: 600; text-transform: uppercase;
        letter-spacing: 0.04em; background: var(--pending-bg);
        color: var(--pending-text); border: 1px solid var(--pending-accent); }
.pending-list { list-style: none; padding: 0; display: grid; gap: var(--s3); }
.pending-list li { display: flex; gap: var(--s3); flex-wrap: wrap;
                   align-items: baseline; color: var(--pending-text); }
.pending-list .amt { font-variant-numeric: tabular-nums; font-weight: 600; }
.pending-list .who { font-family: var(--font-mono); font-size: var(--t-small);
                     word-break: break-all; }

/* switcher */
.switcher { list-style: none; padding: 0; display: flex; gap: var(--s2);
            flex-wrap: wrap; }
.switcher a, .switcher span { display: inline-flex; align-items: center;
            min-height: 44px; padding: var(--s1) var(--s3);
            border-radius: var(--radius-pill); border: 1px solid var(--border);
            font-size: var(--t-small); text-decoration: none; color: var(--text-2); }
.switcher [aria-current="page"] { border-color: var(--accent);
            background: var(--accent-dim); color: var(--accent); font-weight: 600; }
.switcher .unavailable { color: var(--muted); border-style: dashed; }

/* activity */
.activity { width: 100%; border-collapse: collapse; }
.activity th, .activity td { text-align: left; padding: var(--s2) var(--s3);
            border-bottom: 1px solid var(--border); vertical-align: baseline; }
.activity th { color: var(--muted); font-size: var(--t-caption);
            text-transform: uppercase; letter-spacing: 0.04em; font-weight: 500; }
.activity .amt { text-align: right; font-variant-numeric: tabular-nums;
            white-space: nowrap; }
.activity .who { font-family: var(--font-mono); font-size: var(--t-small);
            color: var(--text-2); word-break: break-all; }
.activity .dir { white-space: nowrap; }
.activity .credit { color: var(--ok-text); }

/* empty state */
.empty { display: grid; gap: var(--s1); padding: var(--s5) var(--s4);
         text-align: center; color: var(--muted); }
.empty__title { color: var(--text); font-weight: 600; }
"""

STYLE: str = (
    _root_block(TOKENS)
    + "\n@media (prefers-color-scheme: dark) {\n"
    + _root_block(TOKENS_DARK)
    + "\n}"
    + _COMPONENT_CSS
)


# -- escaping ------------------------------------------------------------------


def _esc(value: object) -> str:
    """The application's only escaper. ``app/web.py`` imports this one.

    ``quote=True`` so a value cannot break out of an attribute it was
    interpolated into, and it is applied to *every* interpolation below rather
    than to the ones that look dangerous.
    """
    return html.escape(str(value), quote=True)


# -- small helpers -------------------------------------------------------------


def _text(value: object) -> str:
    return "" if value is None else str(value)


def _short(value: object, head: int = 8, tail: int = 4) -> str:
    """Truncate an opaque id for a list. Pure slicing — no arithmetic on money."""
    text = _text(value)
    if len(text) <= head + tail + 1:
        return text
    return text[:head] + "…" + text[-tail:]


def _field(row: object, name: str, default: object = None) -> object:
    """Read ``name`` from a mapping or an object. Lets callers pass the dataclass
    they already have instead of converting it, without this module importing it."""
    if isinstance(row, dict):
        return row[name] if name in row else default
    return getattr(row, name, default)


def _amount_display(row: object) -> str:
    """The row's amount, already formatted by the caller where possible.

    Falls back to :func:`format_minor` with the row's own currency, which is
    integer ``divmod`` formatting and not arithmetic on a balance. Where neither
    is available the cell says so rather than showing a number it made up.
    """
    display = _field(row, "amount_display")
    if isinstance(display, str) and display:
        return display
    minor = _field(row, "amount_minor")
    currency = _field(row, "currency")
    if type(minor) is int and isinstance(currency, str) and currency:
        return format_minor(minor, currency)
    return _AMOUNT_UNAVAILABLE


# -- components ----------------------------------------------------------------


def banner(*, kind: str, message: str) -> str:
    """A status message. ``kind`` is ``"error"`` or ``"notice"``.

    Two kinds, two roles: an error interrupts (``role="alert"``), a notice waits
    its turn (``role="status"``). An unknown kind raises rather than guessing —
    a banner that silently renders with the wrong urgency is a lie about
    whether money moved.
    """
    if kind == "error":
        role, icon, css = "alert", "!", "banner--error"
    elif kind == "notice":
        role, icon, css = "status", "✓", "banner--notice"
    else:
        raise ValueError(f"unknown banner kind {kind!r}; expected 'error' or 'notice'")
    return (
        f'<p class="banner {css}" role="{role}">'
        f'<span class="banner__icon" aria-hidden="true">{icon}</span>'
        f"<span>{_esc(message)}</span></p>"
    )


def wallet_header(*, name: object, address: object, balance_display: object) -> str:
    """The account card: what a person reads, what a payer types, and the balance.

    The heading is the **name**. Where no name is available the heading is the
    neutral "Account" and the address carries the identification — a raw
    ``acct-…`` id is never the largest thing on the page (plan §C3.1). The
    address is shown whole rather than truncated, because it is the value someone
    has to give out, and ``user-select: all`` makes one click select it without
    JavaScript (plan §C3.5).
    """
    address_text = _text(address).strip()
    display_name = _text(name).strip()
    heading = display_name or "Account"
    body = [
        f'<section class="card card--account" aria-labelledby="acct-h">',
        f'<h1 class="h1" id="acct-h">{_esc(heading)}</h1>',
    ]
    if not display_name:
        body.append('<p class="hint">This account has no name yet.</p>')
    body.append(
        '<p class="addr-row"><span class="lbl">Account ID</span>'
        f'<code class="addr" title="Account ID">{_esc(address_text)}</code></p>'
    )
    body.append(
        f'<p class="balance"><span class="cur" aria-hidden="true"></span>'
        f"{_esc(balance_display)}</p>"
    )
    body.append("</section>")
    return "".join(body)


def account_switcher(*, accounts: object, active_id: object) -> str:
    """The switch list: the ids the browser remembers, and nothing more.

    Entries are mappings (or objects) with ``account_id``, an optional ``name``/
    ``owner_id``, and an optional ``available`` flag. The cookie is not a source
    of truth (plan §C3.4): an id the server does not know renders as
    **unavailable** — never with an invented balance — and is dropped on the next
    write. An empty list renders nothing at all, because an empty switcher is a
    box that teaches people to ignore boxes.
    """
    entries = list(accounts or [])  # type: ignore[arg-type]
    items = []
    for entry in entries:
        account_id = _text(_field(entry, "account_id")).strip()
        if not account_id:
            continue
        label = _text(_field(entry, "name") or _field(entry, "owner_id")).strip()
        shown = label or _short(account_id)
        available = _field(entry, "available", True) is not False
        if not available:
            items.append(
                '<li><span class="unavailable" '
                f'title="This browser remembers this account, but the server does '
                f'not know it. It will be dropped.">{_esc(shown)} — '
                "unavailable</span></li>"
            )
        elif account_id == _text(active_id):
            items.append(
                f'<li><a href="?account={_esc(quote(account_id, safe=""))}" '
                f'aria-current="page">{_esc(shown)}</a></li>'
            )
        else:
            items.append(
                f'<li><a href="?account={_esc(quote(account_id, safe=""))}" '
                f'title="{_esc(account_id)}">{_esc(shown)}</a></li>'
            )
    if not items:
        return ""
    return (
        '<nav class="card card--wide" aria-label="Your accounts">'
        '<h2 class="h2">Your accounts</h2>'
        f'<ul class="switcher">{"".join(items)}</ul></nav>'
    )


def create_form(*, action: str, name_value: object, address_value: object,
                error: object = None) -> str:
    """Creation asks for a **name**, and optionally an **address** (plan §C3.2).

    The address lives behind a ``<details>`` — it is the advanced case, and
    putting it beside the name at equal weight is what made the old form
    confusing. The disclosure opens itself when an address is present, so a 409
    shows the user the field that has to change. Values are echoed back, so a
    refusal never costs the typing.
    """
    name_text = _text(name_value)
    address_text = _text(address_value)
    parts = [
        '<section class="card card--wide" id="create" aria-labelledby="create-h">',
        '<h2 class="h2" id="create-h">Create an account</h2>',
    ]
    if error:
        parts.append(banner(kind="error", message=_text(error)))
    parts.append(f'<form class="form" method="post" action="{_esc(action)}">')
    parts.append(
        '<label class="field"><span>Account name</span>'
        f'<input name="owner_id" value="{_esc(name_text)}" required '
        'autocomplete="off" placeholder="alice">'
        '<span class="hint">Shown wherever this account appears.</span></label>'
    )
    parts.append(
        '<label class="field"><span>Currency</span>'
        '<input name="currency" value="USD" autocomplete="off"></label>'
    )
    opened = " open" if address_text else ""
    parts.append(f'<details class="adv"{opened}><summary>Advanced</summary>')
    parts.append(
        '<label class="field"><span>Account ID (optional)</span>'
        f'<input name="account_id" value="{_esc(address_text)}" '
        'autocomplete="off" placeholder="blank, or an id you choose">'
        '<span class="hint">Leave blank and an ID is derived from the name. If you '
        "choose one, it is how people will address this account — and anyone "
        "who knows it can act as this account.</span></label>"
    )
    parts.append("</details>")
    parts.append('<button class="btn btn-primary" type="submit">Create account</button>')
    parts.append("</form></section>")
    return "".join(parts)


def send_form(*, action: str, address: object) -> str:
    """The send form. The only hidden field carries the active account id.

    That field is **not** a secret and not an idempotency key: it says which
    account the form acts as, and it is resolved by the same rule the renderer
    uses, so the page and the action cannot disagree about who the sender is.

    The amount label deliberately does **not** name a currency. This function is
    not told the account's currency, and printing "USD" over a EUR account would
    be inventing a fact about someone's money.
    """
    return (
        '<section class="card card--send" aria-labelledby="send-h">'
        '<h2 class="h2" id="send-h">Send money</h2>'
        f'<form class="form" method="post" action="{_esc(action)}">'
        f'<input type="hidden" name="account" value="{_esc(_text(address))}">'
        '<div class="form-row">'
        '<label class="field"><span>To account ID</span>'
        '<input name="to_account_id" required autocomplete="off" '
        'placeholder="acct-bob"></label>'
        '<label class="field"><span>Amount</span>'
        '<input name="amount" required inputmode="decimal" autocomplete="off" '
        'placeholder="12.34"></label>'
        "</div>"
        '<button class="btn btn-primary" type="submit">Send</button>'
        "</form>"
        '<p class="hint">Amounts are sent as whole minor units; more than two '
        "decimal places is rejected, never rounded.</p></section>"
    )


def empty_state(*, message: str) -> str:
    """A real empty state. The old build put "No activity yet." in a table cell,
    which reads as a broken table rather than as an answer."""
    return (
        '<div class="empty">'
        '<p class="empty__title">Nothing here yet</p>'
        f"<p>{_esc(message)}</p></div>"
    )


def activity_table(*, rows: object) -> str:
    """Activity as a table, because the column relationships are the meaning.

    Rows may be dicts or objects. Direction becomes **words** — "Sent to" /
    "Received from" — not the wire enum, and the sign is a character so colour is
    never the only channel. A counterparty is labelled with its owner's name when
    the row carries one and with its address otherwise; the address is always
    present in the title, so a name never hides who was actually paid.
    """
    items = list(rows or [])  # type: ignore[arg-type]
    if not items:
        return empty_state(message=NO_ACTIVITY_MESSAGE)
    body = []
    for row in items:
        direction = _text(_field(row, "direction", ""))
        debit = direction == "debit"
        word = "Sent to" if debit else "Received from"
        sign = "−" if debit else "+"
        amount = _amount_display(row)
        # A sign is a claim about a direction. Where the amount could not be
        # rendered, `_amount_display` returns the placeholder and the sign has
        # nothing true to attach to: "+—" reads as a broken cell and asserts a
        # credit for a figure the module does not have. Show the placeholder
        # alone. Reached in practice only by a caller passing rows that carry
        # neither `amount_display` nor (`amount_minor` and `currency`) — which is
        # exactly the API's own activity key set, since a currency belongs to the
        # account and not to an entry.
        if amount == _AMOUNT_UNAVAILABLE:
            sign = ""
        account_id = _text(_field(row, "counterparty_account_id", ""))
        label = _text(_field(row, "counterparty_label")
                      or _field(row, "counterparty_owner_id")).strip()
        shown = label or _short(account_id)
        when = _text(_field(row, "created_at", ""))
        amount_class = "amt" if debit else "amt credit"
        body.append(
            "<tr>"
            f'<td class="when" title="{_esc(when)}">{_esc(when)}</td>'
            f'<td class="dir">{_esc(word)}</td>'
            f'<td class="who" title="{_esc(account_id)}">{_esc(shown)}</td>'
            f'<td class="{amount_class}">{_esc(sign + amount)}</td>'
            "</tr>"
        )
    return (
        '<table class="activity">'
        '<caption class="vh">Activity for this account</caption>'
        "<thead><tr>"
        '<th scope="col">When</th><th scope="col">What</th>'
        '<th scope="col">Counterparty</th>'
        '<th scope="col" class="amt">Amount</th>'
        "</tr></thead>"
        f'<tbody>{"".join(body)}</tbody></table>'
    )


def pending_block(*, pending: object, action: str = "/retry") -> str:
    """Unconfirmed transfers — the most important display in the product.

    A transfer in flight must never read as settled, so this is a distinct
    surface with the word "Pending" on it, not a quieter row in the feed.

    Two things it must not get wrong:

    * **It names both accounts.** On one account's page a record that named only
      the payee would read as that page's own transfer. The sender is named too.
    * **One Retry for the whole set.** ``resume()`` replays the entire store, so
      a per-row retry control would be a button that lies about its scope.

    The heading string is **not free copy** (planner, 2026-10-04). The gate
    asserts ``"Unconfirmed transfers" in page`` on the redirect target —
    ``app/selfcheck.py:491``, reached from ``tests/test_persist_ordering.py:189``,
    so a red row there reddens phase 1. This heading read "Needs attention" in
    the first draft of this module; the name was changed back rather than the
    assertion, because that row's subject is *the pending transfer is visible
    after the 303 redirect*, and it deserves a stable anchor more than the copy
    deserves a synonym. The distinction the row is not making — pending is a
    state, not an absence — is carried by the ``pill`` below it, which is this
    component's actual contribution.

    Nothing pending renders nothing at all: an always-empty "Unconfirmed
    transfers" box teaches people to stop reading the one box that matters.
    """
    records = list(pending or [])  # type: ignore[arg-type]
    if not records:
        return ""
    items = []
    for rec in records:
        sender = _text(_field(rec, "from_account_id", ""))
        payee = _text(_field(rec, "to_account_id", ""))
        when = _text(_field(rec, "created_at", ""))
        items.append(
            "<li>"
            f'<span class="amt">{_esc(_amount_display(rec))}</span>'
            f'<span class="who">{_esc(sender)} → {_esc(payee)}</span>'
            f'<span class="when">{_esc(when)}</span>'
            "</li>"
        )
    return (
        '<section class="card card--pending card--wide" aria-labelledby="pend-h">'
        '<h2 class="h2" id="pend-h">Unconfirmed transfers '
        '<span class="pill">Pending</span></h2>'
        f'<ul class="pending-list">{"".join(items)}</ul>'
        "<p>These transfers were sent but not confirmed.</p>"
        f'<form method="post" action="{_esc(action)}">'
        '<button class="btn btn-primary" type="submit">Retry</button></form>'
        '<p class="hint">Pressing Retry reuses the key already stored on the '
        "server — it cannot create a second transfer.</p></section>"
    )
