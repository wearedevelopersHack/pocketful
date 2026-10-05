"""T27 at the gate: ``app/design.py``'s two presentation constraints.

Obligation (room plan #12; designer, ``3c3d2c0f``, against ``app/design.py
44d785bb9a88b156fc4301e07725ace41a7bbe0242f612dbf383663f8ce17b13``):

* the address is **whole in the wallet header** and truncated **only in lists**;
* a friendly name is shown **beside** the id, **never instead of** it.

Both are claims about rendered markup, so both are asserted against rendered
markup. Neither is a style preference: the address is the capability (a payer
types it), and a list that showed only a name would hide who was actually paid.

**Why this file calls the components directly.** ``app/design.py``'s subject *is*
the renderer — each component takes plain data and returns an HTML string — and
``app/web.py:164-173`` composes them by passing the server's values straight
down, with no escaping, rewriting or truncation in between:

    wallet_header(name=name, address=account_id, balance_display=...)   # :164
    activity_table(rows=rows)                                           # :173

The trace is what makes the fixture honest: ``name`` is the server's ``owner_id``
and each row's label is its ``counterparty_owner_id``, so **the component is the
injection surface**, and a hostile string handed to ``wallet_header(name=...)``
in this file is the same string the server would hand it. *Which* component
appears on which page, and what ``app/web.py`` passes — the composition — is
``test_web_accounts.py``'s subject, not this file's; the one exception is the
last pair of rows, which is deliberately about a *coupling between two*
components and calls ``render_wallet`` to measure it on the real composition.

**A contradiction, named rather than resolved here.** ``DESIGN-SPEC.md:362`` (the
"id always" row) says the header id is *truncated* with the *full id in a
``title`` attribute*. ``app/design.py:413`` renders the address whole and its
``title`` is the static string ``"Account ID"``. The module and the designer's
instruction agree with each other, so **the rows below certify the module**, and
the spec row is the artifact raised to the planner as the discrepancy. If the
spec turns out to be the live intent, the row that fails is
``test_the_header_shows_the_address_whole``, and it is meant to.

**Why the falsifier rows are permanent and in-gate.** Every constraint below the
fold is paired with a mutant that injects the defect into the *real* component's
own output and asserts that the very predicate the direct row uses rejects it.
A one-shot demonstration in ``/tmp`` is not gate evidence, and an ``_esc`` mutant
against a fixture of ``acct-abc123`` would be **inert** (designer, ``3c3d2c0f``):
removing an escaper changes nothing if nothing in the data needs escaping. So the
hostile fixture carries ``<script>``, ``"`` and ``&``, and each mutant asserts its
own **premise** — that the defect it injected actually reached the output —
before it asserts the effect.
"""

import html.parser
import re
import unittest
from unittest import mock
from urllib.parse import quote

import app.design as app_design
import app.web as app_web

# -- fixtures -----------------------------------------------------------------

#: Long enough that ``_short`` must differ from it, or the truncation rows would
#: be asserting that "a" == "a". Asserted below rather than assumed.
LONG_ID = "acct-9f3c1a7e5b2d8046f1e9c3a5"
SHORT_ID = "acct-bob"          # <= _short's 8+4+1 threshold: unchanged by it

NAME = "Bob"
HOSTILE = '<script>alert("x")</script>&'

#: The three markup sites, matched exactly as ``app/design.py`` emits them.
_ADDR = re.compile(r'<code class="addr"[^>]*>(.*?)</code>', re.S)
_H1 = re.compile(r'<h1 class="h1"[^>]*>(.*?)</h1>', re.S)
# The non-active switcher entry: ``<li><a href="..." title="<full id>">shown</a></li>``
_SWITCH_ITEM = re.compile(r'<li><a href="[^"]*" title="([^"]*)">(.*?)</a></li>', re.S)
_SWITCH_ACTIVE = re.compile(r'<li><a href="[^"]*" aria-current="page">(.*?)</a></li>', re.S)
# The activity counterparty cell: ``<td class="who" title="<full id>">shown</td>``
_WHO_CELL = re.compile(r'<td class="who" title="([^"]*)">(.*?)</td>', re.S)

_ELLIPSIS = "…"


def _one(pattern, html, what):
    match = pattern.search(html)
    if match is None:
        raise AssertionError(f"{what} is not in the rendered markup:\n{html}")
    return match.groups()


# -- the predicates, as functions, so a mutant can be run through them --------
#
# Each raises AssertionError with the measurement in the message. The falsifier
# rows call *these same functions* on mutated output, so a green falsifier is
# evidence about the predicate the direct row uses and not about a re-typed copy
# of it.


