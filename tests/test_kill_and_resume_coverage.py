"""T33 at the gate: the kill-and-resume scenario's own verdict, made gate-visible.

``app/selfcheck.py`` is **not** a gate phase. ``run_gate.sh:138-139`` is
``$PY -m api.selfcheck``; ``grep -n "app.selfcheck\\|app/selfcheck" run_gate.sh``
returns nothing. The only way a scenario in that file reaches the gate is a
``tests/`` row that calls it, and until this file there was exactly one —
``tests/test_persist_ordering.py`` calls ``scenario_web_ui`` and nothing else.
So ``scenario_kill_and_resume``, **including its double-spend falsifier** (the
``MUTANT (fresh key on retry)`` run that must double-debit, which is what makes
the normal run's "moved exactly once" assertion non-vacuous), sat outside every
green run quoted in this room. A regression on that path was invisible to the
gate whether or not the rows happened to read green.

``app/selfcheck.py``'s own module docstring asks for this row in as many words:

    The gate-resident copy of the same scenario belongs in ``tests/`` and is the
    test-author's to write; this is the owner's runnable evidence.

**What this row asserts, and what it deliberately does not.** It does not
re-assert any of the scenario's conditions and does not copy them — a second
copy of an assertion is a second thing to keep true, and it drifts. It asserts
that the scenario's **own** verdict, over its **own** rows, is green, and it
bounds the slice by index around the calls so no unrelated ``RESULTS`` entry can
be counted as passing for it. That is the whole content: the scenario keeps its
assertions; this file makes them gate-visible.

**Why the falsifier is here too.** "The slice is all-green" is a sentence that
cannot fail if the slice is empty, or if the scenario silently stops running its
rows. So the control asserts the slice is non-empty, that every row in it carries
one of the scenario's two labels, and that at least one row of each run is
present; and the falsifier drives the scenario with a **store-loss mutant** (the
parent's view of the pending store loses the record the child left) and requires
that exactly the row naming that fact flips to red. Attribution, not "the run
went red": if the mutant reddened a neighbouring row, it would be measuring
something other than what it claims.

Boundary, stated because the subject is a driver outside the gate. This covers
the two runs ``app/selfcheck.py``'s ``main`` makes — ``mutate=False`` for the
normal pair and ``mutate=True`` for the second — and nothing else in that file.
``run_local_checks`` and the web scenarios are still outside the gate; the rest
of the file remains the owner's runnable evidence, not enforcement.
"""

import contextlib
import os
import shutil
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

import api.app as api_app
from app.client import ApiClient
from app.keys import new_idempotency_key
import app.selfcheck as app_selfcheck

FUNDER = "acct-funder"

#: ``(payer, payee, mutate)`` — the same four accounts and the same two runs
#: ``app/selfcheck.py``'s ``main`` makes, in the same order.
RUNS = (("acct-alice", "acct-bob", False),
        ("acct-carol", "acct-dave", True))

#: The scenario prefixes every label with one of these, so the slice can be shown
#: to contain this scenario's rows and nothing else.
SCENARIO_LABELS = ("[NORMAL]", "[MUTANT (fresh key on retry)]")

#: How many rows each run contributed when this coverage row was written.
#: Deliberately a **floor** (``>=``), not an equality: the scenario's owner adding
#: rows must not redden this file, while a row being quietly **removed** must.
#: That asymmetry is the whole point — the coverage that existed before this file
#: was a blanket all-green assertion over a scenario, and that form stays green
#: when a row disappears, so the scenario could be silently narrowed with nothing
#: to show for it (integrator, ``59427f8b``).
#:
#: Measured 2026-10-05 against ``app/selfcheck.py 1fbfdbee0c49f777``, where both
#: runs sat exactly on these numbers — so a one-row removal goes red, and the
#: floor has no slack to hide one. The revision this was first counted against,
#: ``54c300040059ed4b``, is superseded; re-measured at ``ce42ba10`` on the way here
#: it was still 9 / 6, so the breadth did not move while the file did. That is a
#: count about *that* revision and it is recorded as such: if these move again,
#: that is a fact about the scenario's breadth and should be read, not bumped.
MIN_ROWS = {"[NORMAL]": 9, "[MUTANT (fresh key on retry)]": 6}

