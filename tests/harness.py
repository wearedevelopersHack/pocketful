"""Concurrency harness for the Pocketful ledger.

Everything the scenarios need to run against a *real* ledger lives here:

* a real on-disk SQLite file (WAL needs a file; `:memory:` is per-connection and
  would silently make every "concurrent" test single-connection),
* one connection per thread, so concurrency is real cross-connection contention
  and not one connection shared behind the GIL,
* a `run_concurrent` primitive that starts every task on a `threading.Barrier`
  so the tasks are actually in flight at the same time,
* a `Backend` abstraction so the *same scenario code* can be pointed at a
  deliberately broken implementation (see `mutants.py` + `test_harness_selfcheck`).

Connection/schema provenance is discovered, not assumed: if `ledger/db.py`
exposes a connection factory it is preferred (so tests exercise the production
pragmas), and if `ledger/schema.py` exposes an init function it is preferred
over the local fixture DDL. When neither exists yet, the harness says so
explicitly instead of quietly testing nothing.
"""

import contextlib
import hashlib
import importlib
import sqlite3
import threading

from tests.fixture_schema import create_fixture_schema

MAX_MINOR = 2 ** 53 - 1


class LedgerUnavailable(RuntimeError):
    """The ledger under test is not importable yet."""


# --------------------------------------------------------------------------
# module discovery
# --------------------------------------------------------------------------

def _try_import(name):
    try:
        return importlib.import_module(name)
    except ImportError:
        return None


def _find_callable(module, names):
    if module is None:
        return None
    for name in names:
        fn = getattr(module, name, None)
        if callable(fn):
            return fn
    return None


_CONNECT_NAMES = (
    "connect", "open_connection", "get_connection", "create_connection",
    "new_connection", "connect_db", "open_db", "connection",
)
_SCHEMA_NAMES = (
    "init_schema", "create_schema", "ensure_schema", "initialize",
    "init", "create_tables", "migrate", "setup",
)


def load_ledger():
    core = _try_import("ledger.core")
    if core is None or not hasattr(core, "Ledger"):
        raise LedgerUnavailable(
            "ledger.core.Ledger is not importable yet — T1 has not landed. "
            "The suite is red because the implementation does not exist, and "
            "that is reported as red, not worked around."
        )
    return core.Ledger, _try_import("ledger.db"), _try_import("ledger.schema")


# --------------------------------------------------------------------------
# connections
# --------------------------------------------------------------------------

def fallback_connect(db_path):
    """§1.1 pragmas, used only when ledger/db.py offers no factory."""
    conn = sqlite3.connect(
        db_path, timeout=30.0, isolation_level=None, check_same_thread=False
    )
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=FULL")
    return conn


class Backend:
    """A SQLite file plus a way to build the object under test on a connection."""

    label = "backend"

    #: The exception the implementation under test raises for a bad amount. Set
    #: per subclass so a scenario can assert "rejected *as* InvalidAmount"
    #: without importing the implementation's error module itself.
    invalid_amount_error = Exception

    def __init__(self, db_path):
        self.db_path = db_path
        self._conn_factory = None
        self._schema_fn = None

    def prepare(self):
        """Create the schema once, then close that connection."""
        conn = self.connect()
        try:
            self.init_schema(conn)
        finally:
            conn.close()
        return self

    # -- overridable ------------------------------------------------------
    def connect(self):
        return fallback_connect(self.db_path)

    def make_ledger(self, conn):
        raise NotImplementedError

    def init_schema(self, conn):
        create_fixture_schema(conn)

    # -- helpers ----------------------------------------------------------
    @contextlib.contextmanager
    def fresh(self):
        """A brand-new connection + object under test, closed on exit."""
        conn = self.connect()
        try:
            yield conn, self.make_ledger(conn)
        finally:
            with contextlib.suppress(Exception):
                conn.close()

    def read_conn(self):
        """A read connection that closes when the `with` block exits."""
        @contextlib.contextmanager
        def _ctx():
            conn = self.connect()
            try:
                yield conn
            finally:
                conn.close()
        return _ctx()