def assert_header_address_is_whole(html, address):
    (shown,) = _one(_ADDR, html, "the header's <code class=\"addr\"> element")
    if shown != app_design._esc(address):
        raise AssertionError(
            f"the header must show the address whole; it shows {shown!r} for "
            f"the address {address!r}"
        )
    if _ELLIPSIS in shown:
        raise AssertionError(
            f"the header's address is truncated ({shown!r}); truncation belongs "
            f"to lists, and the header is the value someone has to give out"
        )


def assert_a_list_shows_the_short_form_and_keeps_the_full_id(html, account_id):
    """A list entry with no name: the visible text is the short form, and the
    full id is still reachable in ``title`` — a truncated id that is not
    recoverable anywhere would make the row a display of a value nobody can use."""
    (title, shown) = _one(_WHO_CELL if "<td" in html else _SWITCH_ITEM, html,
                          "a list entry carrying a title")
    if shown != app_design._esc(app_design._short(account_id)):
        raise AssertionError(
            f"a list shows {shown!r}; it must show the short form "
            f"{app_design._short(account_id)!r}"
        )
    if title != app_design._esc(account_id):
        raise AssertionError(
            f"the list entry's title is {title!r}; the FULL id "
            f"{account_id!r} must stay reachable there"
        )


def assert_name_is_beside_the_id(html, name, account_id):
    """Somewhere in this markup both the name and the full id must appear."""
    if app_design._esc(name) not in html:
        raise AssertionError(f"the name {name!r} is not rendered at all")
    if app_design._esc(account_id) not in html:
        raise AssertionError(
            f"the full id {account_id!r} was replaced by the name {name!r}; a "
            f"name sits beside an id, never instead of it"
        )


def assert_no_raw_markup(html):
    """No unescaped metacharacter from the fixture reaches the output."""
    for raw in ("<script>", "</script>", 'alert("x")'):
        if raw in html:
            raise AssertionError(
                f"the raw sequence {raw!r} reached the output unescaped:\n{html}"
            )


def _identity_esc(value):
    """MUTANT: ``_esc`` with the escaping removed."""
    return str(value)


class _Readability(html.parser.HTMLParser):
    """What a person can read on the page, and what only a machine can follow.

    The distinction is the designer's (``101dc286``): an ``href`` is
    *machine*-reachability, not reader-reachability — a percent-encoded id can be
    followed but not read. So the two are collected separately, and the claim
    under test is about ``text``.

    ``<head>`` is excluded because the document title is not the page a person is
    looking at, and ``<style>`` because CSS is not text. Both exclusions matter
    and are the reason this is a parser rather than a regex over the markup: a
    regex cannot tell a text node from an attribute value, and the whole point of
    the cross-component row below is exactly that difference.
    """

    _SKIP = ("head", "style")

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._skipping = 0
        self.text: list[str] = []
        self.attrs: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skipping += 1
        for _name, value in attrs:
            if value:
                self.attrs.append(value)

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skipping:
            self._skipping -= 1

    def handle_data(self, data):
        if not self._skipping:
            self.text.append(data)


def _readability(page_html):
    parser = _Readability()
    parser.feed(page_html)
    return parser


def assert_the_active_id_is_readable_on_the_page(page_html, account_id):
    """The account the page is showing must have its id **readable in the body**.

    Not "present somewhere": the switcher's active entry carries the id only in
    its ``href``, and a ``title`` is not a thing a touch-screen has. This is a
    claim about the composition — it holds because ``wallet_header`` renders the
    address whole, and it is exactly what a truncating header would take away
    while every single-component row above stayed green.

    **The ``<head>`` exclusion is load-bearing, not tidiness** (designer,
    ``2e5442f7``). ``app/web.py:191`` builds the document title as
    ``Pocketful — {account_id}`` — the full id, untruncated — so a third carrier
    exists that neither of us counted at first. Extending this predicate to a full
    response ("test the actual response" is the obvious next move) would let
    ``<title>`` satisfy ``text``, and the direct row would then go green for a
    header that had truncated: the id would still be *present*, just not usable.
    The falsifier's premise (``absent from text``) is what catches that, so it must
    survive any such extension. The claim this row makes is narrower than "the id
    survives somewhere": it is that the id is readable **as page content** and
    copyable. A tab title is visible but not selectable; an ``href`` is followable
    but not readable. Those are different from the id being usable, and only the
    header is the first kind.
    """
    page = _readability(page_html)
    if not any(account_id in part for part in page.text):
        raise AssertionError(
            f"the full id {account_id!r} is not readable text anywhere in the "
            f"page body — it is only machine-reachable (attributes: "
            f"{[a for a in page.attrs if account_id in a]!r})"
        )