#: The scenario's own row that names the surviving pending record. Used only to
#: say *which* row the falsifier must flip — never to re-assert its condition.
SURVIVING_RECORD_ROW = "exactly one pending record survived on disk"


def _slice_problems(rows):
    """Every way the scenario's slice failed to be a green, un-narrowed run.

    Empty means: rows were appended, all of them belong to this scenario, both
    runs contributed at least the breadth recorded in ``MIN_ROWS``, the row the
    falsifier attributes to is present, and no row failed. Returned rather than
    asserted so the coverage row and both falsifiers make the **same**
    measurement — three re-spellings of "is this slice good" would be three
    things to keep true.
    """
    if not rows:
        return ["the scenario appended no rows at all"]
    problems = []
    stray = [label for _, label in rows if not label.startswith(SCENARIO_LABELS)]
    if stray:
        problems.append(f"rows from outside this scenario: {stray}")
    for prefix in SCENARIO_LABELS:
        count = sum(1 for _, label in rows if label.startswith(prefix))
        if count == 0:
            problems.append(f"the {prefix} run contributed no rows at all")
        elif count < MIN_ROWS[prefix]:
            problems.append(
                f"the {prefix} run contributed {count} rows, fewer than the "
                f"{MIN_ROWS[prefix]} measured when this row was written — the "
                "scenario was narrowed")
    if not any(SURVIVING_RECORD_ROW in label for _, label in rows):
        problems.append(f"the row {SURVIVING_RECORD_ROW!r} did not run")
    problems.extend(f"FAILED: {label}" for ok, label in rows if not ok)
    return problems


@contextlib.contextmanager
def _running_api():
    """A real `api/` server over a real on-disk ledger, on an ephemeral port."""
    workdir = tempfile.mkdtemp(prefix="pocketful-t33-api-")
    db_path = os.path.join(workdir, "t33.db")
    server = api_app.create_server(db_path, host="127.0.0.1", port=0)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever,
                              kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        _wait_until_answering(port)
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        shutil.rmtree(workdir, ignore_errors=True)