class RealBackend(Backend):
    """The real `ledger/` package, as specified by room plan §2.1."""

    label = "real ledger"

    def __init__(self, db_path):
        super().__init__(db_path)
        self.Ledger, db_mod, schema_mod = load_ledger()
        self.invalid_amount_error = getattr(
            _try_import("ledger.types"), "InvalidAmount", Exception
        )

        self._conn_factory = _find_callable(db_mod, _CONNECT_NAMES)
        self._conn_factory_source = (
            f"ledger.db.{self._conn_factory.__name__}"
            if self._conn_factory else "tests/harness.py fallback (ledger.db has no factory)"
        )
        # The schema init function is not part of the frozen §2.1 surface, so
        # look for it in ledger/schema.py and then in ledger/db.py. If neither
        # exposes one, fall back to the test-owned DDL fixture.
        self._schema_fn = (_find_callable(schema_mod, _SCHEMA_NAMES)
                           or _find_callable(db_mod, _SCHEMA_NAMES))
        schema_owner = "ledger.schema" if schema_mod is not None else "ledger.db"
        self._schema_source = (
            f"{schema_owner}.{self._schema_fn.__name__}"
            if self._schema_fn else "tests/fixture_schema.py (no ledger schema init fn)"
        )

    def connect(self):
        if self._conn_factory is not None:
            try:
                conn = self._conn_factory(self.db_path)
            except TypeError:
                conn = self._conn_factory()
            if isinstance(conn, sqlite3.Connection):
                return conn
            raise LedgerUnavailable(
                f"{self._conn_factory_source} returned "
                f"{type(conn).__name__}, not a sqlite3.Connection"
            )
        return fallback_connect(self.db_path)

    def make_ledger(self, conn):
        return self.Ledger(conn)

    def init_schema(self, conn):
        if self._schema_fn is not None:
            self._schema_fn(conn)
        else:
            create_fixture_schema(conn)


class MutantBackend(Backend):
    """A deliberately broken implementation, for the harness self-check only.

    Used by `test_harness_selfcheck.py` to prove each scenario detects the
    defect it exists to catch. Never used by the gate against real code.
    """

    label = "mutant"

    def __init__(self, db_path, ledger_cls):
        super().__init__(db_path)
        self.ledger_cls = ledger_cls
        from tests.mutants import MutantInvalidAmount
        self.invalid_amount_error = MutantInvalidAmount

    def connect(self):
        return fallback_connect(self.db_path)

    def make_ledger(self, conn):
        return self.ledger_cls(conn)

    def init_schema(self, conn):
        create_fixture_schema(conn)


# --------------------------------------------------------------------------
# real concurrency
# --------------------------------------------------------------------------

def run_concurrent(tasks, timeout=120.0):
    """Run every taskable callable at once on its own thread.

    All threads are released from a shared `threading.Barrier`, so the calls
    genuinely overlap instead of queueing one behind the other. Returns
    `(results, errors)` index-aligned with `tasks`; a task that raised stores
    its exception in `errors` rather than taking the whole storm down.
    """
    n = len(tasks)
    if n == 0:
        return [], []
    barrier = threading.Barrier(n)
    results = [None] * n
    errors = [None] * n

    def runner(i):
        barrier.wait(timeout=timeout)
        try:
            results[i] = tasks[i]()
        except BaseException as exc:  # noqa: BLE001 - reported, not swallowed
            errors[i] = exc

    threads = [
        threading.Thread(target=runner, args=(i,), name=f"storm-{i}", daemon=True)
        for i in range(n)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=timeout)
    for i, t in enumerate(threads):
        if t.is_alive():
            errors[i] = TimeoutError(f"task {i} still running after {timeout}s")
    return results, errors


def fingerprint(from_account_id, to_account_id, amount_minor, currency):
    """Canonical request fingerprint (room plan §2.1).

    It exists only so same-key-different-body can be detected; it is *not* the
    idempotency key, and the harness never derives a key from it.
    """
    canonical = "|".join(
        [str(from_account_id), str(to_account_id), str(amount_minor), str(currency)]
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# the currency the scenarios run in
# --------------------------------------------------------------------------

CURRENCY = "USD"