class DesignMarkup(unittest.TestCase):

    def setUp(self):
        # The premise every truncation row rests on: the fixture is long enough
        # that ``_short`` changes it. Without this, ``assert_header_address_is_whole``
        # would be satisfied by a component that truncated, and
        # ``assert_a_list_...`` by one that did not short anything.
        self.assertNotEqual(app_design._short(LONG_ID), LONG_ID,
                            "LONG_ID must be long enough for _short to alter it, "
                            "or the truncation rows cannot discriminate")
        self.assertEqual(app_design._short(SHORT_ID), SHORT_ID,
                         "SHORT_ID must be short enough that _short leaves it "
                         "alone, so the list row is about the long case")

    def header(self, *, name=None, address=LONG_ID, balance_display="$100.00"):
        return app_design.wallet_header(name=name, address=address,
                                        balance_display=balance_display)

    def switcher(self, entry, active_id="acct-someone-else"):
        return app_design.account_switcher(accounts=[entry], active_id=active_id)

    def activity(self, row):
        return app_design.activity_table(rows=[row])

    def wallet(self, **overrides):
        """The **real** composition — ``app/web.py:render_wallet``, the same
        function the server calls at ``:422`` — not a reconstruction of it."""
        kwargs = dict(account_id=LONG_ID, balance_minor=0, currency="USD",
                      rows=[], pending=[], message=None, notice=None, name=None,
                      accounts=[{"account_id": LONG_ID}])
        kwargs.update(overrides)
        return app_web.render_wallet(**kwargs)

    # -- constraint 1: whole in the header, truncated only in lists ----------

    def test_the_header_shows_the_address_whole(self):
        """The header is where the account's own id is read and copied, so it is
        shown whole. Certifies ``app/design.py:413`` and the designer's §5.1
        instruction; contradicts ``DESIGN-SPEC.md:362``, which is the finding."""
        assert_header_address_is_whole(self.header(), LONG_ID)

    def test_the_header_does_not_truncate_when_a_name_is_present_too(self):
        """A name must not buy the header permission to shorten the id."""
        assert_header_address_is_whole(self.header(name=NAME), LONG_ID)

    def test_a_list_shows_the_short_form_and_keeps_the_full_id(self):
        """Both lists. ``_short`` in the visible text, the full id in ``title``."""
        assert_a_list_shows_the_short_form_and_keeps_the_full_id(
            self.activity({"direction": "debit", "counterparty_account_id": LONG_ID,
                           "amount_minor": 500, "currency": "USD",
                           "created_at": "2026-10-05T00:00:00Z"}),
            LONG_ID)
        assert_a_list_shows_the_short_form_and_keeps_the_full_id(
            self.switcher({"account_id": LONG_ID}), LONG_ID)

    def test_the_full_id_stays_reachable_in_both_switcher_branches(self):
        """The two branches carry the id by different routes, and only one of
        them is a ``title``.

        The plain entry puts the FULL id in ``title``; the ``aria-current``
        (active) entry has **no ``title`` at all** and carries the id in its
        ``href``. This row exists because the difference is exactly the kind of
        thing a reader infers wrongly: the first version of it asserted the
        active entry had *no* full-id affordance, inferred from the missing
        ``title``, and the assertion failed against the rendered markup — the
        ``href`` holds the id. Asserted here as measured, both branches, so the
        check's shape is on the record rather than in my head.
        """
        encoded = quote(LONG_ID, safe="")

        plain = self.switcher({"account_id": LONG_ID}, active_id="acct-someone-else")
        self.assertIn(f'href="?account={encoded}"', plain)
        self.assertIn(f'title="{app_design._esc(LONG_ID)}"', plain)

        active = self.switcher({"account_id": LONG_ID, "name": NAME}, active_id=LONG_ID)
        self.assertIn(f'href="?account={encoded}"', active,
                      "the active entry must keep the id reachable through its "
                      "href, since it deliberately carries no title")
        self.assertEqual(_one(_SWITCH_ACTIVE, active, "the active switcher entry")[0],
                         app_design._esc(NAME),
                         "and the visible text is still the name")
        self.assertEqual(len(_SWITCH_ITEM.findall(active)), 0,
                         "the active entry is a distinct branch of the component; "
                         "if it ever renders through the titled branch instead, "
                         "the two rows above are the ones that move")

    # -- constraint 2: the name beside the id, never instead of it -----------

    def test_a_name_never_replaces_the_id_in_the_header(self):
        html = self.header(name=NAME)
        self.assertEqual(_one(_H1, html, "the header heading")[0],
                         app_design._esc(NAME))
        assert_name_is_beside_the_id(html, NAME, LONG_ID)

    def test_a_name_never_replaces_the_id_in_a_list(self):
        """In a list the name is the *visible* text, so "beside" is carried
        entirely by ``title`` — which makes the title load-bearing, not decoration."""
        for html in (self.switcher({"account_id": LONG_ID, "name": NAME}),
                     self.activity({"direction": "debit", "counterparty_account_id": LONG_ID,
                                    "counterparty_owner_id": NAME, "amount_minor": 500,
                                    "currency": "USD",
                                    "created_at": "2026-10-05T00:00:00Z"})):
            (title, shown) = _one(_WHO_CELL if "<td" in html else _SWITCH_ITEM, html,
                                  "the named list entry")
            self.assertEqual(shown, app_design._esc(NAME))
            self.assertEqual(title, app_design._esc(LONG_ID),
                             "the full id must survive as the title when a name "
                             "takes the visible text")
            assert_name_is_beside_the_id(html, NAME, LONG_ID)

    # -- the cross-component coupling (designer, 101dc286) -------------------

    def test_the_active_id_is_readable_on_the_wallet_page(self):
        """The account the page is showing must have its id **readable text in
        the body**, not merely followed by a machine.

        This is the one row here that is about a *composition* rather than a
        single component, and it exists because the two components' guarantees
        are coupled and neither one alone notices when the coupling breaks. The
        header renders the address whole; the switcher's active entry carries it
        only percent-encoded in its ``href``, which is machine-reachability, not
        reader-reachability, and it carries no ``title`` at all. So the header is
        the *only* place a person reads this id — and a header that truncated
        again would leave every row above green while making the account's own
        id unreadable on a touch screen.

        It calls ``app/web.py:render_wallet`` rather than reassembling the page,
        so it cannot drift from the composition it is about.
        """
        assert_the_active_id_is_readable_on_the_page(self.wallet(), LONG_ID)

    def test_a_truncating_header_makes_the_active_id_unreadable(self):
        """MUTANT: the cross-component row's falsifier, and the only one here
        whose premise spans two components.

        With ``wallet_header`` truncating, the switcher is untouched — so the id
        is still *machine*-reachable through the active entry's ``href``. That is
        the premise: if the id vanished from the attributes too, this mutant
        would be measuring a page that lost the id, not a page that hid it.
        """
        _real = app_web.wallet_header

        def truncating_header(*, name, address, balance_display):
            return _real(name=name, address=address,
                         balance_display=balance_display).replace(
                app_design._esc(str(address).strip()),
                app_design._esc(app_design._short(address)))

        with mock.patch.object(app_web, "wallet_header", truncating_header):
            page_html = self.wallet()

        page = _readability(page_html)
        self.assertTrue(any(LONG_ID in value for value in page.attrs),
                        "premise: the id must still be machine-reachable, or the "
                        "mutant is measuring a page that dropped it")
        self.assertFalse(any(LONG_ID in part for part in page.text),
                         "premise: the header's truncation must actually remove "
                         "the id from the readable text")
        with self.assertRaises(AssertionError):
            assert_the_active_id_is_readable_on_the_page(page_html, LONG_ID)

    # -- falsifiers: the predicates above must reject the defects ------------

    def test_a_truncating_header_is_rejected(self):
        """MUTANT: the real component's own output, with the header's address
        replaced by its short form — i.e. the header behaving like a list. The
        premise is asserted first: the mutation really did shorten the address."""
        _real = app_design.wallet_header

        def truncating_header(*, name, address, balance_display):
            return _real(name=name, address=address,
                         balance_display=balance_display).replace(
                app_design._esc(str(address).strip()),
                app_design._esc(app_design._short(address)))

        html = truncating_header(name=None, address=LONG_ID, balance_display="$0.00")
        (shown,) = _one(_ADDR, html, "the mutant's addr element")
        self.assertIn(_ELLIPSIS, shown,
                      "the mutant must actually truncate, or this row measures "
                      "nothing but a typo in the replacement")
        with self.assertRaises(AssertionError):
            assert_header_address_is_whole(html, LONG_ID)

    def test_an_id_replaced_by_its_name_is_rejected(self):
        """MUTANT: the real header with the whole ``addr`` element deleted, so the
        heading is the only identification — the exact "instead of" defect."""
        _real = app_design.wallet_header

        def idless_header(*, name, address, balance_display):
            html = _real(name=name, address=address, balance_display=balance_display)
            return _ADDR.sub("", html)

        html = idless_header(name=NAME, address=LONG_ID, balance_display="$0.00")
        self.assertIn(app_design._esc(NAME), html,
                      "the mutant must still render the name, or it is measuring "
                      "a blank page")
        self.assertNotIn(app_design._esc(LONG_ID), html,
                         "the mutant must have removed the id, or it is not the "
                         "defect under test")
        with self.assertRaises(AssertionError):
            assert_name_is_beside_the_id(html, NAME, LONG_ID)

    def test_a_list_title_dropped_is_rejected(self):
        """MUTANT: the switcher's ``title`` attribute removed, which is how a
        named list entry loses its id while still looking correct."""
        _real = app_design.account_switcher

        def untitled_switcher(*, accounts, active_id):
            return re.sub(r' title="[^"]*"', "", _real(accounts=accounts,
                                                       active_id=active_id))

        html = untitled_switcher(accounts=[{"account_id": LONG_ID}],
                                 active_id="acct-someone-else")
        self.assertNotIn("title=", html, "the mutant must have removed the title")
        with self.assertRaises(AssertionError):
            assert_a_list_shows_the_short_form_and_keeps_the_full_id(html, LONG_ID)

    def test_removing_the_escaper_is_caught_on_the_real_injection_surface(self):
        """MUTANT: ``_esc`` with escaping removed, driven through the two sites
        the server's own strings reach — the header heading (``owner_id``) and
        the activity row label (``counterparty_owner_id``).

        The hostile fixture is what gives this teeth: against ``acct-abc123`` an
        identity escaper is **inert**, and the row would stay green for a reason
        unrelated to escaping. So the mutant asserts its premise — the raw
        ``<script>`` really did reach the output — before the effect.
        """
        with mock.patch.object(app_design, "_esc", _identity_esc):
            heading = app_design.wallet_header(
                name=HOSTILE, address=LONG_ID, balance_display="$0.00")
            row = app_design.activity_table(rows=[{
                "direction": "credit", "counterparty_account_id": LONG_ID,
                "counterparty_owner_id": HOSTILE, "amount_minor": 10000,
                "currency": "USD", "created_at": "2026-10-05T00:00:00Z",
            }])

        for surface, html in (("header heading", heading), ("activity label", row)):
            # The premise, asserted rather than assumed: this is the mutant's
            # whole point, and without it a red below could be an unrelated break.
            self.assertIn("<script>", html,
                          f"the mutant must carry the unescaped byte into the "
                          f"{surface}, or it is inert and proves nothing")
            with self.assertRaises(AssertionError):
                assert_no_raw_markup(html)

    def test_the_control_escapes_the_same_hostile_fixture(self):
        """...and the same hostile fixture on the unmutated component is escaped
        at both sites, so the row above is a detection and not a red that is red
        no matter what. The escaped forms are asserted positively, so a component
        that dropped the value entirely would fail here too."""
        for surface, html in (
            ("header heading", self.header(name=HOSTILE)),
            ("activity label", self.activity({
                "direction": "credit", "counterparty_account_id": LONG_ID,
                "counterparty_owner_id": HOSTILE, "amount_minor": 10000,
                "currency": "USD", "created_at": "2026-10-05T00:00:00Z"})),
        ):
            assert_no_raw_markup(html)
            self.assertIn("&lt;script&gt;", html,
                          f"the escaped form is absent from the {surface}: the "
                          f"value was dropped rather than escaped")
            self.assertIn("&amp;", html,
                          f"the ampersand is not escaped in the {surface}")
            self.assertIn("&quot;", html,
                          f"the double quote is not escaped in the {surface}")