def _wait_until_answering(port, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/accounts/nobody/balance", timeout=1):
                pass
            return
        except urllib.error.HTTPError as exc:
            # An HTTPError wraps the response fp; leaving it unclosed defers the
            # close to GC and emits a ResourceWarning. Close it here so the
            # warning channel stays meaningful.
            exc.close()
            return
        except OSError:
            time.sleep(0.02)
    raise AssertionError(f"api server on port {port} never answered")


def _fixture(base_url):
    """The accounts ``app/selfcheck.py``'s ``main`` builds before it calls this
    scenario, over the wire and in the same order.

    Reproduced rather than imported because ``main`` builds it inline. It matters
    that it is the same fixture: the scenario reads its baselines *from these
    accounts*, so a differently-funded fixture would change what the scenario
    measures. It does not change its verdict — the assertions are movements — but
    a coverage row that measured a different fixture would be evidence about a
    run nobody makes.
    """
    client = ApiClient(base_url)
    client.create_account(owner_id="funder", currency="USD",
                          allow_overdraft=True, account_id=FUNDER)
    for name in ("alice", "bob", "carol", "dave"):
        client.create_account(owner_id=name, currency="USD",
                              account_id=f"acct-{name}")
    for payer in ("alice", "carol"):
        client.send_transfer(idempotency_key=new_idempotency_key(),
                             from_account_id=FUNDER,
                             to_account_id=f"acct-{payer}",
                             amount_minor=10000, currency="USD")


_REAL_PENDING_STORE = app_selfcheck.PendingStore
"""Captured before the mutant replaces the module global.

The mutant subclasses this object; it must not be reached through
``app_selfcheck.PendingStore``, which is the name the patch replaces.
"""


class _StoreThatLostTheRecord(_REAL_PENDING_STORE):
    """MUTANT: the parent's view of the pending store after the kill, with the
    record gone.

    This is the shape of the store-loss defect this project has already extracted
    once: ``ui-pending.json`` holds the client's idempotency keys, so a store that
    reports nothing outstanding is a store that will mint a fresh key on the next
    attempt — a brand-new transfer past every ledger invariant. The scenario makes
    exactly one row about it, ``SURVIVING_RECORD_ROW``.

    Only ``outstanding()`` is overridden: the scenario's later read goes through
    ``get()`` and must keep working, or the mutant would be measuring several
    things at once.
    """

    def outstanding(self):
        return []


_REAL_CHECK = app_selfcheck.check
"""Captured before the mutant replaces the module global, for the same reason as
``_REAL_PENDING_STORE``."""


def _check_swallowing_a_row(label_fragment):
    """MUTANT: the scenario's ``check`` with one row never recorded.

    Returns ``(mutant, state)``. This is how a scenario gets **silently
    narrowed**: nothing fails, one assertion simply stops being made. The blanket
    all-green coverage this file replaced stayed green through exactly that, which
    is why the slice asserts breadth and not only the absence of failures
    (integrator, ``59427f8b``). ``state['dropped']`` is the premise the caller
    must assert — that the row really was swallowed — so an inert mutation cannot
    make the guard's silence read as a pass.
    """
    state = {"dropped": False}

    def check(ok, label, detail=""):
        if not state["dropped"] and label_fragment in label:
            state["dropped"] = True
            return True
        return _REAL_CHECK(ok, label, detail)

    return check, state


class KillAndResumeUnderTheGate(unittest.TestCase):

    def setUp(self):
        self._workdir = tempfile.mkdtemp(prefix="pocketful-t33-")
        self.addCleanup(shutil.rmtree, self._workdir, True)

    def _run_pair(self, base_url, payer, payee, mutate):
        """The scenario's own rows, and only its own rows.

        The slice is bounded by index around the call rather than filtered out of
        the whole list, so a row that another test left behind cannot be counted.
        """
        before = len(app_selfcheck.RESULTS)
        app_selfcheck.scenario_kill_and_resume(
            base_url, self._workdir, FUNDER, payer, payee, mutate)
        return app_selfcheck.RESULTS[before:]

    # -- the coverage row -----------------------------------------------------

    def test_the_kill_and_resume_scenario_is_green_in_both_of_its_runs(self):
        """``scenario_kill_and_resume``'s verdict, in the gate.

        Both runs: the normal one (the retry must replay on the same key) and the
        mutant one (the retry mints a fresh key and **must** double-debit — that
        is the scenario's own negative control, and its rows being green is what
        makes the normal run's "moved exactly once" row evidence instead of an
        assertion that would hold for a harness that never sent anything).

        The slice is not merely judged for failures. It must be this scenario's
        rows, both runs must be present, each at no less than the breadth recorded
        in ``MIN_ROWS``, and the row the falsifier attributes to must appear. A
        blanket "no failures" assertion — the shape of coverage that existed
        before this file — is green for a scenario that was quietly narrowed, so
        absence of failures is the weakest of the things asserted here.
        """
        with _running_api() as base_url:
            _fixture(base_url)
            rows = []
            for payer, payee, mutate in RUNS:
                rows.extend(self._run_pair(base_url, payer, payee, mutate))

        problems = _slice_problems(rows)
        self.assertEqual(
            problems, [],
            "app/selfcheck.py's kill-and-resume scenario is not gate-run, so this "
            "row is what makes its verdict visible; on a quiet tree its slice must "
            f"be entirely well-formed and green. Problems: {problems}")

    # -- the falsifier --------------------------------------------------------

    def test_the_coverage_row_fires_when_a_scenario_row_is_red(self):
        """The coverage row's teeth: a red row inside the slice must be reported,
        and attributed to the row that names the fact.

        The mutant is the store-loss shape: the parent's reader reports no
        outstanding record after the kill, while the record is really on disk.
        The child processes are untouched, so the money still moves exactly as it
        does on a good run — the mutation is only in what the scenario *reads*,
        which is what its own row is about.

        The premise is asserted before the effect, and measured against the real
        reader rather than assumed: an inert mutation would make the guard's
        silence read as a pass, which this room has already been fooled by once.

        This patches a **module global** for the duration of one scenario call
        (the technique ``test_persist_ordering.py`` and ``test_wire_refusal.py``
        use); it is process-wide while held, so this row is not parallelizable.
        """
        # Premise, part one: the mutant really does diverge from the real reader.
        probe_path = os.path.join(self._workdir, "probe-store.json")
        _REAL_PENDING_STORE(probe_path).begin(
            from_account_id=FUNDER, to_account_id="acct-alice",
            amount_minor=1, currency="USD")
        self.assertTrue(_REAL_PENDING_STORE(probe_path).outstanding(),
                        "premise: the real reader must see the record the mutant "
                        "is supposed to hide")
        self.assertEqual(_StoreThatLostTheRecord(probe_path).outstanding(), [],
                         "premise: the mutant must actually hide it")

        # Effect: the scenario's own verdict, over its own rows, is no longer
        # green — and the row that flips is the one that names this fact.
        with _running_api() as base_url:
            _fixture(base_url)
            with mock.patch.object(app_selfcheck, "PendingStore",
                                   _StoreThatLostTheRecord):
                self.assertIs(
                    app_selfcheck.PendingStore, _StoreThatLostTheRecord,
                    "the patch must be in force on the name the scenario calls")
                rows = self._run_pair(base_url, "acct-alice", "acct-bob", False)

        failures = [label for ok, label in rows if not ok]
        problems = _slice_problems(rows)
        self.assertTrue(problems,
                        "a scenario row reporting a lost pending record must "
                        "redden the slice; the coverage row above would not have "
                        "noticed")
        self.assertEqual(
            len(failures), 1,
            "the mutant must flip exactly one row, or it is measuring more than "
            f"the store-loss shape; failures were {failures}")
        self.assertIn(
            SURVIVING_RECORD_ROW, failures[0],
            "the row that flips must be the one that names the lost record, not a "
            f"neighbour; the failing row was {failures[0]!r}")
        self.assertEqual(
            [p for p in problems if p.startswith("FAILED:")],
            [f"FAILED: {failures[0]}"],
            "exactly the failing row must be reported as failing, and a store-loss "
            "mutation must not be reported as a narrowing — the two are different "
            f"defects; got {problems}")
        # The slice here is deliberately one run, so the whole-slice predicate also
        # reports the absent mutant run. That is correct behaviour, asserted here so
        # a reader does not mistake it for leakage.
        self.assertIn(
            "the [MUTANT (fresh key on retry)] run contributed no rows at all",
            problems,
            f"the predicate must also name the run that did not happen; got {problems}")

    def test_the_coverage_row_fires_when_a_scenario_row_is_removed(self):
        """The coverage row's other teeth, and the sharper half: a scenario that
        is **narrowed** must be caught even though nothing fails.

        The mutant swallows one of the normal run's rows — the scenario makes one
        fewer assertion and every assertion it still makes passes. The blanket
        all-green coverage that this file replaced is green under exactly this
        mutation, which is how the scenario could shrink without anyone seeing it
        (integrator, ``59427f8b``).

        The premise is asserted, not assumed: the mutant must actually have
        swallowed a row, or a guard reporting nothing would read as a pass.
        """
        mutant, state = _check_swallowing_a_row("the payer activity feed")
        with _running_api() as base_url:
            _fixture(base_url)
            with mock.patch.object(app_selfcheck, "check", mutant):
                self.assertIs(app_selfcheck.check, mutant,
                              "the patch must be in force on the name the "
                              "scenario calls")
                rows = self._run_pair(base_url, "acct-alice", "acct-bob", False)

        self.assertTrue(state["dropped"],
                        "premise: the mutant must actually have swallowed a row, or "
                        "the guard reporting nothing would read as a pass")
        problems = _slice_problems(rows)
        self.assertTrue(problems,
                        "a scenario narrowed by one row must be reported even "
                        "though every remaining row passed")
        self.assertTrue(
            any("narrowed" in problem for problem in problems),
            f"the report must say the scenario was narrowed; got {problems}")
        self.assertFalse(
            any(problem.startswith("FAILED:") for problem in problems),
            "no row failed in this mutant — all-green and un-narrowed are different "
            f"properties and the report must not conflate them; got {problems}")