# -- the 1.4.11 boundary family (DESIGN-SPEC.md §5.9; board #29 / T43) --------
#
# The unit of this check is the **declaration**, not the element. ``.demo``
# shipped invisible because I measured one rule, repaired that one, and never
# asked the token it used where else it appeared. So this reads the stylesheet's
# own text and puts the same question to every border declaration in it: adding a
# new element on the decorative hairline reddens the sweep *wherever* it is
# added, and there is no list of elements anyone has to remember to extend.

#: Selectors that may keep the decorative hairline, each with the ruling that
#: allows it. An exemption is a decision on the record, not a silent omission --
#: and ``assert_no_unruled_border`` fails if this list goes stale, so it cannot
#: quietly grow a selector that is no longer in the sheet.
_RULED_OUT = {
    ".topbar":
        "a region rule, not a component boundary: the header's own content (the "
        "brand, the demo notice) identifies it, so 1.4.11 does not bind it, and a "
        "--border-strong line across the whole page top reads as a heavy bar",
    ".activity th, .activity td":
        "table row dividers: content structure, not a user interface component. "
        "At the tuned token these would be 3.67:1 bars across every row",
}

#: The tokens that mean *"this border is the element's only boundary signal"*.
#: ``--border-strong`` is the control case (the affordance is the border, in
#: either palette); ``--border-edge`` is the panel case (the border carries the
#: element only in the palette where nothing else does).
_BOUNDARY_TOKENS = ("--border-strong", "--border-edge")

def _css_rules(css):
    """``(selector, body)`` for every rule in a stylesheet, comments stripped.

    A depth walk rather than a ``[^{}]*\\{...\\}`` regex because the sheet
    contains an ``@media`` block: the regex matches the inner rule and then
    carries the ``@media``'s closing brace into the *next* selector, so the
    selector it reports for the rule after it is wrong. A sweep whose selector is
    wrong is a sweep that exempts the wrong element.
    """
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    rules, depth, buf, selector = [], 0, [], ""
    for ch in css:
        if ch == "{":
            if depth == 0:
                selector = re.sub(r"\s+", " ", "".join(buf)).strip()
                buf = []
            else:
                buf.append(ch)
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                rules.append((selector, "".join(buf)))
                buf = []
            else:
                buf.append(ch)
        else:
            buf.append(ch)
    return rules


def _declarations_on(css, token):
    """``selector -> [declaration, ...]`` for every declaration using ``token``."""
    found: dict[str, list[str]] = {}
    for selector, body in _css_rules(css):
        for decl in body.split(";"):
            decl = decl.strip()
            head, _, value = decl.partition(":")
            if "border" in head and f"var({token})" in value:
                found.setdefault(selector, []).append(decl)
    return found


def _rule_body(css, selector):
    for found, body in _css_rules(css):
        if found == selector:
            return body
    raise AssertionError(f"no rule for {selector!r} in the sheet")


def _palette(dark):
    """The effective token table for one palette -- the dark overrides layered on
    the base table, which is exactly what the emitted ``@media`` block does."""
    table = dict(app_design.TOKENS)
    if dark:
        table.update(app_design.TOKENS_DARK)
    return table


def _srgb_channel(value):
    value = value / 255
    return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4


def _relative_luminance(hex_colour):
    text = hex_colour.lstrip("#")
    r, g, b = (int(text[i:i + 2], 16) for i in (0, 2, 4))
    return (0.2126 * _srgb_channel(r) + 0.7152 * _srgb_channel(g)
            + 0.0722 * _srgb_channel(b))


def _contrast(foreground, background):
    """The WCAG 2.x ratio, on the module's own token values -- never copies of
    them, so a token edit moves this number instead of leaving it behind."""
    a, b = _relative_luminance(foreground), _relative_luminance(background)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


#: The solid layer of a ``--shadow-*`` token, so the shadow's contribution can be
#: composited from the value rather than quoted as a literally-typed colour.
_SHADOW_RGB = re.compile(r"rgb\(\s*(\d+)\s+(\d+)\s+(\d+)\s*/\s*([\d.]+)\s*\)")


def _composite(colour, alpha, backdrop):
    """``colour`` at ``alpha`` over ``backdrop`` -- both ``#rrggbb``."""
    fg = [int(colour.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4)]
    bg = [int(backdrop.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4)]
    return "#%02x%02x%02x" % tuple(
        round(f * alpha + b * (1 - alpha)) for f, b in zip(fg, bg))


def assert_boundary_clears_three_to_one(token, backdrop, tokens, where):
    ratio = _contrast(tokens[token], tokens[backdrop])
    if ratio < 3.0:
        raise AssertionError(
            f"{where}: {token} ({tokens[token]}) against {backdrop} "
            f"({tokens[backdrop]}) measures {ratio:.2f}:1, under the 3:1 WCAG "
            f"1.4.11 requires where the border is the element's only boundary"
        )
    return ratio


def assert_no_unruled_border(css):
    found = _declarations_on(css, "--border")
    unexpected = {s: d for s, d in found.items() if s not in _RULED_OUT}
    if unexpected:
        raise AssertionError(
            "these selectors put a border on the decorative hairline and are not "
            "on the ruled-out list; where a border is the element's only boundary "
            f"signal it needs a tuned token: {unexpected!r}"
        )
    if sorted(found) != sorted(_RULED_OUT):
        raise AssertionError(
            f"the ruled-out list has gone stale: the sheet declares --border on "
            f"{sorted(found)!r} but the list names {sorted(_RULED_OUT)!r}"
        )


_WHEN_CELL = re.compile(r'<td class="when" title="([^"]*)">(.*?)</td>', re.S)


def assert_when_cell_is_readable(html, raw):
    """The visible cell is the humanised form; the raw record is in ``title``.

    A cell that showed the raw string is not a *wrong* value, which is why this
    has to be its own claim: it is the same fact, printed at a precision nobody
    reading an activity feed asked for. The record stays reachable -- the display
    is narrowed, not the value.
    """
    title, shown = _one(_WHEN_CELL, html, "the when cell")
    if title != raw:
        raise AssertionError(
            f"the when cell's title is {title!r}; the recorded value {raw!r} must "
            f"stay reachable there"
        )
    if shown != app_design._when_display(raw):
        raise AssertionError(
            f"the when cell shows {shown!r}; it must show the humanised form "
            f"{app_design._when_display(raw)!r}"
        )
    if raw in shown:
        raise AssertionError(
            f"the when cell shows the raw recorded string {shown!r}; the raw form "
            f"belongs in the title"
        )


class DesignBoundaries(unittest.TestCase):
    """Board #29 / T43: the family, not the instance."""

    #: The pre-fix dark value of the panel boundary, kept as a literal so the
    #: red-first row can reproduce the bytes that shipped. This is the whole
    #: point of that row: the defect is re-created, not described.
    PRE_FIX_DARK_EDGE = "#2a3646"

    def test_no_border_sits_on_the_decorative_hairline_unruled(self):
        """Every ``var(--border)`` border in the sheet is a decision.

        This is the row that would have caught ``.demo``, and the row that would
        have caught the card: it is over the declaration, so it fails for an
        element nobody thought to list.
        """
        assert_no_unruled_border(app_design._COMPONENT_CSS)

    def test_the_control_boundary_clears_three_to_one_in_both_palettes(self):
        """The switcher entries are links whose border is the entire affordance.

        Fill is transparent -- the card's own ``--surface`` shows through, 1.00:1
        -- and there is no shadow, so the border is the only thing that says
        "this is a control" in *either* palette.
        """
        for dark in (False, True):
            tokens = _palette(dark)
            for backdrop in ("--surface", "--bg"):
                assert_boundary_clears_three_to_one(
                    "--border-strong", backdrop, tokens,
                    f"the switcher chips, {'dark' if dark else 'light'}")

    def test_the_panel_boundary_clears_three_to_one_where_it_is_the_only_signal(self):
        """``--border-edge`` in dark, and the light exemption stated as a value.

        In dark the card's border is its only signal: the fill step is 1.10:1 and
        ``--shadow-1`` composites to ``#07090d`` on ``#0b0f16`` -- **1.04:1**, a
        black shadow on a near-black page. In light the card is carried by its
        fill step *and* its shadow, so the light value is deliberately the same
        hairline as before; the row asserts that equality rather than trusting
        the comment that claims it, so "light is unchanged" is measured here.
        """
        dark = _palette(True)
        for backdrop in ("--surface", "--bg"):
            assert_boundary_clears_three_to_one(
                "--border-edge", backdrop, dark, "the card edge, dark")

        light = _palette(False)
        self.assertEqual(
            light["--border-edge"], light["--border"],
            "in light the panel boundary is deliberately the decorative hairline; "
            "if this moved, the light theme changed and the disclosure in "
            "DESIGN-SPEC.md §5.9 is out of date")
        self.assertIn("box-shadow", _rule_body(app_design._COMPONENT_CSS, ".card"),
                      "premise of that exemption: in light the card really does "
                      "carry a shadow, so the border is not its only signal")

    def test_the_dark_shadow_really_is_the_reason(self):
        """The premise under the whole change, asserted rather than asserted-about.

        If the dark shadow were a real boundary, dark would not need the stronger
        token and this change would be a preference. It is not. The composite is
        **derived from the tokens** rather than written down, so tuning either
        the shadow or the page moves the measurement instead of leaving it
        behind -- and the row is about the property, not about ``1.04``.
        """
        dark, light = _palette(True), _palette(False)

        def composited_over(tokens, backdrop):
            match = _SHADOW_RGB.search(tokens["--shadow-1"])
            self.assertIsNotNone(
                match, f"premise: --shadow-1 ({tokens['--shadow-1']!r}) is an "
                       f"rgb()/alpha value this row can composite")
            red, green, blue, alpha = match.groups()
            solid = "#%02x%02x%02x" % (int(red), int(green), int(blue))
            return _contrast(_composite(solid, float(alpha), tokens[backdrop]),
                             tokens[backdrop])

        dark_ratio = composited_over(dark, "--bg")
        self.assertLess(
            dark_ratio, 1.5,
            f"the dark shadow composites to {dark_ratio:.2f}:1 against the page. "
            f"If that ever becomes a boundary, --border-edge is over-tuned in dark "
            f"and the panel token should move back")
        self.assertGreater(
            composited_over(light, "--bg"), dark_ratio,
            "the shadow must be the *weaker* signal in dark than in light; that "
            "asymmetry is the reason the two palettes treat the card edge "
            "differently, and if it inverts the exemption in the row above is "
            "backwards")

    # -- the falsifiers ------------------------------------------------------

    def test_the_sweep_rejects_a_panel_put_back_on_the_hairline(self):
        """MUTANT: ``.card`` on the decorative token again -- the shipped defect,
        re-created in the sheet's own text and run through the same predicate."""
        mutant = app_design._COMPONENT_CSS.replace(
            "border: 1px solid var(--border-edge)",
            "border: 1px solid var(--border)")
        self.assertNotEqual(mutant, app_design._COMPONENT_CSS,
                            "premise: the mutation must have changed the sheet, or "
                            "this row measures a typo in the replacement")
        self.assertIn(".card", _declarations_on(mutant, "--border"),
                      "premise: the card must now be on the decorative token")
        with self.assertRaises(AssertionError):
            assert_no_unruled_border(mutant)

    def test_the_panel_predicate_rejects_the_pre_fix_dark_token(self):
        """RED-FIRST as a check, not a judgment: the token that shipped is put
        back for one call and the predicate above is run against it.

        The release directory would be the better witness, but it is not readable
        from this tree, so the pre-fix value is reproduced and *its premise
        asserted* -- that the mutant token equals the decorative one -- before
        the effect is claimed. Restored in a ``finally``.
        """
        original = app_design.TOKENS_DARK["--border-edge"]
        try:
            app_design.TOKENS_DARK["--border-edge"] = self.PRE_FIX_DARK_EDGE
            tokens = _palette(True)
            self.assertEqual(
                tokens["--border-edge"], tokens["--border"],
                "premise: the mutant must reproduce the pre-fix bytes -- the panel "
                "token equal to the decorative one -- or this falsifier is "
                "measuring some other defect")
            with self.assertRaises(AssertionError):
                assert_boundary_clears_three_to_one(
                    "--border-edge", "--surface", tokens, "the pre-fix card")
        finally:
            app_design.TOKENS_DARK["--border-edge"] = original
        self.assertEqual(app_design.TOKENS_DARK["--border-edge"], original,
                         "the mutant must be restored, or every row after this "
                         "one measures the defect")


class DesignTimestamps(unittest.TestCase):
    """DESIGN-SPEC.md §5.6: humanised visible, raw ISO in ``title``."""

    RAW = "2026-10-04T08:33:07.221767+00:00"

    def _row(self, created_at):
        return app_design.activity_table(rows=[{
            "direction": "debit", "counterparty_account_id": SHORT_ID,
            "amount_minor": 500, "currency": "USD", "created_at": created_at}])

    def test_the_when_cell_is_readable_and_keeps_the_recorded_value(self):
        assert_when_cell_is_readable(self._row(self.RAW), self.RAW)

    def test_the_pending_list_uses_the_same_display(self):
        """Two surfaces render a timestamp; a change to one that misses the other
        is how a page ends up speaking two dialects."""
        html = app_design.pending_block(pending=[{
            "from_account_id": SHORT_ID, "to_account_id": LONG_ID,
            "amount_minor": 500, "currency": "USD", "created_at": self.RAW}])
        self.assertIn(app_design._esc(app_design._when_display(self.RAW)), html)
        self.assertIn(self.RAW, html)
        self.assertNotIn(
            f'class="when">{self.RAW}', html,
            "the pending list must render the humanised form, not the raw one")

    def test_a_timestamp_it_cannot_parse_is_shown_verbatim(self):
        """No fabrication. A second, absent, or zoneless timestamp is not
        something this module may restate under a ``UTC`` label: a cell that
        reformats a string it did not understand is a cell that lies about the
        record. The zoneless case is the interesting one -- it parses as a
        *datetime* and still must not be given a zone it never carried."""
        for value in ("now", "", "2026-10-05T12:00:00", "1696500000"):
            self.assertEqual(app_design._when_display(value), value)

    def test_an_offset_is_converted_to_utc_rather_than_dropped(self):
        """The zone is named, so the value must actually be *in* it. ``23:59`` at
        ``-05:00`` is ``04:59`` the next day in UTC; a module that printed the
        wall-clock time under a UTC label would be off by five hours."""
        self.assertEqual(app_design._when_display("2026-01-02T23:59:00-05:00"),
                         "3 Jan 2026, 04:59 UTC")

    def test_the_readable_cell_rejects_the_raw_string(self):
        """MUTANT: the pre-fix cell -- raw ISO in the visible text -- re-created
        from the real component's own output and run through the same predicate."""
        mutant = re.sub(r'(<td class="when" title="[^"]*">).*?(</td>)',
                        r"\g<1>" + self.RAW + r"\g<2>", self._row(self.RAW))
        title, _shown = _one(_WHEN_CELL, mutant, "the mutant's when cell")
        self.assertEqual(title, self.RAW, "premise: the title is untouched")
        self.assertIn(f'>{self.RAW}<', mutant,
                      "premise: the raw string must really be in the visible cell")
        with self.assertRaises(AssertionError):
            assert_when_cell_is_readable(mutant, self.RAW)


if __name__ == "__main__":
    unittest.main()
